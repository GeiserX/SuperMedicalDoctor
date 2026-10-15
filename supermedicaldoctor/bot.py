"""The bot: free text, voice notes, photos and files for the assistant, the commands, and the
reminders it sends by itself.

Text meant for the bot is queued in SQLite (`pending:<chat>`) and one worker thread runs assistant
turns, one at a time, so a command or a tap is answered while the assistant thinks. A voice note
is queued the same way and turned into text by the plugin on a hearing thread. A photo or a file
rides on its line and is downloaded when the turn starts. Slow work a plugin hands back with its
answer (`later`) runs on a third thread, one job at a time, and a person's answer to an action the
assistant wants to take goes back on a fourth.

One answer per burst: a turn starts DEBOUNCE_S after the chat's newest message, and waits up to
VOICE_WAIT_S for a voice note of that chat still being heard. What arrives while the turn runs is
handed to the plugin (`turn["incoming"]`); the answer names what it covered, and the rest goes with
the next turn. An edited message is handled by where it is: still queued, its new wording goes; in
the running turn, a correction joins it; already answered, a new turn checks that answer; an edit
that now addresses the bot is a new message.

Scope is structural: a private chat's turn carries only that person's data, a group turn
everyone's. Before every turn the data repository is pulled and the listed files of other
repositories are fetched, so the assistant answers from the latest commit.

The service log gets event kinds and counts only: never a message text, a value or a name.
"""

from __future__ import annotations

import html
import logging
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from supermedicaldoctor import assistant, edits, export, records, schedule
from supermedicaldoctor.i18n import Translator
from supermedicaldoctor.records import (
    SETTINGS_FILE,
    Attachment,
    BlockError,
    Person,
    PersonData,
    Settings,
    load_settings,
    settings_errors,
)
from supermedicaldoctor.repo import (
    DATA_LOCK,
    KV,
    LOG_PREFIX,
    PULL_EVERY_S,
    STATE_DB,
    UNDO_PREFIX,
    GitError,
    Repo,
)
from supermedicaldoctor.sources import Sources
from supermedicaldoctor.telegram import TelegramError, safe

log = logging.getLogger("smd.bot")

PENDING_MAX = 10  # the newest lines kept for the assistant
PENDING_MAX_AGE_S = 7 * 24 * 3600  # and none older than this
TEXT_MAX = 4096  # Telegram's limit on one message, in UTF-16 code units
FILE_MAX = 20 * 1024 * 1024  # the largest file the Bot API lets a bot download
TURN_FILES_MAX = 25 * 1024 * 1024  # the photos and files one turn carries at most
ECHO_MAX = 4000  # the «🎤» echo of a transcript is cut to this, in UTF-16 code units
FILE_MIMES = ("application/pdf", "image/jpeg", "image/png", "image/heic", "image/heif")
ADMIN_STATUSES = frozenset({"administrator", "creator"})
ANONYMOUS_ADMIN = 1087968824  # Telegram's GroupAnonymousBot: an admin writing as the group
NOT_MODIFIED = "message is not modified"
POLL_S = 20  # getUpdates long poll
WAIT_409_S = 30  # another poller holds the token: wait and keep the offset
TICK_S = 60  # a long wait ticks at least this often: the healthcheck allows 180 s
BACKOFF_MIN_S, BACKOFF_MAX_S = 5, 300  # 5xx and network failures
MENU = ("citas", "medicacion", "suplementos", "recordatorios", "deshacer", "ayuda")
DEBOUNCE_S = 2.5  # a chat's turn starts this long after its newest message: a burst goes together
VOICE_WAIT_S = 30  # and waits this long at most, from a note's arrival, for a note being heard
SAID_MAX = 30  # answered messages kept per chat, so an edit of one can be compared with it
ANSWER_CLIP = 1500  # characters of the earlier answer that an edit of an answered message carries
OPTIONS_TTL_S = 12 * 3600  # a choice under an answer can be tapped this long
OPTIONS_KEEP = 20  # offers kept per chat
ASK_DEFAULT_S = 300  # an ask that names no end of its own waits this long
NO_ONE = "no one could be asked"  # the note of an ask declined because nobody could answer it
CONTEXT_MAX = 50  # unaddressed group lines a turn takes as context, the newest
LOG_KEEP_DAYS = 7  # the chat log keeps at least this much, for the context lines of a turn
NOT_TO_YOU = "[not to you] "  # the mark of a group line that did not call the bot
OFFER_GONE = "remind_timeout"  # the label an offer's buttons give way to when nobody answered
REMIND_TTL_S = 1800  # an offered reminder can be tapped this long; then nothing is done
REMIND_DAYS = 60  # the furthest ahead a reminder may be
LATE_S = 600  # a reminder posted this much after its time says so
APPT_TTL_S = 12 * 3600  # an appointment's calendar offer can be tapped this long
FOLLOWUP_TTL_S = 12 * 3600  # and the ask after an appointment
GRANT_SLACK_S = 60  # a tap approves an addition at its time, give or take this
UNDO_KEEP = 20  # record commits remembered per chat for /deshacer
INSTR_TTL_S = 12 * 3600  # a change that drops instruction lines can be approved this long
OWN_KEEP = 200  # ids of the bot's own messages remembered per chat

_COMMAND = re.compile(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?$", re.DOTALL)


# --- the worker ----------------------------------------------------------------------------


class Worker:
    """One thread that runs jobs one at a time. A chat is queued once however many lines wait for
    it; a line that arrives mid-turn queues it again."""

    def __init__(self, fn: Callable, name: str = "smd-worker"):
        self._fn = fn
        self._cond = threading.Condition()
        self._queue: deque = deque()
        self._busy = False
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def submit(self, chat) -> None:
        with self._cond:
            if chat not in self._queue:
                self._queue.append(chat)
            self._cond.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                chat = self._queue.popleft()
                self._busy = True
            try:
                self._fn(chat)
            except Exception as e:  # noqa: BLE001 - one bad turn must not stop the worker
                log.error("job crashed: %s", safe(e))
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def idle(self) -> bool:
        with self._cond:
            return not self._queue and not self._busy

    def join_idle(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queue or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("worker_busy")
                self._cond.wait(remaining)

    def stop(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        self._thread.join(timeout)


@dataclass
class Turn:
    """One assistant turn running for a chat: the lines it started with, the lines handed to it
    while it ran, who wrote them, where to answer and the asks it posted."""

    chat: int
    ids: list
    users: list[int]
    mid: int | None  # the message an ask replies to: the newest the turn started with
    thread: int | None = None  # the forum topic the turn answers in
    pulled: list = field(default_factory=list)
    asks: list[str] = field(default_factory=list)
    edited: dict = field(default_factory=dict)  # message id -> its newest wording
    closed: bool = False
    grants: list = field(default_factory=list)  # taps on a calendar button: what each approves

    def has(self, line_id: object) -> bool:
        return line_id in self.ids or line_id in self.pulled


def _mid(line: Mapping) -> int | None:
    if "mid" in line:
        return line["mid"]
    return line["id"] if isinstance(line.get("id"), int) else None


def _users(lines: Sequence[Mapping]) -> list[int]:
    return list(dict.fromkeys(x["uid"] for x in lines if x.get("uid")))


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).isoformat(timespec="seconds")


def _grant(raw: Mapping) -> dict:
    out = dict(raw)
    if isinstance(out.get("events"), list):
        out["events"] = [dict(e) for e in out["events"] if isinstance(e, dict)]
    return out


def _same_event(event: object, entry: Mapping) -> bool:
    if not isinstance(event, Mapping):
        return False
    return all(
        str(event.get(k) or "") == str(entry.get(k) or "") for k in ("title", "start", "end")
    )


def _words(text: str) -> str:
    return " ".join(str(text or "").split())


def _keyboard(rows: Sequence[Sequence[tuple[str, str]]]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row] for row in rows
        ]
    }


def _safe_name(name: str, fallback: str) -> str:
    folded = unicodedata.normalize("NFKD", name or "")
    ascii_ = "".join(c for c in folded if not unicodedata.combining(c))
    stem, dot, ext = ascii_.rpartition(".")
    if not dot:
        stem, ext = ascii_, ""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")[:60] or fallback
    ext = re.sub(r"[^A-Za-z0-9]+", "", ext)[:5].lower()
    return f"{stem}.{ext}" if ext else stem


def fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in folded if not unicodedata.combining(c)).strip()


class Interrupted(BaseException):
    """Raised by the SIGTERM handler to cut a long poll short; never caught by `except
    Exception`."""


# --- the bot -------------------------------------------------------------------------------


class Bot:
    def __init__(
        self,
        data: str | Path,
        tg,
        *,
        now: Callable[[], datetime] | None = None,
        token: str = "",
        settings: Settings | None = None,
        alive: Path | None = None,
        sources: Sources | None = None,
    ):
        self.data = Path(data)
        self.tg = tg
        self.now = now or (lambda: datetime.now(UTC))
        self.settings = settings or load_settings(self.data)
        self.t = Translator(self.settings.language)
        self.repo = Repo(self.data, token)
        self.kv = KV(self.data / STATE_DB)
        self.sources = sources or Sources(self.data)
        self.bot_id: int | None = None
        self.username = ""
        self.last_ai: datetime | None = None  # the last successful assistant turn, for /estado
        self.alive = alive
        self.polling = False
        self._backoff = BACKOFF_MIN_S
        self._resets: Counter[int] = Counter()
        self._conv_lock = threading.Lock()
        self.commands: dict[str, tuple[Callable, bool]] = {}
        for names, handler, admin in (
            (("citas", "appointments"), self.cmd_citas, False),
            (("medicacion", "meds"), self.cmd_medicacion, False),
            (("suplementos", "supplements"), self.cmd_suplementos, False),
            (("recordatorios", "reminders"), self.cmd_recordatorios, False),
            (("deshacer", "undo"), self.cmd_deshacer, False),
            (("estado", "status"), self.cmd_estado, True),
            (("nuevo", "new"), self.cmd_nuevo, True),
            (("presentacion", "about"), self.cmd_presentacion, True),
            (("ayuda", "help"), self.cmd_ayuda, False),
        ):
            for name in names:
                self.commands[name] = (handler, admin)
        self.worker = Worker(self._turn)
        self.later = Worker(lambda job: job(), name="smd-later")
        self.debounce_s, self.voice_wait_s = DEBOUNCE_S, VOICE_WAIT_S
        self.clock, self.sleep = time.monotonic, time.sleep
        self._q = threading.Lock()
        self._newest: dict[int, float] = {}
        self._voicing: dict[int, dict] = {}
        self._running: dict[int, Turn] = {}
        self.asks: dict[str, dict] = {}
        self.hearer = Worker(self._hear_chat, name="smd-hear")
        self.side = Worker(lambda job: job(), name="smd-asks")

    def start(self) -> None:
        """`getMe`, then whether the bot is an administrator of the group: Telegram delivers
        every group message (plain text included) only to a bot that is privacy-off or a group
        administrator, and voice notes need it."""
        me = self.tg.call("getMe") or {}
        self.bot_id = me.get("id")
        self.username = str(me.get("username") or "")
        log.info("bot @%s up", self.username)
        if me.get("can_read_all_group_messages") or self.group_admin():
            log.info("group privacy off: every group message arrives")
        else:
            log.warning("group privacy on: make the bot a group admin so every message arrives")

    def group_admin(self) -> bool:
        chat = self.settings.chat
        if self.bot_id is None or chat is None:
            return False
        try:
            member = self.tg.call("getChatMember", chat_id=chat, user_id=self.bot_id)
        except TelegramError as e:
            log.warning("getChatMember failed: %s", safe(e))
            return False
        return isinstance(member, Mapping) and member.get("status") in ADMIN_STATUSES

    def set_commands(self) -> None:
        """The menu, in the default scope and for Spanish clients."""
        menu = self.t("menu")
        commands = [{"command": name, "description": menu[name]} for name in MENU]
        for language in (None, "es"):
            try:
                self.tg.call("setMyCommands", commands=commands, language_code=language)
            except TelegramError as e:
                log.warning("setMyCommands failed: %s", safe(e))

    def close(self) -> None:
        self.hearer.stop()
        self.worker.stop()
        self.later.stop()
        self.side.stop()
        self.kv.close()

    def join_idle(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        workers = (self.hearer, self.worker, self.side)
        while not all(w.idle() for w in workers):
            for w in workers:
                w.join_idle(max(0.01, deadline - time.monotonic()))

    # --- who is who ----------------------------------------------------------------------

    @property
    def has_assistant(self) -> bool:
        return bool(self.settings.plugin())

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    def today(self) -> date:
        return self.now().astimezone(self.zone()).date()

    def person(self, user: object) -> Person | None:
        return self.settings.by_user(user)

    def is_admin(self, user: object) -> bool:
        p = self.person(user)
        return p is not None and self.settings.is_admin(p.id)

    def admin_uids(self) -> list[int]:
        out = []
        for pid in self.settings.admins:
            p = self.settings.people.get(pid)
            if p is not None and p.user_id is not None:
                out.append(p.user_id)
        return out

    def name_of(self, user: object) -> str:
        p = self.person(user)
        return p.name if p else ""

    def mention(self, p: Person | None) -> str:
        """`@username` when the settings have one, else an HTML text mention."""
        if p is None:
            return ""
        if p.username:
            return "@" + p.username
        return f'<a href="tg://user?id={p.user_id}">{html.escape(p.name)}</a>'

    def mention_uid(self, uid: object) -> str:
        return self.mention(self.person(uid))

    def allowed_chat(self, chat: Mapping, user: object) -> bool:
        """The group, or a private chat with a person."""
        if chat.get("type") == "private":
            return self.person(user) is not None
        return chat.get("id") == self.settings.chat

    def scope_of(self, chat: int) -> list[str]:
        """The people whose data a turn in `chat` carries: the person alone in their private
        chat, everyone in the group."""
        if chat == self.settings.chat:
            return list(self.settings.people)
        p = self.person(chat)
        return [p.id] if p else []

    # --- Telegram ----------------------------------------------------------------------

    def _own(self, chat: int, sent: object) -> None:
        mid = sent.get("message_id") if isinstance(sent, Mapping) else None
        if isinstance(mid, int):
            self.kv.update_json(f"own:{chat}", lambda cur: [*(cur or []), mid][-OWN_KEEP:], [])

    def thread_for(self, chat: int, thread: int | None = None) -> int | None:
        """The forum topic to post in: the one given, or `telegram.thread_id` in the group."""
        if thread is not None:
            return thread
        return self.settings.thread if chat == self.settings.chat else None

    def send(
        self, chat: int, text: str, markup: dict | None = None, thread: int | None = None, **extra
    ) -> dict | None:
        sent = self.tg.call(
            "sendMessage",
            chat_id=chat,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            message_thread_id=self.thread_for(chat, thread),
            **extra,
        )
        self._own(chat, sent)
        return sent

    def post(
        self, chat: int, text: str, markup: dict | None = None, thread: int | None = None, **extra
    ) -> dict | None:
        try:
            return self.send(chat, text, markup, thread, **extra)
        except TelegramError as e:
            log.warning("send failed: %s", safe(e))
            return None

    def answer(self, callback_id: str, text: str | None = None) -> None:
        try:
            self.tg.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as e:
            log.warning("answerCallbackQuery failed: %s", safe(e))

    def unbutton(self, chat: int, message_id: object, label: str | None = None) -> None:
        if not message_id:
            return
        markup = _keyboard([[(label, "x")]]) if label else {"inline_keyboard": []}
        try:
            self.tg.call(
                "editMessageReplyMarkup", chat_id=chat, message_id=message_id, reply_markup=markup
            )
        except TelegramError as e:
            log.info("buttons not removed: %s", safe(e))

    def rekey(self, chat: int, message_id: object, rows: list) -> None:
        if not message_id:
            return
        try:
            self.tg.call(
                "editMessageReplyMarkup",
                chat_id=chat,
                message_id=message_id,
                reply_markup=_keyboard(rows),
            )
        except TelegramError as e:
            log.info("buttons not changed: %s", safe(e))

    # --- updates -----------------------------------------------------------------------

    def handle(self, update: Mapping) -> None:
        if "callback_query" in update:
            self.on_callback(update["callback_query"])
        elif "message" in update:
            self.on_message(update["message"])
        elif "edited_message" in update:
            self.on_edit(update["edited_message"])

    def on_callback(self, cq: Mapping) -> None:
        user = (cq.get("from") or {}).get("id")
        message = cq.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        if self.person(user) is None:
            self.answer(cq["id"], self.t("not_for_you"))
            log.info("callback refused: not_allowed")
            return
        data = cq.get("data")
        if chat is None or not isinstance(data, str):
            self.answer(cq["id"])
            return
        handler = {
            "a": self.tapped_ask,
            "o": self.tapped_option,
            "r": self.tapped_remind,
            "c": self.tapped_appt,
            "f": self.tapped_followup,
            "i": self.tapped_instr,
            "e": self.tapped_edit,
            "k": self.tapped_cancel,
        }.get(data.split(":", 1)[0])
        if handler is None:
            self.answer(cq["id"])
            return
        handler(cq, chat, data, user)

    # --- messages ------------------------------------------------------------------------

    def on_message(self, msg: Mapping) -> None:
        chat = msg.get("chat") or {}
        user = (msg.get("from") or {}).get("id")
        if not self.allowed_chat(chat, user):
            return
        if msg.get("voice") or msg.get("audio"):
            self.on_voice(msg)
            return
        if msg.get("photo") or msg.get("document"):
            self.on_file(msg)
            return
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        p = self.person(user)
        private = chat.get("type") == "private"
        if p is None:  # someone else in the group: never answered, maybe kept as context
            frm = msg.get("from") or {}
            if frm.get("id") == ANONYMOUS_ADMIN or msg.get("sender_chat"):
                log.info("group text from an anonymous admin or a channel: not a person")
            self.log_seen(chat["id"], msg, text)
            return
        command = _COMMAND.match(text.strip())
        if command:
            name, handle, rest = command.group(1).lower(), command.group(2), command.group(3)
            if handle and handle.lower() != self.username.lower():
                return
            entry = self.commands.get(name)
            if entry is not None and (not entry[1] or self.settings.is_admin(p.id)):
                entry[0](msg, (rest or "").strip(), p)
                log.info("command %s", entry[0].__name__.removeprefix("cmd_"))
            return
        content = self.addressed(msg, text, private, p)
        if content is not None:
            self.queue_text(chat["id"], msg, content, p, self._strip_mention(msg, text)[0])
        elif not private:
            log.info("group text not for the bot: %s", self.not_addressed(msg, private))
            self.log_seen(chat["id"], msg, text)

    def sender(self, msg: Mapping) -> str:
        frm = msg.get("from") or {}
        p = self.person(frm.get("id"))
        if p is not None:
            return p.name
        return " ".join(str(frm.get("first_name") or "?").split())[:64]

    def log_seen(self, chat: int, msg: Mapping, text: str) -> None:
        """A group message that does not call the bot. With `history_scope: all` the chat log
        keeps it, and it goes to the next turn as context marked «[not to you]»."""
        if self.settings.history_scope != "all" or not self.has_assistant:
            return
        body = text.strip()
        if body.startswith("/"):
            return
        name = self.sender(msg)
        now = int(self.now().timestamp())
        self.kv.add_chat(
            chat,
            "user",
            name,
            f"{name}: {body}",
            now,
            body=body,
            mid=msg.get("message_id"),
            asked=False,
        )
        log.info("group line logged")

    def to_bot(self, msg: Mapping) -> bool:
        """Whether the message replies to one of the bot's messages: Telegram says the replied
        message is the bot's, or its id is one the bot sent in this chat, or (a reply from
        another topic or chat) its origin is the bot."""
        reply = msg.get("reply_to_message") or {}
        if self.bot_id is not None and (reply.get("from") or {}).get("id") == self.bot_id:
            return True
        chat = (msg.get("chat") or {}).get("id")
        own = self.kv.get_json(f"own:{chat}", []) or []
        mid = reply.get("message_id")
        if isinstance(mid, int) and mid in own:
            return True
        external = msg.get("external_reply") or {}
        origin = external.get("origin") or {}
        if self.bot_id is not None and (origin.get("sender_user") or {}).get("id") == self.bot_id:
            return True
        same_chat = (external.get("chat") or {}).get("id") in (None, chat)
        mid = external.get("message_id")
        return same_chat and isinstance(mid, int) and mid in own

    def not_addressed(self, msg: Mapping, private: bool) -> str:
        """Why a group text was not for the bot, as a kind for the service log."""
        if msg.get("reply_to_message") or msg.get("external_reply"):
            return "reply to someone else"
        return "not a reply and no mention"

    def addressed(self, msg: Mapping, text: str, private: bool, p: Person) -> str | None:
        """The line for the assistant when the message is meant for the bot, else None: a
        private chat, a reply to any bot message, or a message that mentions the bot."""
        to_bot = self.to_bot(msg)
        body, mentioned = self._strip_mention(msg, text)
        if not (private or to_bot or mentioned):
            return None
        body = body.strip()
        if not body:
            return None
        prefix = f"{p.name}: "
        if to_bot:
            prefix += f"[replying to the bot: {self.quoted(msg)}] "
        return prefix + body

    @staticmethod
    def quoted(msg: Mapping) -> str:
        """What the reply quotes: the part the person selected (`quote`), else the replied
        message's text or caption, whatever its formatting."""
        quote = msg.get("quote") or {}
        if isinstance(quote.get("text"), str) and quote["text"].strip():
            return quote["text"]
        for source in (msg.get("reply_to_message") or {}, msg.get("external_reply") or {}):
            for key in ("text", "caption"):
                if isinstance(source.get(key), str) and source[key].strip():
                    return source[key]
        return ""

    def _strip_mention(self, msg: Mapping, text: str, key: str = "entities") -> tuple[str, bool]:
        """Remove each `mention` entity equal to the bot's @username. Entity offsets count
        UTF-16 code units, so the text is cut in UTF-16."""
        if not self.username:
            return text, False
        raw = text.encode("utf-16-le")
        target = "@" + self.username.lower()
        spans = []
        for e in msg.get(key) or []:
            if e.get("type") != "mention":
                continue
            a, b = 2 * int(e.get("offset", 0)), 2 * int(e.get("offset", 0) + e.get("length", 0))
            if raw[a:b].decode("utf-16-le", "replace").lower() == target:
                spans.append((a, b))
        for a, b in sorted(spans, reverse=True):
            raw = raw[:a] + raw[b:]
        return raw.decode("utf-16-le", "replace"), bool(spans)

    def _base_line(self, msg: Mapping, p: Person) -> dict:
        chat = msg.get("chat") or {}
        return {
            "id": msg.get("message_id"),
            "ts": int(self.now().timestamp()),
            "uid": p.user_id,
            "pid": p.id,
            "admin": self.settings.is_admin(p.id),
            "thread": msg.get("message_thread_id") if msg.get("is_topic_message") else None,
            "private": chat.get("type") == "private",
        }

    def on_voice(self, msg: Mapping) -> None:
        """A voice note (or an audio file) from a person. It is always for the bot: a voice note
        cannot carry a mention. It is queued and the hearing thread turns it into text."""
        chat = msg["chat"]
        p = self.person((msg.get("from") or {}).get("id"))
        if p is None:
            return
        media = msg.get("voice") or msg.get("audio")
        if not isinstance(media, Mapping) or not media.get("file_id"):
            return
        line = self._base_line(msg, p)
        note = {"message_id": msg.get("message_id"), "allow_sending_without_reply": True}
        if not self.has_assistant:
            self.post(chat["id"], self.t("voice_off"), thread=line["thread"], reply_parameters=note)
            log.info("voice without assistant")
            return
        size = media.get("file_size")
        if isinstance(size, int) and size > FILE_MAX:
            self.post(
                chat["id"], self.t("voice_too_long"), thread=line["thread"], reply_parameters=note
            )
            log.info("voice refused: too_long")
            return
        line["voice"] = {
            "file_id": media["file_id"],
            "mime": str(media.get("mime_type") or "audio/ogg"),
        }
        line["who"] = p.name
        self.queue_line(chat["id"], line)
        log.info("voice queued")

    def on_file(self, msg: Mapping) -> None:
        """A photo or a file (PDF, JPEG, PNG, HEIC) from a person. In the group it is for the
        bot like a voice note: a lab sheet dropped in the doctor's group is meant for it. Its
        caption is the message. It is queued as a line naming the file; the bytes are fetched
        when the turn starts."""
        chat = msg["chat"]
        p = self.person((msg.get("from") or {}).get("id"))
        if p is None:
            return
        line = self._base_line(msg, p)
        note = {"message_id": msg.get("message_id"), "allow_sending_without_reply": True}
        mid = msg.get("message_id")
        if msg.get("photo"):
            sizes = [s for s in msg["photo"] if isinstance(s, Mapping) and s.get("file_id")]
            if not sizes:
                return
            big = max(sizes, key=lambda s: (s.get("file_size") or 0, s.get("width") or 0))
            media = {"file_id": big["file_id"], "file_size": big.get("file_size")}
            name, mime, label = f"inbox/{mid}-foto.jpg", "image/jpeg", "foto"
        else:
            doc = msg["document"]
            mime = str(doc.get("mime_type") or "").lower()
            if mime not in FILE_MIMES or not doc.get("file_id"):
                self.post(
                    chat["id"], self.t("file_kind"), thread=line["thread"], reply_parameters=note
                )
                log.info("file refused: kind")
                return
            media = {"file_id": doc["file_id"], "file_size": doc.get("file_size")}
            fallback = "documento" + records.DOC_EXTS.get(mime, "")
            name = f"inbox/{mid}-{_safe_name(str(doc.get('file_name') or ''), 'documento')}"
            if "." not in name.rsplit("/", 1)[-1]:
                name = f"inbox/{mid}-{fallback}"
            label = "archivo"
        size = media.get("file_size")
        if isinstance(size, int) and size > FILE_MAX:
            self.post(
                chat["id"], self.t("file_too_big"), thread=line["thread"], reply_parameters=note
            )
            log.info("file refused: too_big")
            return
        caption = str(msg.get("caption") or "")
        body = self._strip_mention(msg, caption, "caption_entities")[0].strip()
        line["file"] = {"file_id": media["file_id"], "name": name, "mime": mime, "size": size}
        line["content"] = f"{p.name}: [{label}: {name}] {body}".rstrip()
        line["body"] = body
        if not self.has_assistant:
            self.post(chat["id"], self.t("no_ai"), thread=line["thread"])
        line["row"] = self.kv.add_chat(
            chat["id"], "user", p.name, line["content"], line["ts"], body=body, mid=mid
        )
        self.queue_line(chat["id"], line, run=self.has_assistant)
        log.info("file queued")

    def queue_text(
        self,
        chat: int,
        msg: Mapping,
        content: str,
        p: Person,
        body: str | None = None,
        row: int | None = None,
    ) -> None:
        """Append a line to `pending:<chat>`, log it in the chat log (or reuse its `row`) and
        queue the chat for the worker. Without an assistant the line waits: the bot says it
        cannot read written messages yet and reads it once it can."""
        line = self._base_line(msg, p)
        if row is None:
            row = self.kv.add_chat(
                chat,
                "user",
                p.name,
                content,
                line["ts"],
                body=body.strip() if body is not None else None,
                mid=msg.get("message_id"),
            )
        line["content"], line["row"] = content, row
        if body is not None:
            line["body"] = body.strip()
        if not self.has_assistant:
            self.post(chat, self.t("no_ai"), thread=line["thread"])
            log.info("text without assistant")
        self.queue_line(chat, line, run=self.has_assistant)
        log.info("text queued")

    def queue_line(self, chat: int, line: dict, run: bool = True) -> None:
        """Append `line` to `pending:<chat>` (the newest 10, none older than 7 days) and queue
        the chat: a voice note for the hearing thread, text for the worker."""
        now = int(self.now().timestamp())
        dropped = []

        def add(lines):
            lines = [*(lines or []), line]
            kept = [x for x in lines if now - int(x.get("ts", 0)) <= PENDING_MAX_AGE_S]
            kept = kept[-PENDING_MAX:]
            dropped.append(len(lines) - len(kept))
            return kept

        self.kv.update_json(f"pending:{chat}", add, [])
        if dropped and dropped[0]:
            log.info("pending capped dropped=%d", dropped[0])
        if not run:
            return
        with self._q:
            self._newest[chat] = self.clock()
            if "voice" in line:
                self._voicing.setdefault(chat, {})[line["id"]] = self.clock()
        if "voice" in line:
            self.hearer.submit(chat)
        else:
            self.worker.submit(chat)

    # --- commands ------------------------------------------------------------------------

    def _chat(self, msg: Mapping) -> tuple[int, int | None]:
        thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        return msg["chat"]["id"], thread

    def reply(self, msg: Mapping, text: str, markup: dict | None = None) -> dict | None:
        chat, thread = self._chat(msg)
        return self.send(chat, text, markup, thread)

    def load(self, pid: str) -> PersonData:
        with DATA_LOCK:
            return records.load_person(self.data, pid)

    def when_text(self, d: date | None, t_: str | None) -> str:
        if d is None:
            return "?"
        text = self.t.date(d)
        return self.t("at_time", date=text, time=hm(t_)) if t_ else text

    def appt_text(self, a: Mapping) -> str:
        d = records.as_date(a.get("date"))
        line = self.t(
            "appt_line",
            when=self.when_text(d, records.clock_text(a.get("time"))),
            what=html.escape(str(a.get("what") or "")),
        )
        if a.get("place"):
            line += self.t("appt_place", place=html.escape(str(a["place"])))
        return line

    def cmd_citas(self, msg: Mapping, rest: str, p: Person) -> None:
        """/citas: the asker's pending appointments, soonest first. No AI."""
        self.repo.pull()
        try:
            pd = self.load(p.id)
        except ValueError:
            self.reply(msg, self.t("data_unreadable"))
            return
        today = self.today()
        pending = [
            a
            for a in pd.appointments
            if isinstance(a, Mapping)
            and a.get("status", "pending") == "pending"
            and (records.as_date(a.get("date")) or date.min) >= today
        ]
        if not pending:
            self.reply(msg, self.t("appts_none", name=html.escape(p.name)))
            return
        pending.sort(key=lambda a: (str(a.get("date")), str(records.clock_text(a.get("time")))))
        lines = [self.t("appts_title", name=html.escape(p.name))]
        for a in pending[:30]:
            lines.append("• " + self.appt_text(a))
            for prep in a.get("prep") or []:
                if isinstance(prep, Mapping):
                    lines.append(
                        "   "
                        + self.t(
                            "prep_line",
                            text=html.escape(str(prep.get("text", ""))),
                            hours=prep.get("hours_before"),
                        )
                    )
        self.reply(msg, "\n".join(lines))

    def _meds_text(self, p: Person, kind: str) -> str:
        pd = self.load(p.id)
        today = self.today()
        taken = [
            m
            for m in pd.meds
            if isinstance(m, Mapping) and m.get("kind") == kind and records.active(m, today)
        ]
        if not taken:
            return self.t(f"{kind}_none", name=html.escape(p.name))
        lines = [self.t(f"{kind}_title", name=html.escape(p.name))]
        for slot in records.SLOTS:
            here = [m for m in taken if (m.get("slot") or "as_needed") == slot]
            if not here:
                continue
            lines.append(f"<b>{html.escape(self.t.slot(slot))}</b>")
            for m in sorted(here, key=lambda m: str(m.get("name"))):
                item = " ".join(str(x) for x in (m.get("name"), m.get("dose")) if x)
                if m.get("days") not in (None, "daily"):
                    days = ", ".join(self.t("days")[records.WEEKDAYS.index(d)] for d in m["days"])
                    item += f" ({days})"
                if m.get("food"):
                    item += f". {m['food']}"
                lines.append("• " + html.escape(item))
        return "\n".join(lines)

    def cmd_medicacion(self, msg: Mapping, rest: str, p: Person) -> None:
        """/medicacion: the asker's medicines still taken, by slot. No AI."""
        self.repo.pull()
        try:
            self.reply(msg, self._meds_text(p, "medicine"))
        except ValueError:
            self.reply(msg, self.t("data_unreadable"))

    def cmd_suplementos(self, msg: Mapping, rest: str, p: Person) -> None:
        """/suplementos: the asker's supplements still taken, by slot. No AI."""
        self.repo.pull()
        try:
            self.reply(msg, self._meds_text(p, "supplement"))
        except ValueError:
            self.reply(msg, self.t("data_unreadable"))

    def cmd_recordatorios(self, msg: Mapping, rest: str, p: Person) -> None:
        """/recordatorios: the chat's reminders still to come, each with a button to cancel it."""
        chat, _ = self._chat(msg)
        rows = self.kv.reminders(chat)[:20]
        if not rows:
            self.reply(msg, self.t("reminders_none"))
            return
        lines, buttons = [self.t("reminders_title")], []
        for n, r in enumerate(rows, 1):
            who = self.name_of(r["uid"])
            name = f" · {html.escape(who)}" if who else ""
            when = self.reminder_when(r["at"])
            lines.append(f"{n}. {when}{name}: {html.escape(r['text'])}")
            buttons.append([(self.t("reminders_cancel", n=n), f"k:{r['id']}:{n}")])
        self.reply(msg, "\n".join(lines), _keyboard(buttons))

    def cmd_deshacer(self, msg: Mapping, rest: str, p: Person) -> None:
        """/deshacer: revert the last record commit made from this chat, pushed."""
        chat, _ = self._chat(msg)
        undo = self.kv.get_json(f"undo:{chat}", []) or []
        if not undo:
            self.reply(msg, self.t("undo_none"))
            return
        last = undo[-1]
        pid = str(last.get("person") or "")
        try:
            sha = self.repo.revert(
                str(last["sha"]),
                lambda: self.export_files(),
                f"{UNDO_PREFIX}{pid}): revert {str(last['sha'])[:8]}",
            )
        except (GitError, OSError, ValueError) as e:
            log.warning("undo failed: %s", safe(e))
            self.reply(msg, self.t("undo_failed"))
            return
        self.kv.update_json(f"undo:{chat}", lambda cur: (cur or [])[:-1] or None, [])
        if sha is None:
            self.reply(msg, self.t("undo_none"))
            return
        self.reply(msg, self.t("undo_done", what=html.escape(str(last.get("said") or ""))))
        log.info("undo done")

    def cmd_estado(self, msg: Mapping, rest: str, p: Person) -> None:
        """/estado (admins): the data repository's sync, each source's fetch time, the last
        answer and the assistant."""
        self.reply(msg, self.status_text())

    def status_text(self) -> str:
        t = self.t
        head = self.repo.head() or "-"
        if self.repo.pull_ok is None:
            pulled = t("status_pull_never")
        elif self.repo.pull_ok and self.repo.pulled_at is not None:
            pulled = t("status_pull_ok", ago=_ago(self.repo.clock() - self.repo.pulled_at))
        else:
            pulled = t("status_pull_failed")
        unpushed = self.repo.unpushed()
        if unpushed:
            git = t("status_git_unpushed", n=unpushed)
        elif self.repo.remote_ok is False:
            git = t("status_git_down")
        elif unpushed is None:
            git = t("status_git_unknown")
        else:
            git = t("status_git_ok")
        if not self.has_assistant:
            ai = t("status_ai_none")
        elif self.last_ai is None:
            ai = t("status_ai_never")
        else:
            ai = t("status_ai_ok", ago=_ago((self.now() - self.last_ai).total_seconds()))
        lines = [t("status_data", head=head, pulled=pulled, git=git), t("status_ai", ai=ai)]
        files = self.sources.status(self.settings.sources)
        if files:
            lines.append(t("status_sources"))
            for key, fetched in files:
                if fetched is None:
                    lines.append(f"• {html.escape(key)}: {t('status_source_never')}")
                else:
                    at = datetime.fromtimestamp(fetched, self.zone())
                    when = t("at_time", date=t.date(at.date(), "short"), time=hm(f"{at:%H:%M}"))
                    lines.append(f"• {html.escape(key)}: {when}")
        return "\n".join(lines)

    def cmd_nuevo(self, msg: Mapping, rest: str, p: Person) -> None:
        """/nuevo (admins): drop the pending lines, the chat log and the assistant conversation,
        always, so an admin can always reset a chat."""
        chat, _ = self._chat(msg)
        with self._conv_lock:
            self._resets[chat] += 1
            conversation = self.kv.get(f"conv:{chat}")
            self.kv.delete(f"conv:{chat}")
        for key in ("pending", "said", "options", "ctx"):
            self.kv.delete(f"{key}:{chat}")
        self.kv.chat_clear(chat)
        if conversation and self.has_assistant:
            block = self.settings.assistant or {}
            try:
                assistant.forget(
                    self.data, block.get("plugin"), conversation, self.plugin_settings()
                )
            except Exception as e:  # noqa: BLE001 - the plugin may raise anything
                log.warning("forget failed: %s", safe(e))
        self.reply(msg, self.t("new_conversation"))

    def cmd_presentacion(self, msg: Mapping, rest: str, p: Person) -> None:
        """/presentacion (admins): the introduction, posted when an admin asks for it."""
        self.reply(msg, self.t("intro"))

    def cmd_ayuda(self, msg: Mapping, rest: str, p: Person) -> None:
        text = self.t("help")
        if self.settings.is_admin(p.id):
            text += "\n\n" + self.t("help_admin")
        self.reply(msg, text)

    # --- the loop ------------------------------------------------------------------------

    def tick(self, now: datetime | None = None) -> None:
        """Between two polls: the data pull every 10 minutes, a push left behind, the
        appointment reminders, the reminders set from the chat, the expiry of asks and offers,
        the chat log's pruning and the heartbeat."""
        now = now or self.now()
        for step, what in (
            (lambda: self.repo.pull(PULL_EVERY_S), "git pull"),
            (self.repo.flush, "git sync"),
            (self.reload_if_pulled, "settings reload"),
            (lambda: self.tick_appointments(now), "appointment reminders"),
            (self.tick_reminders, "reminder tick"),
            (self.expire_asks, "ask expiry"),
            (self.expire_offers, "offer expiry"),
            (self.expire_options, "options expiry"),
            (lambda: self.prune_log(now), "log prune"),
        ):
            try:
                step()
            except Exception as e:  # noqa: BLE001 - the loop must go on
                log.warning("%s failed: %s", what, safe(e))
        self.heartbeat()

    def prune_log(self, now: datetime) -> None:
        hour = int(now.timestamp()) // 3600
        if getattr(self, "_pruned", None) == hour:
            return
        self._pruned = hour
        days = max(self.settings.days, LOG_KEEP_DAYS)
        gone = self.kv.chat_prune(int(now.timestamp()) - days * 86400)
        if gone:
            log.info("chat log pruned rows=%d", gone)

    def heartbeat(self) -> None:
        if self.alive is not None:
            try:
                self.alive.touch()
            except OSError as e:
                log.warning("heartbeat failed: %s", safe(e))

    @staticmethod
    def wait(stop: threading.Event, seconds: float, between: Callable[[], None]) -> None:
        while seconds > 0 and not stop.wait(min(seconds, TICK_S)):
            seconds -= TICK_S
            if seconds > 0:
                between()

    def poll_once(self) -> float:
        offset = self.kv.get("offset")
        self.polling = True
        try:
            updates = self.tg.call(
                "getUpdates",
                offset=int(offset) if offset else None,
                timeout=POLL_S,
                allowed_updates=["message", "edited_message", "callback_query"],
            )
        except TelegramError as e:
            log.warning("getUpdates failed: %s", safe(e))
            if e.code == 401:
                raise
            if e.code == 409:
                return WAIT_409_S
            if e.code == 429:
                return float(min(e.retry_after or BACKOFF_MIN_S, BACKOFF_MAX_S))
            delay = self._backoff
            self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
            return delay
        finally:
            self.polling = False
        self._backoff = BACKOFF_MIN_S
        for update in updates or []:
            try:
                self.handle(update)
            except Exception as e:  # noqa: BLE001 - one bad update must not stop the bot
                log.error("update failed: %s", safe(e))
            self.kv.set("offset", str(int(update["update_id"]) + 1))
        return 0

    def run(self, stop: threading.Event) -> None:
        """getMe, the menu, the remote check and the export once, then poll and tick until
        `stop` is set. On the way out a push left behind is tried once more."""
        delay = BACKOFF_MIN_S
        while not stop.is_set():
            try:
                self.start()
                break
            except TelegramError as e:
                if e.code == 401:
                    raise
                log.warning("getMe failed: %s", safe(e))
                self.wait(stop, delay, self.heartbeat)
                delay = min(delay * 2, BACKOFF_MAX_S)
        self.repo.check_remote()
        self.repo.pull(0)
        self.reload_if_pulled()
        self.sync_export()
        self.set_commands()
        self.resume()
        try:
            while not stop.is_set():
                try:
                    wait = self.poll_once()
                except Interrupted:
                    break
                self.tick()
                if wait:
                    self.wait(stop, wait, self.tick)
        finally:
            self.repo.flush(force=True)
            log.info("stopped")

    def resume(self) -> None:
        """At start: voice notes a restart left unheard go to the hearing thread, and lines that
        waited for the assistant go to the worker."""
        for key in self.kv.keys("pending:"):
            chat = int(key.removeprefix("pending:"))
            lines = self.kv.get_json(key, []) or []
            if any("voice" in x for x in lines):
                self.hearer.submit(chat)
            elif self.has_assistant and any("content" in x for x in lines):
                self.worker.submit(chat)

    # --- the data ------------------------------------------------------------------------

    def plugin_settings(self) -> dict:
        """What the plugin gets as `settings`: the `assistant:` block minus `plugin` and
        `instructions`, plus the people and the group's id."""
        out = assistant.settings(self.settings.assistant)
        out["people"] = {
            pid: {
                "name": p.name,
                "user_id": p.user_id,
                "username": p.username,
                "calendar": p.calendar,
            }
            for pid, p in self.settings.people.items()
        }
        out["chat_id"] = self.settings.chat
        return out

    def everyone(self) -> dict[str, PersonData]:
        return records.load_people(self.data, list(self.settings.people))

    def export_files(self, override: Mapping[str, PersonData | None] | None = None) -> dict:
        """Every export the settings list, rendered from the data on disk (with `override` in
        place of what is on disk for those people); an export that already says the same keeps
        its text, so its `generated` time moves only with a change."""
        people = self.everyone()
        people.update({k: v for k, v in (override or {}).items() if v is not None})
        now = self.now()
        out = {}
        for ex in self.settings.exports:
            if not isinstance(ex, Mapping) or ex.get("kind") != "chef":
                continue
            path = str(ex["path"])
            try:
                old = (self.data / path).read_text(encoding="utf-8")
            except FileNotFoundError:
                old = None
            if export.same(old, people, now, self.settings.timezone):
                out[path] = old
            else:
                out[path] = export.render(people, now, self.settings.timezone)
        return out

    def sync_export(self) -> None:
        """At start: commit the exports when they no longer say what the data says (an
        appointment's fasting window left the 14 days, a hand edit pulled from the remote)."""
        try:
            with DATA_LOCK:
                files = {
                    p: text
                    for p, text in self.export_files().items()
                    if self.repo._read(p) != text.encode("utf-8")
                }
                if files and self.repo.is_checkout():
                    self.repo.commit(files, "export: chef")
                    log.info("export refreshed")
        except (GitError, OSError, ValueError) as e:
            log.warning("export failed: %s", safe(e))

    def reload_if_pulled(self) -> None:
        if self.repo.settings_pulled:
            self.repo.settings_pulled = False
            self.reload_settings()

    def reload_settings(self) -> bool:
        """After a pull changed `settings.yaml`: use the new one, unless it fails the checks of
        `smd check`; then the old one stays and the log says so. Lines that waited for an
        assistant go to it once one is configured."""
        try:
            s = load_settings(self.data)
            errors = settings_errors(s, self.data)
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            errors = ["bad_settings"]
        if errors:
            log.warning("settings reload refused: %s", ", ".join(errors))
            return False
        had = self.has_assistant
        if s.language != self.settings.language:
            self.t = Translator(s.language)
        self.settings = s
        log.info("settings reloaded")
        if self.has_assistant and not had:
            self.resume()
        return True

    # --- the assistant turn (worker thread) ----------------------------------------------

    def settle(self, chat: int) -> None:
        while True:
            with self._q:
                newest = self._newest.get(chat)
                notes = list((self._voicing.get(chat) or {}).values())
            now = self.clock()
            wait = newest + self.debounce_s - now if newest is not None else 0.0
            if notes:
                wait = max(wait, min(notes) + self.voice_wait_s - now)
            if wait <= 0:
                return
            self.sleep(min(wait, 0.5))

    def _turn(self, chat: int) -> None:
        key = f"pending:{chat}"
        self.settle(chat)
        if not self.has_assistant:
            return
        with self._q:
            lines = [x for x in self.kv.get_json(key, []) or [] if "content" in x]
            if not lines:
                return
            state = Turn(
                chat,
                [x["id"] for x in lines],
                _users(lines),
                _mid(lines[-1]),
                lines[-1].get("thread"),
            )
            state.grants = [_grant(x["grant"]) for x in lines if isinstance(x.get("grant"), dict)]
            self._running[chat] = state
        try:
            self._converse(chat, key, lines, state)
        finally:
            with self._q:
                state.closed = True
                self._running.pop(chat, None)
            self.close_asks(state)

    def fetch_files(self, lines: list[dict], chat: int) -> tuple[list[dict], list[Attachment]]:
        """Download the photos and files of the turn's lines, 20 MiB each and 25 MiB in all.
        Lines past the budget wait for the next turn; a file that cannot be fetched says so
        under its message and its line goes on without it."""
        kept, atts, total = [], [], 0
        for x in lines:
            f = x.get("file")
            if not isinstance(f, Mapping):
                kept.append(x)
                continue
            size = f.get("size") if isinstance(f.get("size"), int) else 0
            if atts and total + size > TURN_FILES_MAX:
                continue
            try:
                info = self.tg.call("getFile", file_id=f["file_id"])
                info = info if isinstance(info, Mapping) else {}
                path = info.get("file_path")
                if isinstance(info.get("file_size"), int) and info["file_size"] > FILE_MAX:
                    raise TelegramError("getFile", 413, "file too large")
                if not isinstance(path, str) or not path:
                    raise TelegramError("getFile", 0, "no file path")
                data = self.tg.download(path, FILE_MAX)
            except TelegramError as e:
                log.warning("file not fetched: %s", safe(e))
                note = {"message_id": _mid(x), "allow_sending_without_reply": True}
                too_big = e.code == 413 or "too big" in e.description.lower()
                self.post(
                    chat,
                    self.t("file_too_big" if too_big else "file_failed"),
                    thread=x.get("thread"),
                    reply_parameters=note,
                )
                kept.append({**x, "file": None})
                continue
            if atts and total + len(data) > TURN_FILES_MAX:
                continue
            total += len(data)
            atts.append(Attachment(str(f["name"]), str(f["mime"]), data, x.get("pid")))
            kept.append(x)
        return kept, atts

    def _converse(self, chat: int, key: str, lines: list[dict], state: Turn) -> None:
        s = self.settings
        block_cfg = s.assistant or {}
        try:
            self.tg.call(
                "sendChatAction",
                chat_id=chat,
                action="typing",
                message_thread_id=self.thread_for(chat, state.thread),
            )
        except TelegramError as e:
            log.warning("sendChatAction failed: %s", safe(e))
        started = time.monotonic()
        with DATA_LOCK:
            self.repo.pull()
            self.reload_if_pulled()
        s = self.settings
        now = self.now()
        today = now.astimezone(self.zone()).date()
        scope = self.scope_of(chat)
        lines, atts = self.fetch_files(lines, chat)
        state.ids = [x["id"] for x in lines]
        if not lines:
            return
        private = bool(lines[0].get("private"))
        askers = list(dict.fromkeys(x.get("pid") for x in lines if x.get("pid")))
        asker = askers[0] if len(askers) == 1 else None
        with self._conv_lock:
            resets = self._resets[chat]
            conversation = self.kv.get(f"conv:{chat}")
        now_ts = int(now.timestamp())
        last_row = self.kv.chat_last(chat)
        seen = self.context_rows(chat, now_ts, last_row)
        try:
            with DATA_LOCK:
                people = records.load_people(self.data, scope)
                settings_text = (self.data / SETTINGS_FILE).read_text(encoding="utf-8")
                files = {"data/settings.yaml": records.settings_text_for_turn(settings_text)}
                for pid in scope:
                    for rel, text in records.person_texts(self.data, pid).items():
                        files[f"data/{rel}"] = text
                head = self.repo.head()
            fetched = self.sources.fetch_all(s.sources, scope)
            context = assistant.context(
                people,
                {pid: p.name for pid, p in s.people.items()},
                now,
                tz=s.timezone,
                t=self.t,
                reminders=self.kv.reminders(chat),
                sources=[assistant.SourceState(f.key, f.fetched, f.stale) for f in fetched],
                head=head,
                pull_ok=self.repo.pull_ok,
            )
            instructions = "\n".join(
                x for x in (*s.rules, str(block_cfg.get("instructions") or "")) if x.strip()
            )
            rules = assistant.rules(
                {pid: p.name for pid, p in s.people.items()}, s.language, instructions
            )
            turn = assistant.turn(
                conversation,
                rules,
                context,
                self.new_lines(lines, seen),
                self.history(
                    chat, now_ts, last_row, {x.get("row") for x in lines} | {r["id"] for r in seen}
                ),
                scope=scope,
                asker=asker,
                files=files,
                sources={f.key: f.text for f in fetched if f.text is not None},
                attachments=[
                    {"name": a.name, "mime": a.mime, "data": a.data, "from": a.who} for a in atts
                ],
                calendars={pid: s.people[pid].calendar for pid in scope if s.people[pid].calendar},
            )
            turn["incoming"] = self._incoming(state, key)
            if assistant.asks(self.data, block_cfg.get("plugin")):
                turn["ask"] = self._asker(state)
            reply = assistant.converse(
                self.data, block_cfg.get("plugin"), turn, self.plugin_settings()
            )
        except Exception as e:  # noqa: BLE001 - the plugin may raise anything
            log.warning("ai turn failed: %s", safe(e))
            self.post(chat, self.t("ai_down"), thread=state.thread)
            return
        finally:
            with self._q:
                state.closed = True
        if not reply.kept:
            log.warning("ai turn not kept")
            self.post(chat, self.t("ai_down"), thread=state.thread)
            return
        log.info("ai turn ok %ds", round(time.monotonic() - started))
        self.last_ai = self.now()
        with self._conv_lock:
            if self._resets[chat] != resets:
                log.info("conversation dropped by /nuevo during the turn")
            elif reply.conversation:
                self.kv.set(f"conv:{chat}", reply.conversation)
            else:
                self.kv.delete(f"conv:{chat}")
        handed = [
            x
            for x in self.kv.get_json(key, []) or []
            if x.get("id") in state.pulled and x.get("id") in reply.covers
        ]
        covered = [*lines, *handed]
        reply_to = _mid(covered[0]) if len(covered) == 1 else None
        text, blocks = assistant.extract(reply.text)
        text, settings_blocks = assistant.extract(text, "settings")
        text, edit_blocks = assistant.extract(text, "edit")
        text, offered = assistant.extract_options(text)
        text, listed = assistant.extract_reminders(text)
        offered = reply.options or offered
        listed = reply.reminders or listed
        users = _users(covered)
        if text and not self.say(chat, text, reply_to, offered, users, state.thread):
            self.post(chat, self.t("ai_down"), thread=state.thread)
            return
        if text:
            self.kv.add_chat(chat, "assistant", None, text, int(self.now().timestamp()))
        attachments = {a.name: a for a in atts}
        for block in blocks:
            self.apply_block(chat, block, scope, asker, private, attachments, today, state.thread)
        admin = all(x.get("admin") for x in covered)
        if settings_blocks:
            self.apply_settings(chat, settings_blocks[-1], admin, state.thread, _users(covered))
        if edit_blocks:
            self.apply_edits(
                chat, edit_blocks, scope, asker, private, admin, _users(covered), state.thread
            )
        if text:
            self.remember(chat, covered, text)
        taken = {x["id"] for x in covered}
        self.kv.update_json(key, lambda cur: [x for x in cur or [] if x["id"] not in taken] or None)
        if seen:
            self.kv.set(f"ctx:{chat}", str(max(r["id"] for r in seen)))
        if listed:
            self.offer_reminders(chat, listed, covered, reply_to, scope, state.thread)
        missed = len(state.pulled) - len(handed)
        if handed or missed:
            log.info("handed over: covered=%d next_turn=%d", len(handed), missed)
        left = [x for x in self.kv.get_json(key, []) or [] if "content" in x]
        if any(x.get("id") not in taken for x in left):
            # a line this answer did not take (handed over but not covered, or a file the budget
            # held back) gets a turn of its own now, not when someone happens to write again
            self.worker.submit(chat)
            log.info("pending lines left after a turn: next turn queued")
        if reply.later is not None:
            job = reply.later
            thread = state.thread
            self.later.submit(lambda: self.run_later(chat, job, thread))
            log.info("later queued")

    def _incoming(self, state: Turn, key: str) -> Callable[[], list[dict]]:
        """`turn["incoming"]`: the chat's text lines queued since the turn started and not
        handed over yet. A line with a photo or a file waits for the next turn, which carries
        its bytes."""

        def incoming() -> list[dict]:
            with self._q:
                if state.closed:
                    return []
                got = [
                    x
                    for x in self.kv.get_json(key, []) or []
                    if "content" in x and not x.get("file") and not state.has(x.get("id"))
                ]
                state.pulled.extend(x["id"] for x in got)
                state.grants += [
                    _grant(x["grant"]) for x in got if isinstance(x.get("grant"), dict)
                ]
                for uid in _users(got):
                    if uid not in state.users:
                        state.users.append(uid)
            return [
                {
                    "id": x["id"],
                    "role": "user",
                    "content": x["content"],
                    "ts": datetime.fromtimestamp(int(x.get("ts") or 0), UTC).isoformat(),
                }
                for x in got
            ]

        return incoming

    def context_rows(self, chat: int, now: int, upto: int) -> list[dict]:
        after = int(self.kv.get(f"ctx:{chat}") or 0)
        rows = self.kv.chat_rows(chat, now - PENDING_MAX_AGE_S, upto)
        return [r for r in rows if not r["asked"] and r["role"] == "user" and r["id"] > after][
            -CONTEXT_MAX:
        ]

    @staticmethod
    def new_lines(lines: Sequence[Mapping], seen: Sequence[Mapping]) -> list[dict]:
        items = [(int(r["ts"]), 0, i, NOT_TO_YOU + r["text"]) for i, r in enumerate(seen)]
        items += [(int(x.get("ts") or 0), 1, i, x["content"]) for i, x in enumerate(lines)]
        return [{"content": c, "ts": _iso(ts)} for ts, _, _, c in sorted(items)]

    def history(self, chat: int, now: int, upto: int, skip: set) -> list[dict]:
        days = self.settings.days
        if days <= 0:
            return []
        out = []
        for r in self.kv.chat_rows(chat, now - days * 86400, upto):
            if r["id"] in skip:
                continue
            text = r["text"] if r["role"] == "assistant" or r["asked"] else NOT_TO_YOU + r["text"]
            role = "assistant" if r["role"] == "assistant" else "user"
            out.append({"role": role, "content": text, "ts": _iso(r["ts"])})
        return out

    def remember(self, chat: int, covered: Sequence[Mapping], answer: str) -> None:
        now = int(self.now().timestamp())
        said = {
            str(_mid(x)): {"body": x["body"], "answer": answer[:ANSWER_CLIP], "ts": now}
            for x in covered
            if _mid(x) is not None and isinstance(x.get("body"), str) and not x.get("tap")
        }
        if not said:
            return

        def add(cur):
            merged = {**(cur or {}), **said}
            fresh = [
                kv for kv in merged.items() if now - int(kv[1].get("ts", 0)) <= PENDING_MAX_AGE_S
            ]
            fresh.sort(key=lambda kv: kv[1].get("ts", 0))
            return dict(fresh[-SAID_MAX:]) or None

        self.kv.update_json(f"said:{chat}", add, {})

    def run_later(self, chat: int, job: Callable[[], str], thread: int | None = None) -> None:
        started = time.monotonic()
        try:
            out = job()
            if not isinstance(out, str):
                raise assistant.PluginError("bad_later")
        except BaseException as e:  # noqa: BLE001 - the plugin may raise anything
            log.warning("later failed: %s", safe(e))
            self.post(chat, self.t("later_failed"), thread=thread)
            return
        log.info("later done %ds", round(time.monotonic() - started))
        if out.strip() and self.say(chat, out.strip(), None, thread=thread):
            self.kv.add_chat(chat, "assistant", None, out.strip(), int(self.now().timestamp()))

    # --- records -------------------------------------------------------------------------

    def apply_block(
        self,
        chat: int,
        block: object,
        scope: Sequence[str],
        asker: str | None,
        private: bool,
        attachments: Mapping[str, Attachment],
        today: date,
        thread: int | None = None,
    ) -> bool:
        """Validate one `record` block and commit it, all or nothing, with the exports; then
        «Anotado:» built from what was saved (none when nothing changed), with the calendar
        offer for each new appointment."""
        try:
            if block is assistant.BAD_BLOCK:
                raise BlockError("bad_json")
            with DATA_LOCK:
                self.repo.pull()
                people = records.load_people(self.data, scope)
                applied = records.apply_record(
                    block,
                    people=people,
                    scope=scope,
                    asker=asker,
                    private=private,
                    attachments=attachments,
                    today=today,
                    read=self.repo._read,
                )
                if not applied:
                    return True
                sha = self.commit_record(applied)
        except BlockError as e:
            log.warning("block refused: %s", e.code)
            self.post(chat, self.t("invalid_block"), thread=thread)
            return False
        except (GitError, ValueError, OSError) as e:
            log.warning("block failed: %s", safe(e))
            self.post(chat, self.t("record_failed"), thread=thread)
            return False
        lines = self.recorded_lines(applied)
        p = self.settings.people.get(applied.person)
        many = len(self.settings.people) > 1 and not private
        head = (
            self.t("recorded_for", name=html.escape(p.name)) if many and p else self.t("recorded")
        )
        text = "\n".join([head, *lines])
        offer = self.appt_offer(applied, chat)
        if offer:
            text += "\n\n" + self.t("appt_offer", m=self.mention(p))
        markup = _keyboard(self.appt_rows(offer)) if offer else None
        sent = self.post(chat, text, markup, thread=thread)
        if sha:
            said = "; ".join(re.sub(r"<[^>]+>", "", x).lstrip("• ") for x in lines)
            entry = {"sha": sha, "person": applied.person, "said": html.unescape(said)[:300]}
            self.kv.update_json(f"undo:{chat}", lambda cur: [*(cur or []), entry][-UNDO_KEEP:], [])
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        self.kv.add_chat(chat, "assistant", None, plain, int(self.now().timestamp()))
        if offer and sent:
            offer["msg"] = sent.get("message_id")
            offer["thread"] = thread
            self.keep_offer(chat, offer)
            log.info("appointments offered n=%d", len(offer["items"]))
        log.info("record written items=%d", len(applied.items))
        return True

    def commit_record(self, applied: records.Applied) -> str | None:
        """The block's files and the exports regenerated from the data after it, in one commit
        `log(<person>): <short list>`, pushed."""
        message = f"{LOG_PREFIX}{applied.person}): " + "; ".join(applied.summary)
        files = dict(applied.files)
        with DATA_LOCK:
            files.update(self.export_files({applied.person: applied.data}))
            return self.repo.commit(files, message[:200])

    def recorded_lines(self, applied: records.Applied) -> list[str]:
        """«Anotado:» lines from what was saved, never from the assistant's prose."""
        t, out = self.t, []
        for kind, item in applied.items:
            if kind == "appt_add":
                out.append(t("line_appt_add", appt=self.appt_text(item)))
                for prep in item.get("prep") or []:
                    out.append(
                        "   "
                        + t(
                            "prep_line",
                            text=html.escape(str(prep["text"])),
                            hours=prep["hours_before"],
                        )
                    )
            elif kind == "appt_change":
                before, after = item["before"], item["after"]
                moved = {k for k in after if after.get(k) != before.get(k)}
                if moved == {"prep"}:  # only the preparation: the appointment itself stays
                    d = records.as_date(after.get("date"))
                    out.append(
                        t(
                            "line_appt_prep",
                            what=html.escape(str(after.get("what") or "")),
                            date=t.date(d) if d else "?",
                        )
                    )
                    for prep in after.get("prep") or []:
                        out.append(
                            "   "
                            + t(
                                "prep_line",
                                text=html.escape(str(prep["text"])),
                                hours=prep["hours_before"],
                            )
                        )
                else:
                    out.append(t("line_appt_change", appt=self.appt_text(after)))
            elif kind == "appt_cancel":
                out.append(t("line_appt_cancel", appt=self.appt_text(item)))
            elif kind == "appt_done":
                out.append(t("line_appt_done", appt=self.appt_text(item)))
            elif kind in ("med_add", "med_change", "med_stop"):
                m = item["after"] if kind == "med_change" else item
                what = " ".join(str(x) for x in (m.get("name"), m.get("dose")) if x)
                if m.get("slot"):
                    what += f", {t.slot(m['slot']).lower()}"
                label = t(f"kind_{m.get('kind')}")
                if kind == "med_stop":
                    until = records.as_date(m.get("until"))
                    out.append(
                        t(
                            "line_med_stop",
                            kind=label,
                            what=html.escape(what),
                            date=t.date(until, "short") if until else "",
                        )
                    )
                else:
                    out.append(t(f"line_{kind}", kind=label, what=html.escape(what)))
            elif kind == "labs":
                values = item["values"]
                shown = []
                for v in values[:8]:
                    piece = f"{v['test']} {v['value']}"
                    if v.get("unit"):
                        piece += f" {v['unit']}"
                    shown.append(html.escape(piece))
                dates = sorted({str(v["date"]) for v in values})
                d = records.as_date(dates[-1])
                more = t("labs_more", n=len(values) - 8) if len(values) > 8 else ""
                out.append(
                    t("line_labs", date=t.date(d, "short") if d else "", values=", ".join(shown))
                    + more
                )
            elif kind == "doc":
                out.append(
                    t(
                        "line_doc",
                        title=html.escape(item["title"]),
                        date=t.date(item["date"], "short"),
                    )
                )
            elif kind in ("profile_add", "profile_remove"):
                value = item["value"]
                if isinstance(value, Mapping):
                    value = value.get("what") or value.get("name") or value.get("id") or ""
                out.append(
                    t(
                        f"line_{kind}",
                        what=t(f"profile_{item['key']}"),
                        value=html.escape(str(value)),
                    )
                )
        return out

    # --- the calendar offer of a new appointment -----------------------------------------

    def appt_offer(self, applied: records.Applied, chat: int) -> dict | None:
        """«En el calendario» and «No» under «Anotado:» for each appointment the block added,
        for its person only, for 12 hours: with `calendar.appointments`, a plugin that can ask
        and a calendar for that person."""
        p = self.settings.people.get(applied.person)
        if (
            not applied.added
            or self.settings.calendar_appointments is not True
            or p is None
            or not p.calendar
            or p.user_id is None
            or not assistant.asks(self.data, self.settings.plugin())
        ):
            return None
        items = []
        for a in applied.added:
            items.append(
                {
                    "id": a["id"],
                    "date": str(records.as_date(a["date"])),
                    "time": records.clock_text(a.get("time")),
                    "what": a["what"],
                    "place": a.get("place"),
                    "prep": a.get("prep") or [],
                    "done": None,
                }
            )
        return {
            "key": secrets.token_hex(4),
            "kind": "appt",
            "person": p.id,
            "uid": p.user_id,
            "for": [p.user_id],
            "items": items,
            "ts": self.now().timestamp(),
            "ttl": APPT_TTL_S,
        }

    def appt_rows(self, offer: Mapping) -> list:
        t, key, items = self.t, offer["key"], offer["items"]
        rows = []
        for n, item in enumerate(items):
            pre = f"{n + 1} · " if len(items) > 1 else ""
            done = item.get("done")
            if done:
                label = {"calendar": "appt_calendar", "no": "button_no"}.get(done, OFFER_GONE)
                rows.append([(f"✓ {pre}{t(label)}", "x")])
                continue
            rows.append(
                [
                    (pre + t("appt_calendar"), f"c:{key}:{n}y"),
                    (pre + t("button_no"), f"c:{key}:{n}n"),
                ]
            )
        return rows

    def appt_event(self, item: Mapping) -> tuple[dict, str]:
        """The grant a calendar tap gives, and the words of the message it sends: one event
        titled «Cita: <what>» at the appointment's time (an all-day event without one)."""
        title = self.t("event_title", what=item["what"])
        d = date.fromisoformat(item["date"])
        if item.get("time"):
            at = datetime.combine(d, records.hhmm(item["time"]), tzinfo=self.zone())
            grant = {"at": at.timestamp()}
            when = self.t("at_time", date=self.t.date(d), time=hm(item["time"]))
            iso = at.isoformat()
        else:
            grant = {
                "events": [
                    {
                        "title": title,
                        "start": d.isoformat(),
                        "end": (d + timedelta(days=1)).isoformat(),
                    }
                ]
            }
            when, iso = self.t.date(d), d.isoformat()
        prep = "; ".join(
            str(p.get("text")) for p in item.get("prep") or [] if isinstance(p, Mapping)
        )
        text = self.t(
            "appt_calendar_turn",
            title=title,
            when=when,
            iso=iso,
            place=item.get("place") or "-",
            prep=prep or "-",
        )
        return grant, text

    def tapped_appt(self, cq: Mapping, chat: int, data: str, user: object) -> None:
        _, key, choice = [*data.split(":"), "", ""][:3]
        mid = (cq.get("message") or {}).get("message_id")
        offer = self.find_offer(chat, key)
        if offer is None:
            self.answer(cq["id"], self.t("tap_gone"))
            self.unbutton(chat, mid)
            return
        if user not in offer.get("for", []):
            self.answer(cq["id"], self.t("tap_not_yours", name=self.plain_names(offer["for"])))
            return
        n, c = choice[:-1], choice[-1:]
        items = offer["items"]
        if not n.isdigit() or int(n) >= len(items) or c not in ("y", "n"):
            self.answer(cq["id"])
            return
        item = items[int(n)]
        if item.get("done"):
            self.answer(cq["id"], self.t("tap_gone"))
            return
        item["done"] = "calendar" if c == "y" else "no"
        self.keep_offer(chat, offer)
        self.answer(cq["id"])
        self.rekey(chat, offer.get("msg"), self.appt_rows(offer))
        if c == "n":
            log.info("appointment calendar declined")
            return
        grant, text = self.appt_event(item)
        grant.update({"by": user, "owner": offer.get("person")})
        self.queue_tap(chat, mid, user, text, grant, offer.get("thread"))
        log.info("appointment to the calendar")

    # --- the reminders the app sends by itself -------------------------------------------

    def tick_appointments(self, now: datetime) -> None:
        """Post every appointment reminder that is due and not posted yet: the evening before,
        each preparation, and the ask afterwards."""
        s = self.settings
        if not s.people:
            return
        try:
            people = self.everyone()
        except ValueError as e:
            log.warning("appointments unreadable: %s", safe(e))
            return
        quiet = (records.hhmm(s.quiet[0]), records.hhmm(s.quiet[1]))
        for d in schedule.due(
            people,
            now,
            s.timezone,
            day_before_at=records.hhmm(s.day_before_at),
            followup=s.followup is True,
            quiet=quiet,
        ):
            if self.kv.get(d.key) == d.stamp:
                continue
            p = s.people.get(d.person)
            if p is None:
                continue
            if self.post_due(d, p) is not None:
                self.kv.set(d.key, d.stamp)

    def due_chat(self, appt: Mapping, p: Person) -> int | None:
        if appt.get("chat") == "private":
            return p.user_id
        return self.settings.chat

    def post_due(self, d: schedule.Due, p: Person) -> object | None:
        """One app reminder. Returns None when Telegram could not take it now (tried again on the
        next tick); a chat Telegram refuses for good counts as posted, so it is not retried."""
        chat = self.due_chat(d.appt, p)
        if chat is None:
            return None
        t, a = self.t, d.appt
        m = self.mention(p)
        day = records.as_date(a.get("date"))
        when = hm(records.clock_text(a.get("time")))
        what = html.escape(str(a.get("what") or ""))
        place = html.escape(str(a.get("place") or ""))
        rel = self.relative_day(day)
        markup = None
        offer = None
        if d.kind == "day_before":
            key = "remind_day_before" if when else "remind_day_before_untimed"
            text = t(key, m=m, time=when or "", what=what)
            if place:
                text = text.rstrip(".") + t("remind_place", place=place) + "."
            for prep in a.get("prep") or []:
                if isinstance(prep, Mapping):
                    text += "\n" + t(
                        "prep_line",
                        text=html.escape(str(prep.get("text"))),
                        hours=prep.get("hours_before"),
                    )
        elif d.kind.startswith("prep-"):
            prep_text = html.escape(lower_first(str((d.prep or {}).get("text") or "")))
            if d.early:
                begins = records.appt_start(a, self.settings.timezone)
                frm = records.before(begins, int(d.prep["hours_before"]))
                text = t(
                    "remind_prep_from",
                    m=m,
                    start=hm(f"{frm.astimezone(self.zone()):%H:%M}"),
                    text=prep_text,
                    what=what,
                    day=rel,
                    time=when,
                )
            else:
                text = t("remind_prep", m=m, text=prep_text, what=what, day=rel, time=when)
        else:
            offer = {
                "key": secrets.token_hex(4),
                "kind": "followup",
                "person": p.id,
                "appt": a.get("id"),
                "for": [p.user_id],
                "items": [{"done": None}],
                "ts": self.now().timestamp(),
                "ttl": FOLLOWUP_TTL_S,
            }
            text = t("followup", m=m, what=what)
            markup = _keyboard(self.followup_rows(offer))
        try:
            sent = self.send(chat, text, markup)
        except TelegramError as e:
            log.warning("appointment reminder not posted: %s", safe(e))
            return True if e.code in (400, 403) else None
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        self.kv.add_chat(chat, "assistant", None, plain, int(self.now().timestamp()))
        if offer is not None:
            offer["msg"] = (sent or {}).get("message_id")
            offer["thread"] = self.thread_for(chat)
            self.keep_offer(chat, offer)
        log.info("appointment reminder posted: %s", d.kind.split("-")[0])
        return sent or True

    def relative_day(self, d: date | None) -> str:
        if d is None:
            return ""
        today = self.today()
        if d == today:
            return self.t("today")
        if d == today + timedelta(days=1):
            return self.t("tomorrow")
        return self.t("on_date", date=self.t.date(d))

    def followup_rows(self, offer: Mapping) -> list:
        done = offer["items"][0].get("done")
        if done:
            label = {"yes": "followup_yes", "no": "button_no"}.get(done, OFFER_GONE)
            return [[("✓ " + self.t(label), "x")]]
        key = offer["key"]
        return [[(self.t("followup_yes"), f"f:{key}:y"), (self.t("button_no"), f"f:{key}:n")]]

    def tapped_followup(self, cq: Mapping, chat: int, data: str, user: object) -> None:
        """«Sí, te cuento»: a prompt that opens the person's reply box. «No»: the appointment is
        marked done, committed like any record."""
        _, key, choice = [*data.split(":"), "", ""][:3]
        mid = (cq.get("message") or {}).get("message_id")
        offer = self.find_offer(chat, key)
        if offer is None:
            self.answer(cq["id"], self.t("tap_gone"))
            self.unbutton(chat, mid)
            return
        if user not in offer.get("for", []):
            self.answer(cq["id"], self.t("tap_not_yours", name=self.plain_names(offer["for"])))
            return
        if choice not in ("y", "n") or offer["items"][0].get("done"):
            self.answer(cq["id"], None if choice in ("y", "n") else self.t("tap_gone"))
            return
        offer["items"][0]["done"] = "yes" if choice == "y" else "no"
        self.keep_offer(chat, offer)
        self.answer(cq["id"])
        self.rekey(chat, offer.get("msg"), self.followup_rows(offer))
        p = self.settings.people.get(str(offer.get("person")))
        if p is None:
            return
        if choice == "y":
            markup = {"force_reply": True, "selective": True}
            self.post(chat, self.t("tell_me", m=self.mention(p)), markup, offer.get("thread"))
            log.info("followup: tell")
            return
        block = {"person": p.id, "appointments": [{"op": "done", "id": offer.get("appt")}]}
        private = chat != self.settings.chat
        try:
            with DATA_LOCK:
                self.repo.pull()
                people = records.load_people(self.data, [p.id])
                applied = records.apply_record(
                    block,
                    people=people,
                    scope=[p.id],
                    asker=p.id,
                    private=private,
                    attachments={},
                    today=self.today(),
                )
                if applied:
                    self.commit_record(applied)
        except (BlockError, GitError, ValueError, OSError) as e:
            log.warning("followup done failed: %s", safe(e))
            self.post(chat, self.t("record_failed"), thread=offer.get("thread"))
            return
        self.kv.add_chat(
            chat, "assistant", None, self.t("followup_no_log"), int(self.now().timestamp())
        )
        log.info("followup: done")

    # --- reminders offered with buttons --------------------------------------------------

    def reminder_when(self, at: float) -> str:
        local = datetime.fromtimestamp(at, self.zone())
        return self.t("at_time", date=self.t.date(local.date()), time=hm(f"{local:%H:%M}"))

    def offers(self, chat: int) -> list[dict]:
        return [o for o in self.kv.get_json(f"remind:{chat}", []) or [] if isinstance(o, dict)]

    def find_offer(self, chat: int, key: str) -> dict | None:
        offer = next((o for o in self.offers(chat) if o.get("key") == key), None)
        if offer is None:
            return None
        if self.now().timestamp() - offer.get("ts", 0) > offer.get("ttl", REMIND_TTL_S):
            return None
        return offer

    def keep_offer(self, chat: int, offer: dict) -> None:
        key = offer["key"]
        waiting = any(not i.get("done") for i in offer.get("items") or [])

        def put(cur):
            rest = [o for o in cur or [] if o.get("key") != key]
            return [*rest, offer] if waiting else rest or None

        self.kv.update_json(f"remind:{chat}", put, [])

    def remind_rows(self, offer: Mapping) -> list:
        t, key, items = self.t, offer["key"], offer["items"]
        calendar = self.settings.calendar_reminders is True and bool(offer.get("calendar"))
        rows = []
        for n, r in enumerate(items):
            pre = f"{n + 1} · " if len(items) > 1 else ""
            if r.get("done"):
                rows.append([(f"✓ {pre}{t('remind_' + r['done'])}", "x")])
                continue
            first = [(pre + t("remind_here"), f"r:{key}:{n}h")]
            if calendar:
                first.append((pre + t("remind_calendar"), f"r:{key}:{n}c"))
            rows += [first, [(pre + t("remind_nothing"), f"r:{key}:{n}x")]]
        return rows

    def rows_of(self, offer: Mapping) -> list:
        kind = offer.get("kind")
        if kind == "appt":
            return self.appt_rows(offer)
        if kind == "followup":
            return self.followup_rows(offer)
        if kind == "instr":
            return self.instr_rows(offer)
        if kind == "edit":
            return self.edit_rows(offer)
        return self.remind_rows(offer)

    def offer_reminders(
        self,
        chat: int,
        listed: Sequence[Mapping],
        covered: Sequence[Mapping],
        reply_to,
        scope: Sequence[str],
        thread: int | None = None,
    ) -> None:
        """One message per person with the reminders an answer listed for them, buttons for
        each. A reminder is for the person the answer was for, or the one it names (someone in
        the turn's scope); with several people and none named, it is skipped."""
        now = self.now().timestamp()
        users = _users(covered)
        default = self.person(users[0]) if len(users) == 1 else None
        by_person: dict[str, list] = {}
        for r in listed:
            p = self.settings.people.get(r.get("person", "")) if r.get("person") else default
            if p is None or p.id not in scope or p.user_id is None:
                continue
            try:
                at = datetime.strptime(r["when"], "%Y-%m-%d %H:%M").replace(tzinfo=self.zone())
            except (KeyError, TypeError, ValueError):
                continue
            if now + 60 <= at.timestamp() <= now + REMIND_DAYS * 86400:
                by_person.setdefault(p.id, []).append(
                    {"at": at.timestamp(), "text": r["text"], "done": None}
                )
        for pid, items in by_person.items():
            p = self.settings.people[pid]
            many = len(items) > 1
            lines = "\n".join(
                (f"{n + 1}. " if many else "")
                + f"{self.reminder_when(r['at'])}: {html.escape(r['text'])}"
                for n, r in enumerate(items)
            )
            offer = {
                "key": secrets.token_hex(4),
                "kind": "remind",
                "person": pid,
                "uid": p.user_id,
                "for": [p.user_id],
                "calendar": bool(p.calendar),
                "items": items,
                "ts": now,
                "ttl": REMIND_TTL_S,
                "thread": thread,
            }
            where = (
                {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None
            )
            text = self.t("remind_offer", name=self.mention(p), lines=lines)
            sent = self.post(
                chat, text, _keyboard(self.remind_rows(offer)), thread, reply_parameters=where
            )
            if sent is None:
                continue
            offer["msg"] = sent.get("message_id")
            self.keep_offer(chat, offer)
            log.info("reminders offered n=%d", len(items))

    def tapped_remind(self, cq: Mapping, chat: int, data: str, user: object) -> None:
        _, key, choice = [*data.split(":"), "", ""][:3]
        mid = (cq.get("message") or {}).get("message_id")
        offer = self.find_offer(chat, key)
        if offer is None:
            self.answer(cq["id"], self.t("tap_gone"))
            self.unbutton(chat, mid)
            return
        if user not in offer.get("for", []):
            self.answer(cq["id"], self.t("tap_not_yours", name=self.plain_names(offer["for"])))
            return
        n, c = choice[:-1], choice[-1:]
        items = offer["items"]
        calendar = self.settings.calendar_reminders is True and bool(offer.get("calendar"))
        if (
            not n.isdigit()
            or int(n) >= len(items)
            or c not in ("h", "c", "x")
            or (c == "c" and not calendar)
        ):
            self.answer(cq["id"])
            return
        item = items[int(n)]
        if item.get("done"):
            self.answer(cq["id"], self.t("tap_gone"))
            return
        item["done"] = {"h": "here", "c": "calendar", "x": "nothing"}[c]
        self.keep_offer(chat, offer)
        when = self.reminder_when(item["at"])
        self.answer(cq["id"], self.t("remind_set", when=when) if c == "h" else None)
        self.rekey(chat, offer.get("msg"), self.remind_rows(offer))
        now = int(self.now().timestamp())
        if c == "h":
            self.kv.add_reminder(
                chat, offer.get("uid"), item["at"], item["text"], user, now, offer.get("thread")
            )
            said = self.t("remind_set_log", when=when, text=item["text"])
            self.kv.add_chat(chat, "assistant", None, said, now)
            log.info("reminder set")
        elif c == "c":
            local = datetime.fromtimestamp(item["at"], self.zone())
            asked = self.t(
                "remind_calendar_turn", when=when, iso=local.isoformat(), text=item["text"]
            )
            grant = {"by": user, "at": item["at"], "owner": offer.get("person")}
            self.queue_tap(chat, mid, user, asked, grant, offer.get("thread"))
            log.info("reminder to the calendar")

    def queue_tap(
        self, chat: int, mid: object, user: object, text: str, grant: dict, thread=None
    ) -> None:
        """A calendar button's tap as that person's message, carrying what it approves."""
        p = self.person(user)
        if p is None:
            return
        now = int(self.now().timestamp())
        content = f"{p.name}: {text}"
        self.queue_line(
            chat,
            {
                "id": f"g{secrets.token_hex(4)}",
                "mid": mid,
                "ts": now,
                "content": content,
                "admin": self.settings.is_admin(p.id),
                "uid": user,
                "pid": p.id,
                "tap": True,
                "grant": grant,
                "thread": thread,
                "private": chat != self.settings.chat,
                "row": self.kv.add_chat(chat, "user", p.name, content, now),
            },
        )

    def expire_offers(self) -> None:
        now = self.now().timestamp()
        for key in self.kv.keys("remind:"):
            chat = int(key.removeprefix("remind:"))
            gone: list[dict] = []

            def keep(cur, gone=gone):
                fresh = []
                for o in cur or []:
                    late = now - o.get("ts", 0) > o.get("ttl", REMIND_TTL_S)
                    (gone if late else fresh).append(o)
                return fresh or None

            self.kv.update_json(key, keep, [])
            for o in gone:
                for r in o.get("items") or []:
                    r["done"] = r.get("done") or "timeout"
                self.rekey(chat, o.get("msg"), self.rows_of(o))

    def tick_reminders(self) -> None:
        now = self.now().timestamp()
        for r in self.kv.due_reminders(now):
            name = self.mention_uid(r["uid"])
            body = html.escape(r["text"])
            text = f"⏰ {name}: {body}" if name else f"⏰ {body}"
            if now - r["at"] > LATE_S:
                text += "\n" + self.t("reminder_late", when=self.reminder_when(r["at"]))
            try:
                self.send(r["chat"], text, thread=r.get("thread"))
            except TelegramError as e:
                log.warning("reminder not posted: %s", safe(e))
                if e.code in (400, 403):
                    self.kv.settle_reminder(r["id"], "failed")
                continue
            self.kv.settle_reminder(r["id"], "sent")
            plain = html.unescape(re.sub(r"<[^>]+>", "", text))
            self.kv.add_chat(r["chat"], "assistant", None, plain, int(self.now().timestamp()))
            log.info("reminder posted")

    def tapped_cancel(self, cq: Mapping, chat: int, data: str, user: object) -> None:
        _, rid, n = [*data.split(":"), "", ""][:3]
        message = cq.get("message") or {}
        found = [r for r in self.kv.reminders(chat) if str(r["id"]) == rid]
        if not found or not self.kv.settle_reminder(found[0]["id"], "cancelled"):
            self.answer(cq["id"], self.t("tap_gone"))
            return
        r = found[0]
        self.answer(cq["id"], self.t("reminder_cancelled"))
        said = self.t("reminder_cancelled_log", when=self.reminder_when(r["at"]), text=r["text"])
        self.kv.add_chat(chat, "assistant", None, said, int(self.now().timestamp()))
        rows = [
            [
                (f"✓ {self.t('reminders_cancelled', n=n)}", "x")
                if b.get("callback_data") == data
                else (b.get("text", ""), b.get("callback_data", "x"))
                for b in row
            ]
            for row in (message.get("reply_markup") or {}).get("inline_keyboard") or []
        ]
        self.rekey(chat, message.get("message_id"), rows)
        log.info("reminder cancelled")

    # --- asks: an action the assistant wants to take, approved by a person ----------------

    def _asker(self, state: Turn) -> Callable[[object], bool]:
        def ask(request: object) -> bool:
            return self.pose(state, request)

        return ask

    def approvers(self, state: Turn, request: Mapping) -> list[int]:
        """Who may answer an ask. With `owner` (a write to that person's calendar): that person
        alone, never the asker or the admins in their place. Else the person the turn answers,
        when the plugin asks the asker and the turn carries one person's messages; else the
        admins."""
        if "owner" in request:
            p = self.settings.people.get(str(request.get("owner")))
            return [p.user_id] if p is not None and p.user_id is not None else []
        users = list(state.users)
        if request.get("confirm_by") == "asker" and len(users) == 1 and self.person(users[0]):
            return users
        return self.admin_uids()

    def names(self, users: Sequence[int]) -> str:
        return ", ".join(x for x in (self.mention_uid(u) for u in users) if x)

    def plain_names(self, users: Sequence[int]) -> str:
        return ", ".join(x for x in (self.name_of(u) for u in users) if x)

    def pose(self, state: Turn, request: object) -> bool:
        if not isinstance(request, Mapping) or not isinstance(request.get("id"), str):
            return True
        request = dict(request)
        summary = str(request.get("summary") or "").strip()
        with self._q:
            if any(a["request"].get("id") == request["id"] for a in self.asks.values()):
                return True
        expires = request.get("expires_at")
        ok_end = isinstance(expires, int | float) and not isinstance(expires, bool)
        expires = float(expires) if ok_end else time.time() + ASK_DEFAULT_S
        if not state.closed and summary and expires > time.time():
            grant = self.granted(state, request)
            if grant is not None:
                self.preapproved(state, request, grant, summary)
                return True
        by = self.approvers(state, request)
        if state.closed or not by or not summary or expires <= time.time():
            self.decline(request, NO_ONE)
            log.info("ask declined: nobody to ask")
            return True
        key = secrets.token_hex(4)
        text = self.t("ask_confirm", name=self.names(by), summary=html.escape(summary, quote=False))
        markup = _keyboard(
            [[(self.t("button_approve"), f"a:{key}:y"), (self.t("button_reject"), f"a:{key}:n")]]
        )
        where = (
            {"message_id": state.mid, "allow_sending_without_reply": True} if state.mid else None
        )
        try:
            sent = self.send(state.chat, text, markup, state.thread, reply_parameters=where)
        except TelegramError as e:
            log.warning("ask not posted: %s", safe(e))
            if e.code == 0 or e.code == 429 or e.code >= 500:
                return False
            self.decline(request, NO_ONE)
            return True
        item = {
            "request": request,
            "by": by,
            "chat": state.chat,
            "thread": state.thread,
            "msg": (sent or {}).get("message_id"),
            "expires": expires,
        }
        with self._q:
            self.asks[key] = item
            state.asks.append(key)
            late = state.closed
        if late:
            self.close_asks(state)
        log.info("ask posted")
        return True

    def granted(self, state: Turn, request: Mapping) -> dict | None:
        """The grant a calendar button gave this turn for this ask, used up, or None. A grant
        that names an owner approves only an ask for that same person's calendar."""
        if request.get("action") != "add":
            return None
        when, event = request.get("when"), request.get("event")
        with self._q:
            for g in state.grants:
                if g.get("owner") is not None and request.get("owner") != g.get("owner"):
                    continue
                if "at" in g:
                    if g.get("used"):
                        continue
                    g["used"] = True
                    ok = isinstance(when, int | float) and not isinstance(when, bool)
                    return g if ok and abs(float(when) - float(g["at"])) < GRANT_SLACK_S else None
                for e in g.get("events") or []:
                    if not e.get("used") and _same_event(event, e):
                        e["used"] = True
                        return g
        return None

    def preapproved(self, state: Turn, request: Mapping, grant: Mapping, summary: str) -> None:
        block = self.settings.assistant or {}
        try:
            out = assistant.answer(
                self.data,
                block.get("plugin"),
                dict(request),
                {"approved": True, "by": str(grant.get("by")), "note": "approved by the button"},
                self.plugin_settings(),
            )
        except Exception as e:  # noqa: BLE001 - the plugin may raise anything
            log.warning("a preapproved ask failed: %s", safe(e))
            out = {"ok": False, "error": None}
        said = self.outcome(out)
        where = (
            {"message_id": state.mid, "allow_sending_without_reply": True} if state.mid else None
        )
        self.post(
            state.chat,
            self.t("ask_preapproved", what=html.escape(summary, quote=False), result=said),
            thread=state.thread,
            reply_parameters=where,
        )
        log.info("ask approved by a button")

    def outcome(self, out: Mapping) -> str:
        if out.get("ok"):
            return self.t("ask_done")
        if out.get("error"):
            return self.t("ask_failed", error=html.escape(str(out["error"])[:300], quote=False))
        return self.t("ask_failed_unknown")

    def decline(self, request: Mapping, note: str, by: str = "bot") -> None:
        block = self.settings.assistant or {}
        try:
            assistant.answer(
                self.data,
                block.get("plugin"),
                dict(request),
                {"approved": False, "by": by, "note": note},
                self.plugin_settings(),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("could not decline an ask: %s", safe(e))

    def tapped_ask(self, cq: Mapping, chat: int, data: str, user: object) -> None:
        _, key, choice = [*data.split(":"), "", ""][:3]
        mid = (cq.get("message") or {}).get("message_id")
        with self._q:
            item = self.asks.get(key)
            if item is not None and item["expires"] <= time.time():
                self.asks.pop(key, None)
                item = None
            mine = item is not None and user in item["by"]
            if mine:
                self.asks.pop(key, None)
        if item is None:
            self.answer(cq["id"], self.t("tap_gone"))
            self.unbutton(chat, mid)
            return
        if not mine:
            self.answer(cq["id"], self.t("tap_not_yours", name=self.plain_names(item["by"])))
            return
        yes = choice == "y"
        self.answer(cq["id"])
        self.unbutton(
            chat, item["msg"], "✓ " + self.t("button_approve" if yes else "button_reject")
        )
        self.side.submit(lambda: self.settle_ask(item, yes, user))
        log.info("ask answered")

    def settle_ask(self, item: Mapping, yes: bool, user: object) -> None:
        block = self.settings.assistant or {}
        try:
            out = assistant.answer(
                self.data,
                block.get("plugin"),
                dict(item["request"]),
                {"approved": yes, "by": str(user), "note": None},
                self.plugin_settings(),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("an ask's answer failed: %s", safe(e))
            out = {"ok": False, "error": None}
        if not yes:
            return
        where = {"message_id": item["msg"], "allow_sending_without_reply": True}
        self.post(
            item["chat"],
            self.outcome(out),
            thread=item.get("thread"),
            reply_parameters=where if item["msg"] else None,
        )

    def close_asks(self, state: Turn, note: str | None = None) -> None:
        with self._q:
            keys, state.asks[:] = list(state.asks), []
            items = [self.asks.pop(k) for k in keys if k in self.asks]
        for item in items:
            if note:
                self.decline(item["request"], note)
            self.unbutton(item["chat"], item["msg"])
            if note:
                where = {"message_id": item["msg"], "allow_sending_without_reply": True}
                self.post(
                    item["chat"],
                    self.t("ask_edited"),
                    thread=item.get("thread"),
                    reply_parameters=where,
                )

    def expire_asks(self) -> None:
        now = time.time()
        with self._q:
            gone = [k for k, v in self.asks.items() if v["expires"] <= now]
            items = [self.asks.pop(k) for k in gone]
        for item in items:
            self.unbutton(item["chat"], item["msg"])

    # --- choices under an answer ---------------------------------------------------------

    def yes_no(self, labels: Sequence[str]) -> bool:
        return sorted(fold(x) for x in labels) == sorted(
            fold(self.t(k)) for k in ("button_yes", "button_no")
        )

    def offer(self, chat: int, msg: object, key: str, labels: Sequence[str], users, thread=None):
        now = int(self.now().timestamp())
        item = {
            "key": key,
            "msg": msg,
            "labels": list(labels),
            "for": list(users),
            "ts": now,
            "thread": thread,
        }

        def add(cur):
            kept = [o for o in cur or [] if now - int(o.get("ts", 0)) <= OPTIONS_TTL_S]
            return [*kept, item][-OPTIONS_KEEP:]

        self.kv.update_json(f"options:{chat}", add, [])

    def tapped_option(self, cq: Mapping, chat: int, data: str, user: object) -> None:
        _, key, choice = [*data.split(":"), "", ""][:3]
        mid = (cq.get("message") or {}).get("message_id")
        now = int(self.now().timestamp())
        offers = self.kv.get_json(f"options:{chat}", []) or []
        item = next((o for o in offers if o.get("key") == key), None)
        if item is None or now - int(item.get("ts", 0)) > OPTIONS_TTL_S:
            self.answer(cq["id"], self.t("tap_gone"))
            self.unbutton(chat, mid)
            return
        if item.get("for") and user not in item["for"]:
            self.answer(cq["id"], self.t("tap_not_yours", name=self.plain_names(item["for"])))
            return
        labels = item.get("labels") or []
        if not choice.isdigit() or int(choice) >= len(labels):
            self.answer(cq["id"])
            return
        label = labels[int(choice)]
        self.kv.update_json(
            f"options:{chat}", lambda cur: [o for o in cur or [] if o.get("key") != key] or None
        )
        self.answer(cq["id"])
        self.unbutton(chat, mid, f"✓ {label}")
        p = self.person(user)
        content = f"{p.name}: {label}"
        line = {
            "id": f"o{key}",
            "mid": mid,
            "ts": now,
            "content": content,
            "admin": self.settings.is_admin(p.id),
            "uid": user,
            "pid": p.id,
            "body": label,
            "tap": True,
            "thread": item.get("thread"),
            "private": chat != self.settings.chat,
            "row": self.kv.add_chat(chat, "user", p.name, content, now),
        }
        self.queue_line(chat, line)
        log.info("option tapped")

    def expire_options(self) -> None:
        now = int(self.now().timestamp())
        for key in self.kv.keys("options:"):
            gone: list[dict] = []

            def keep(cur, gone=gone):
                fresh = []
                for o in cur or []:
                    (gone if now - int(o.get("ts", 0)) > OPTIONS_TTL_S else fresh).append(o)
                return fresh or None

            self.kv.update_json(key, keep, [])
            for o in gone:
                self.unbutton(int(key.removeprefix("options:")), o.get("msg"), self.t(OFFER_GONE))

    # --- edits ---------------------------------------------------------------------------

    def on_edit(self, msg: Mapping) -> None:
        """An edited text from a person, handled by where its line is: still queued, the new
        wording goes; in the running turn, its open asks are declined and a correction joins
        it; already answered, a new line asks to check that answer; unknown, a new message when
        it now addresses the bot. An edit is never a command, and one that changes no word does
        nothing."""
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip() or text.strip().startswith("/"):
            return
        chat = msg.get("chat") or {}
        user = (msg.get("from") or {}).get("id")
        if not self.allowed_chat(chat, user) or not self.has_assistant:
            return
        p = self.person(user)
        private = chat.get("type") == "private"
        chat_id, mid = chat["id"], msg.get("message_id")
        body = self._strip_mention(msg, text)[0].strip()
        logged = self.kv.chat_find(chat_id, mid) if isinstance(mid, int) else None
        if p is None:
            if logged is not None:
                self.relog(logged, body, self.sender(msg))
            return
        key = f"pending:{chat_id}"
        found: list[dict] = []

        def swap(cur):
            out = []
            for x in cur or []:
                if x.get("id") == mid and "content" in x and "body" in x and not x.get("file"):
                    found.append(dict(x))
                    if _words(x["body"]) != _words(body):
                        head = x["content"][: len(x["content"]) - len(x["body"])]
                        if not x["content"].endswith(x["body"]):
                            head = f"{p.name}: "
                        x = {**x, "content": head + body, "body": body}
                        x["ts"] = int(self.now().timestamp())
                out.append(x)
            return out or None

        with self._q:
            state = self._running.get(chat_id)
            running = state is not None and state.has(mid)
            if not running:
                self.kv.update_json(key, swap, [])
                if found:
                    self._newest[chat_id] = self.clock()
        if logged is not None and (found or logged["asked"]):
            self.relog(logged, body, p.name)
        if found:
            self.worker.submit(chat_id)
            log.info("edit: queued line")
            return
        line = self._base_line(msg, p)
        line.update({"mid": mid, "body": body, "id": f"e{mid}.{secrets.token_hex(3)}"})
        if running:
            old = state.edited.get(mid)
            if old is None:
                pending = self.kv.get_json(key, []) or []
                old = next((x.get("body", "") for x in pending if x.get("id") == mid), "")
            if _words(old) == _words(body):
                return
            state.edited[mid] = body
            line["content"] = (
                f"[{p.name} edited a message of this turn. Reconsider your answer with the new "
                f"wording.]\nBefore: {old}\nNow: {body}"
            )

            def correct(state=state, line=line, chat_id=chat_id, body=body):
                self.close_asks(state, f"editado: {body}")
                self.queue_line(chat_id, line)

            self.side.submit(correct)
            log.info("edit: running turn")
            return
        said = (self.kv.get_json(f"said:{chat_id}", {}) or {}).get(str(mid))
        if said is not None:
            if _words(said.get("body", "")) == _words(body):
                return
            line["content"] = (
                f"[{p.name} edited a message you already answered. Check whether your answer "
                f"still holds with the new wording, and say what changes.]\nBefore: "
                f"{said.get('body', '')}\nNow: {body}\nYour answer was: {said.get('answer', '')}"
            )
            self.queue_line(chat_id, line)
            log.info("edit: answered message")
            return
        content = self.addressed(msg, text, private, p)
        if content is not None:
            row = None
            if logged is not None:
                self.kv.chat_edit(logged["id"], content, body, asked=True)
                row = logged["id"]
            self.queue_text(chat_id, msg, content, p, body, row)
            log.info("edit: new message")
        elif logged is not None and not logged["asked"]:
            self.relog(logged, body, p.name)

    def relog(self, logged: Mapping, body: str, name: str) -> None:
        old_text, old_body = str(logged.get("text") or ""), logged.get("body")
        if isinstance(old_body, str) and old_body and old_text.endswith(old_body):
            text = old_text[: len(old_text) - len(old_body)] + body
        else:
            text = f"{name}: {body}"
        self.kv.chat_edit(logged["id"], text, body)

    # --- voice notes (hearing thread) ----------------------------------------------------

    def _hear_chat(self, chat: int) -> None:
        try:
            self.hear_all(chat, f"pending:{chat}")
        finally:
            self.worker.submit(chat)

    def hear_all(self, chat: int, key: str) -> None:
        for line in self.kv.get_json(key, []) or []:
            if "voice" not in line:
                continue
            try:
                content = self.hear(chat, line)
                row = None
                if content is not None:
                    row = self.kv.add_chat(
                        chat,
                        "user",
                        line.get("who"),
                        content,
                        int(line.get("ts") or self.now().timestamp()),
                        mid=line["id"] if isinstance(line["id"], int) else None,
                    )

                def settle(cur, line_id=line["id"], content=content, row=row):
                    out = []
                    for x in cur or []:
                        if x.get("id") == line_id and "voice" in x:
                            if content is None:
                                continue
                            x = {k: v for k, v in x.items() if k not in ("voice", "who")}
                            x["content"] = content
                            x["row"] = row
                        out.append(x)
                    return out or None

                self.kv.update_json(key, settle)
            finally:
                with self._q:
                    notes = self._voicing.get(chat) or {}
                    notes.pop(line["id"], None)
                    if not notes:
                        self._voicing.pop(chat, None)

    def hear(self, chat: int, line: Mapping) -> str | None:
        """One voice note: `getFile`, the download, the plugin's `transcribe`, then the «🎤»
        echo under the note. The line for the assistant, or None after posting why not."""
        block = self.settings.assistant or {}
        voice = line["voice"]
        note = {"message_id": line["id"], "allow_sending_without_reply": True}
        reason = "voice_failed"
        text = ""
        try:
            info = self.tg.call("getFile", file_id=voice["file_id"])
            info = info if isinstance(info, Mapping) else {}
            size, path = info.get("file_size"), info.get("file_path")
            if isinstance(size, int) and size > FILE_MAX:
                raise TelegramError("getFile", 413, "file too large")
            if not isinstance(path, str) or not path:
                raise TelegramError("getFile", 0, "no file path")
            audio = self.tg.download(path, FILE_MAX)
            text = assistant.transcribe(
                self.data,
                block.get("plugin"),
                audio,
                voice["mime"],
                self.settings.language,
                self.plugin_settings(),
            ).strip()
        except NotImplementedError:
            reason = "voice_off"
        except TelegramError as e:
            log.warning("voice failed: %s", safe(e))
            too_big = e.code == 413 or "too big" in e.description.lower()
            reason = "voice_too_long" if too_big else "voice_failed"
        except Exception as e:  # noqa: BLE001 - the plugin may raise anything
            log.warning("voice failed: %s", safe(e))
        thread = line.get("thread")
        if not text:
            log.info("voice not heard: %s", reason)
            self.post(chat, self.t(reason), thread=thread, reply_parameters=note)
            return None
        log.info("voice heard")
        echo = split_text(text, ECHO_MAX)
        shown = echo[0] + ("…" if len(echo) > 1 else "")
        self.post(
            chat,
            self.t("voice_echo", text=html.escape(shown)),
            thread=thread,
            reply_parameters=note,
        )
        return f"{line.get('who') or ''}: [voice note] {text}"

    def say(
        self,
        chat: int,
        text: str,
        reply_to: int | None,
        offered: Sequence[str] = (),
        users: Sequence[int] = (),
        thread: int | None = None,
    ) -> bool:
        """An assistant answer: plain text, a reply to the message, split at Telegram's limit.
        With choices its last part carries them as buttons, for `users` to tap, and a line
        saying they may answer in their own words; without, one that ends with a question
        carries a selective force_reply. False when a part could not be sent."""
        key = secrets.token_hex(4) if offered else None
        if offered:
            hint = self.t("yes_no_hint" if self.yes_no(offered) else "options_hint")
            text = f"{text}\n\n{hint}"
        parts = split_text(text)
        sent = None
        for i, part in enumerate(parts):
            markup = None
            if i == len(parts) - 1 and key:
                markup = _keyboard([[(label, f"o:{key}:{n}")] for n, label in enumerate(offered)])
            elif i == len(parts) - 1 and text.rstrip().endswith("?"):
                markup = {"force_reply": True, "selective": True}
            parameters = None
            if reply_to and i == 0:
                parameters = {"message_id": reply_to, "allow_sending_without_reply": True}
            try:
                sent = self.tg.call(
                    "sendMessage",
                    chat_id=chat,
                    text=part,
                    reply_parameters=parameters,
                    reply_markup=markup,
                    message_thread_id=self.thread_for(chat, thread),
                )
            except TelegramError as e:
                log.warning("send failed: %s", safe(e))
                return False
            self._own(chat, sent)
        if key:
            msg = sent.get("message_id") if isinstance(sent, Mapping) else None
            self.offer(chat, msg, key, offered, users, thread)
        return True

    # --- settings ------------------------------------------------------------------------

    def apply_settings(
        self, chat: int, block: object, admin: bool, thread=None, users: Sequence[int] = ()
    ) -> bool:
        """A `settings` block: check it all or nothing, rewrite `settings.yaml` by line, commit
        it (`settings: <keys>`), push, and use the new settings at once.

        The standing instructions are never lost by accident. `instructions_add` appends lines.
        A whole new `instructions` text that only adds lines is applied; one that drops or
        rewrites any existing line is not: the bot shows what would go and what would come and
        asks the admins who asked for a «Sí», and nothing changes without it."""
        try:
            if block is assistant.BAD_BLOCK:
                raise BlockError("bad_json")
            values = records.validate_settings(block, admin)
            current = str((self.settings.assistant or {}).get("instructions") or "")
            if records.INSTRUCTIONS_ADD in values:
                lines = values.pop(records.INSTRUCTIONS_ADD)
                values[records.INSTRUCTIONS] = records.add_instructions(current, lines)
            new = values.get(records.INSTRUCTIONS)
            if new is not None and len(new) > records.INSTRUCTIONS_MAX:
                raise BlockError("bad_instructions")
        except BlockError as e:
            log.warning("settings refused: %s", e.code)
            self.post(chat, self.t("invalid_settings"), thread=thread)
            return False
        diff = ([], [])
        if new is not None:
            diff = records.instruction_diff(current, new)
            if new == current:
                values.pop(records.INSTRUCTIONS)  # nothing to change
            elif diff[1]:
                values.pop(records.INSTRUCTIONS)
                self.ask_instructions(chat, current, new, diff, users, thread)
                diff = ([], [])  # asked, not done: the confirmation below must not list it
        if not values:
            return True
        if not self.write_settings(chat, values, thread):
            return False
        done = self.t("settings_done", what=self.settings_what(values, diff))
        self.post(chat, done + self.diff_text(*diff), thread=thread)
        return True

    def write_settings(
        self, chat: int, values: dict, thread=None, expect_instructions: str | None = None
    ) -> bool:
        """Rewrite `settings.yaml` by line with `values`, checked, committed and pushed; the new
        settings are used at once. With `expect_instructions`, the write happens only if the
        instructions in the file it rewrites are still that text. False, after saying so, when
        it could not be written."""
        result: list[Settings] = []
        moved: list[bool] = []

        def rewrite(old: str) -> str:
            if expect_instructions is not None:
                block = (yaml.safe_load(old) or {}).get("assistant") or {}
                if str(block.get("instructions") or "") != expect_instructions:
                    moved.append(True)
                    raise ValueError("instructions_moved")
            new = records.set_lines(old, values)
            loaded = yaml.safe_load(new)
            s = Settings.from_dict(loaded)
            if settings_errors(s, self.data):
                raise ValueError("settings_not_editable")
            for (section, key), value in values.items():
                if records.clock_text(loaded.get(section, {}).get(key)) != value:
                    raise ValueError("settings_not_editable")
            result.append(s)
            return new

        message = ", ".join(f"{a}.{b}" for a, b in values)
        try:
            self.repo.write_settings(rewrite, message)
        except (ValueError, yaml.YAMLError, GitError, OSError, AttributeError) as e:
            if moved:
                log.info("instructions change refused: moved")
                self.post(chat, self.t("instr_moved"), thread=thread)
                return False
            log.warning("settings failed: %s", safe(e))
            self.post(chat, self.t("settings_failed"), thread=thread)
            return False
        if result:
            self.settings = result[0]
        self.repo.settings_pulled = False
        log.info("settings applied keys=%d", len(values))
        return True

    def settings_what(self, values: Mapping, diff: tuple[list[str], list[str]]) -> str:
        what = []
        for (section, key), value in values.items():
            if key == "day_before_at":
                what.append(self.t("settings_day_before_at", time=value))
            elif key == "followup":
                what.append(self.t("settings_followup_" + ("on" if value else "off")))
            else:
                what.append(self.t("settings_instructions"))
        return "; ".join(what)

    def diff_text(self, added: Sequence[str], removed: Sequence[str]) -> str:
        """The lines that go and the lines that come, one per line, «−» and «+»."""
        lines = [f"\n− {html.escape(x)}" for x in removed]
        lines += [f"\n+ {html.escape(x)}" for x in added]
        return "".join(lines)

    # --- a change of the standing instructions that drops lines waits for a «Sí» ------------

    def ask_instructions(
        self, chat: int, base: str, new: str, diff, users: Sequence[int], thread=None
    ) -> None:
        """Show the change and ask the admins who asked for it (all admins when none of them
        did) with «Sí» and «No», for 12 hours. «Sí» writes exactly that text, if the
        instructions are still what they were when it was asked."""
        by = [u for u in users if self.is_admin(u)] or self.admin_uids()
        offer = {
            "key": secrets.token_hex(4),
            "kind": "instr",
            "for": by,
            "items": [{"done": None}],
            "base": base,
            "new": new,
            "ts": self.now().timestamp(),
            "ttl": INSTR_TTL_S,
            "thread": thread,
        }
        text = self.t("instr_ask", name=self.names(by), lines=self.diff_text(*diff).lstrip("\n"))
        sent = self.post(chat, text, _keyboard(self.instr_rows(offer)), thread)
        if sent is None:
            return
        offer["msg"] = sent.get("message_id")
        self.keep_offer(chat, offer)
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        self.kv.add_chat(chat, "assistant", None, plain, int(self.now().timestamp()))
        log.info("instructions change asked removed=%d added=%d", len(diff[1]), len(diff[0]))

    def instr_rows(self, offer: Mapping) -> list:
        done = offer["items"][0].get("done")
        if done:
            label = {"yes": "button_yes", "no": "button_no"}.get(done, OFFER_GONE)
            return [[("✓ " + self.t(label), "x")]]
        key = offer["key"]
        return [[(self.t("button_yes"), f"i:{key}:y"), (self.t("button_no"), f"i:{key}:n")]]

    def tapped_instr(self, cq: Mapping, chat: int, data: str, user: object) -> None:
        _, key, choice = [*data.split(":"), "", ""][:3]
        mid = (cq.get("message") or {}).get("message_id")
        offer = self.find_offer(chat, key)
        if offer is None:
            self.answer(cq["id"], self.t("tap_gone"))
            self.unbutton(chat, mid)
            return
        if user not in offer.get("for", []):
            self.answer(cq["id"], self.t("tap_not_yours", name=self.plain_names(offer["for"])))
            return
        if choice not in ("y", "n") or offer["items"][0].get("done"):
            self.answer(cq["id"], self.t("tap_gone"))
            return
        offer["items"][0]["done"] = "yes" if choice == "y" else "no"
        self.keep_offer(chat, offer)
        self.answer(cq["id"])
        self.rekey(chat, offer.get("msg"), self.instr_rows(offer))
        thread = offer.get("thread")
        if choice == "n":
            self.kv.add_chat(
                chat, "assistant", None, self.t("instr_kept"), int(self.now().timestamp())
            )
            self.post(chat, self.t("instr_kept"), thread=thread)
            log.info("instructions change declined")
            return
        values = {records.INSTRUCTIONS: offer["new"]}
        if self.write_settings(chat, values, thread, expect_instructions=offer["base"]):
            diff = records.instruction_diff(offer["base"], offer["new"])
            done = self.t("settings_done", what=self.settings_what(values, diff))
            self.post(chat, done + self.diff_text(*diff), thread=thread)
            log.info("instructions change approved")

    # --- file edits: keeping the household's files current ----------------------------------

    def apply_edits(
        self,
        chat: int,
        blocks: Sequence[object],
        scope: Sequence[str],
        asker: str | None,
        private: bool,
        admin: bool,
        users: Sequence[int],
        thread=None,
    ) -> bool:
        """The ```edit blocks of an answer, checked all or nothing: every path allowed for
        this chat and person, the result sound on a copy (`smd check`). An edit the person
        asked for is committed at once and confirmed with its diff; one the assistant proposes,
        the deletion of a file, more than half of one, or a line of the instructions waits for
        a «Sí»."""
        try:
            if any(b is assistant.BAD_BLOCK for b in blocks):
                raise BlockError("bad_json")
            wanted, asked = edits.parse(blocks)
            wanted = [replace(e, path=edits.normal(e.path)) for e in wanted]
            for e in wanted:
                edits.allowed(
                    e.path, self.settings, scope=scope, asker=asker, private=private, admin=admin
                )
            with DATA_LOCK:  # from the plan to the commit, nothing else writes
                self.repo.pull()
                planned = edits.plan(self.data, wanted)
                if not planned:
                    return True
                files = self.edit_files(planned)
                ask = not asked or any(p.big or edits.instructions_dropped(p) for p in planned)
                if files is not None and not ask:
                    return self.commit_edits(chat, planned, files, asker, thread)
        except BlockError as e:
            log.warning("edit refused: %s", e.code)
            self.post(chat, self.t("edit_refused"), thread=thread)
            return False
        if files is None:
            self.post(chat, self.t("edit_check_failed"), thread=thread)
            return False
        self.ask_edits(chat, planned, users, thread)
        return True

    def edit_files(self, planned: Sequence[edits.Planned]) -> dict | None:
        """The files one commit of `planned` writes: the edits and, when a person's file
        changed, the exports regenerated from the edited data. None when the edited copy fails
        the checks of `smd check`."""
        with tempfile.TemporaryDirectory(prefix="smd-edit-") as tmp:
            copy = edits.copy_data(self.data, tmp)
            errors = edits.check_copy(copy, planned)
            if errors:
                log.warning("edit refused: check_failed n=%d", len(errors))
                return None
            files: dict = {p.path: p.after for p in planned}
            if any(p.path.startswith(records.PEOPLE_DIR + "/") for p in planned):
                after = records.load_people(copy, list(self.settings.people))
                files.update(self.export_files(after))
        return files

    def edit_text(self, planned: Sequence[edits.Planned]) -> str:
        lines = []
        for p in planned:
            key = "edit_deleted" if p.after is None else "edit_file"
            lines.append(self.t(key, path=html.escape(p.path)))
            lines += [html.escape(x) for x in edits.diff(p)]
        return "\n".join(lines)

    def commit_edits(self, chat: int, planned, files: dict, asker: str | None, thread=None) -> bool:
        who = asker or "group"
        message = f"chat({who}): edit {edits.paths_text(planned)}"[:200]
        try:
            sha = self.repo.commit(files, message)
        except (GitError, OSError) as e:
            log.warning("edit failed: %s", safe(e))
            self.post(chat, self.t("record_failed"), thread=thread)
            return False
        if any(p.path == records.SETTINGS_FILE for p in planned):
            self.repo.settings_pulled = True
            self.reload_if_pulled()
        text = self.t("edited") + "\n" + self.edit_text(planned)
        self.post(chat, text, thread=thread)
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        self.kv.add_chat(chat, "assistant", None, plain, int(self.now().timestamp()))
        if sha:
            said = self.t("edit_undo_said", paths=edits.paths_text(planned))
            entry = {"sha": sha, "person": who, "said": said[:300]}
            self.kv.update_json(f"undo:{chat}", lambda cur: [*(cur or []), entry][-UNDO_KEEP:], [])
        log.info("edit written files=%d", len(planned))
        return True

    def ask_edits(self, chat: int, planned, users: Sequence[int], thread=None) -> None:
        """Offer the edits with their diff and «Sí»/«No», for 12 hours, to the people the answer
        covered (the admins, for `settings.yaml` or when nobody is known)."""
        settings_edit = any(p.path == records.SETTINGS_FILE for p in planned)
        by = [u for u in users if not settings_edit or self.is_admin(u)] or self.admin_uids()
        offer = {
            "key": secrets.token_hex(4),
            "kind": "edit",
            "for": by,
            "items": [{"done": None}],
            "planned": [{"path": p.path, "before": p.before, "after": p.after} for p in planned],
            "ts": self.now().timestamp(),
            "ttl": INSTR_TTL_S,
            "thread": thread,
        }
        text = self.t("edit_ask", name=self.names(by), lines=self.edit_text(planned))
        sent = self.post(chat, text, _keyboard(self.edit_rows(offer)), thread)
        if sent is None:
            return
        offer["msg"] = sent.get("message_id")
        self.keep_offer(chat, offer)
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        self.kv.add_chat(chat, "assistant", None, plain, int(self.now().timestamp()))
        log.info("edit offered files=%d", len(planned))

    def edit_rows(self, offer: Mapping) -> list:
        done = offer["items"][0].get("done")
        if done:
            label = {"yes": "button_yes", "no": "button_no"}.get(done, OFFER_GONE)
            return [[("✓ " + self.t(label), "x")]]
        key = offer["key"]
        return [[(self.t("button_yes"), f"e:{key}:y"), (self.t("button_no"), f"e:{key}:n")]]

    def tapped_edit(self, cq: Mapping, chat: int, data: str, user: object) -> None:
        """«Sí» writes exactly the edits shown, if every file is still what it was then;
        «No» leaves everything as it is."""
        _, key, choice = [*data.split(":"), "", ""][:3]
        mid = (cq.get("message") or {}).get("message_id")
        offer = self.find_offer(chat, key)
        if offer is None:
            self.answer(cq["id"], self.t("tap_gone"))
            self.unbutton(chat, mid)
            return
        if user not in offer.get("for", []):
            self.answer(cq["id"], self.t("tap_not_yours", name=self.plain_names(offer["for"])))
            return
        if choice not in ("y", "n") or offer["items"][0].get("done"):
            self.answer(cq["id"], self.t("tap_gone"))
            return
        offer["items"][0]["done"] = "yes" if choice == "y" else "no"
        self.keep_offer(chat, offer)
        self.answer(cq["id"])
        self.rekey(chat, offer.get("msg"), self.edit_rows(offer))
        thread = offer.get("thread")
        if choice == "n":
            self.post(chat, self.t("edit_kept"), thread=thread)
            log.info("edit declined")
            return
        planned = [edits.Planned(x["path"], x["before"], x["after"]) for x in offer["planned"]]
        p = self.person(user)
        with DATA_LOCK:  # the files compared are the files committed over
            self.repo.pull()
            moved = any(self.repo._read(x.path) != _bytes(x.before) for x in planned)
            files = None if moved else self.edit_files(planned)
            if files is not None:
                self.commit_edits(chat, planned, files, p.id if p else None, thread)
                return
        if moved:
            self.post(chat, self.t("edit_moved"), thread=thread)
            log.info("edit refused: moved")
            return
        self.post(chat, self.t("edit_check_failed"), thread=thread)


def _bytes(text: str | None) -> bytes | None:
    return None if text is None else text.encode("utf-8")


def hm(value: object) -> str:
    """«09:30» as people write it, «9:30»."""
    text = str(value or "")
    return text[1:] if len(text) == 5 and text.startswith("0") else text


def lower_first(text: str) -> str:
    """«En ayunas» inside a sentence: «en ayunas» (an acronym such as «INR» stays)."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def split_text(text: str, limit: int = TEXT_MAX) -> list[str]:
    """`text` in parts of at most `limit` UTF-16 code units, cut between paragraphs where it
    can and inside one only when a paragraph alone is too long."""
    parts, current = [], ""
    for para in text.split("\n\n"):
        joined = f"{current}\n\n{para}" if current else para
        if _units(joined) <= limit:
            current = joined
            continue
        if current:
            parts.append(current)
        while _units(para) > limit:
            cut = limit
            while _units(para[:cut]) > limit:
                cut -= 1
            parts.append(para[:cut])
            para = para[cut:]
        current = para
    if current:
        parts.append(current)
    return parts


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"
