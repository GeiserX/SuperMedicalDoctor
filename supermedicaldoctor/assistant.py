"""The assistant plugin: loading it, the turn it gets, and the blocks it may send back.

The plugin is one Python file in the data directory (`settings.yaml` `assistant.plugin`). The app
never knows which provider sits behind it: it hands over the rules, a fresh `context.md`, the data
files of the people in the turn's scope, the files fetched from other repositories, the photos and
files sent in the turn, and the new lines; it takes back text that may end with `record` blocks
(one per person), a `settings` block, `options` and `reminders`. The app, not the assistant,
validates and applies them. A plugin may also turn a voice note into text (`transcribe`), ask a
person to approve an action (`answer`, through `turn["ask"]`) and hand back slow work to finish
after its answer is posted (`later`).

Nothing here logs: errors are `PluginError` reason codes, and a plugin's own exception goes up to
the caller, which logs its class only (`telegram.safe`).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cache, lru_cache
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

from supermedicaldoctor.i18n import Translator
from supermedicaldoctor.records import (
    SLOTS,
    PersonData,
    active,
    as_date,
    clock_text,
)

RULES = Path(__file__).parent / "rules.md"
PRINCIPLES = Path(__file__).parent / "principles"  # the shared assistant principles, per language
DEFAULT_TIMEOUT_S = 900
NOT_SETTINGS = ("plugin", "instructions")  # the settings keys a plugin's settings leave out
LANGUAGE_NAMES = {"en": "English", "es": "Spanish"}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
APPOINTMENT_DAYS = 60  # context.md lists the pending appointments this far ahead
DONE_SHOWN = 3  # and the last done ones

BLOCK_KINDS = ("record", "settings", "edit")  # the fenced JSON blocks an answer may end with
_BLOCK = {k: re.compile(rf"```{k}[^\S\n]*\n(.*?)```", re.DOTALL) for k in BLOCK_KINDS}
_UNCLOSED = {k: re.compile(rf"```{k}\b.*\Z", re.DOTALL) for k in BLOCK_KINDS}
BAD_BLOCK = object()  # `extract` found a block whose JSON does not parse
OPTIONS_MAX, OPTION_CHARS = 5, 40  # choices offered as buttons under an answer
_OPTIONS = re.compile(r"\n?```options[^\S\n]*\n(.*?)```[^\S\n]*", re.DOTALL)
REMINDERS_MAX, REMINDER_CHARS = 5, 300  # reminders an answer may offer
_REMINDERS = re.compile(r"\n?```reminders[^\S\n]*\n(.*?)```[^\S\n]*", re.DOTALL)
_WHEN = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")


class PluginError(Exception):
    """The plugin could not be loaded or answered wrongly. The message is a reason code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- loading -------------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _module(path: str, mtime_ns: int) -> ModuleType:
    """One module per plugin file version: state the plugin keeps at module level survives
    between calls, and an edited file (a data repository pull) is loaded again.

    The module sits in `sys.modules` under a name unique to this version before it runs, as a
    normal import does: `dataclasses` and `typing` look the module up there by name."""
    digest = hashlib.sha256(path.encode()).hexdigest()[:12]
    name = f"smd_plugin_{digest}_{mtime_ns}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PluginError("plugin_load_failed")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    # the plugin lives in the data checkout: compiling it must not leave __pycache__ there
    writes, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    finally:
        sys.dont_write_bytecode = writes
    return mod


def load_module(data: str | Path, rel: str | None) -> ModuleType:
    """The plugin module at `rel`, which must resolve inside the data directory."""
    if not rel or not isinstance(rel, str):
        raise PluginError("no_plugin")
    root = Path(data).resolve()
    path = (root / rel).resolve()
    if root not in path.parents:
        raise PluginError("plugin_outside_data")
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        raise PluginError("plugin_missing") from None
    try:
        return _module(str(path), mtime_ns)
    except PluginError:
        raise
    except Exception:  # noqa: BLE001 - a plugin's import error may quote anything
        raise PluginError("plugin_load_failed") from None


def function(data: str | Path, rel: str | None, name: str, required: bool = True):
    fn = getattr(load_module(data, rel), name, None)
    if not callable(fn):
        if required:
            raise PluginError(f"plugin_has_no_{name}")
        return None
    return fn


def settings(assistant: Mapping | None) -> dict:
    """The `assistant:` block of the settings minus `plugin` and `instructions`."""
    return {k: v for k, v in (assistant or {}).items() if k not in NOT_SETTINGS}


def timeout_s(plugin_settings: Mapping) -> float:
    value = plugin_settings.get("timeout_s")
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return float(DEFAULT_TIMEOUT_S)


# --- calling -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    text: str
    conversation: str | None = None
    kept: bool = True
    later: Callable[[], str] | None = None  # slow work the app runs after posting `text`
    covers: frozenset = frozenset()  # ids of handed-over messages the answer took in
    options: tuple[str, ...] = ()  # choices to offer as buttons under the answer
    reminders: tuple[dict, ...] = ()  # {"when": "YYYY-MM-DD HH:MM", "text", "person"?}


def reply(out: object) -> Reply:
    """A plugin's return value: a bare string, or `{"text", "conversation", "kept", "later",
    "covers", "options", "reminders"}`."""
    if isinstance(out, str):
        return Reply(out)
    if isinstance(out, Mapping):
        text, conversation, kept = out.get("text"), out.get("conversation"), out.get("kept", True)
        later, covers = out.get("later"), out.get("covers")
        if not isinstance(text, str) or not isinstance(kept, bool):
            raise PluginError("bad_reply")
        if conversation is not None and not isinstance(conversation, str):
            raise PluginError("bad_reply")
        if later is not None and not callable(later):
            raise PluginError("bad_reply")
        ids = (
            frozenset(
                i for i in covers or () if isinstance(i, int | str) and not isinstance(i, bool)
            )
            if isinstance(covers, list | tuple)
            else frozenset()
        )
        return Reply(
            text,
            conversation or None,
            kept,
            later,
            ids,
            options(out.get("options")),
            reminders(out.get("reminders")),
        )
    raise PluginError("bad_reply")


def options(raw: object) -> tuple[str, ...]:
    """Well-formed choices: short, distinct, at most OPTIONS_MAX; anything else is dropped."""
    out: list[str] = []
    for o in raw if isinstance(raw, list | tuple) else ():
        label = " ".join(o.split()) if isinstance(o, str) else ""
        if label and len(label) <= OPTION_CHARS and label not in out:
            out.append(label)
    return tuple(out[:OPTIONS_MAX])


def extract_options(text: str) -> tuple[str, tuple[str, ...]]:
    """The answer without its ```options block, and the choices the last block lists, one
    `- label` per line."""
    blocks = list(_OPTIONS.finditer(text))
    if not blocks:
        return text, ()
    lines = [x.strip() for x in blocks[-1].group(1).splitlines()]
    labels = [x[1:].strip() for x in lines if x.startswith(("-", "*"))]
    clean = re.sub(r"\n{3,}", "\n\n", _OPTIONS.sub("", text)).strip()
    return clean, options(labels)


def reminders(raw: object) -> tuple[dict, ...]:
    """Well-formed reminders: a time written `YYYY-MM-DD HH:MM`, a short text and, optionally,
    the id of the person it is for; distinct, at most REMINDERS_MAX; anything else is dropped.
    Whether the time is still to come, and the person known, is the app's to judge."""
    out: list[dict] = []
    for r in raw if isinstance(raw, list | tuple) else ():
        if not isinstance(r, Mapping):
            continue
        when = str(r.get("when") or "").strip()
        text = " ".join(str(r.get("text") or "").split())
        if not _WHEN.fullmatch(when) or not text or len(text) > REMINDER_CHARS:
            continue
        item = {"when": when.replace("T", " "), "text": text}
        person = str(r.get("person") or "").strip()
        if person:
            item["person"] = person
        if item not in out:
            out.append(item)
    return tuple(out[:REMINDERS_MAX])


def extract_reminders(text: str) -> tuple[str, tuple[dict, ...]]:
    """The answer without its ```reminders block, and the reminders the last block lists, one
    `- YYYY-MM-DD HH:MM | text [| person]` per line."""
    blocks = list(_REMINDERS.finditer(text))
    if not blocks:
        return text, ()
    found = []
    for line in blocks[-1].group(1).splitlines():
        line = line.strip()
        if line.startswith(("-", "*")) and "|" in line:
            parts = [p.strip() for p in line[1:].split("|")]
            item = {"when": parts[0], "text": parts[1]}
            if len(parts) > 2:
                item["person"] = parts[2]
            found.append(item)
    clean = re.sub(r"\n{3,}", "\n\n", _REMINDERS.sub("", text)).strip()
    return clean, reminders(found)


# per plugin path and function: a call that outlived its wait
_ABANDONED: dict[str, threading.Thread] = {}


def _call(data: str | Path, rel: str | None, fn: Callable, args: tuple, plugin_settings: dict):
    """Run a plugin function in its own daemon thread and wait at most `timeout_s`.

    The app cannot stop a plugin: a call that outlives the wait is abandoned and counts as
    failed (`PluginError("timeout")`). Until that call returns, the same function is not called
    again (`PluginError("plugin_busy")`), so a turn never starts beside an abandoned turn. The
    functions themselves may run at once: a note is heard and an ask answered while a turn
    runs, so one abandoned function never blocks another, and a call that ends clears only a
    marker whose thread has ended too. The plugin's own exception is raised as it is; an exit
    that is not an `Exception` (`SystemExit`, `KeyboardInterrupt`) becomes
    `PluginError("plugin_aborted")`."""
    key = f"{(Path(data) / str(rel)).resolve()}:{getattr(fn, '__name__', '?')}"
    previous = _ABANDONED.get(key)
    if previous is not None and previous.is_alive():
        raise PluginError("plugin_busy")
    box: dict = {}

    def run():
        try:
            box["out"] = fn(*args, plugin_settings)
        except BaseException as e:  # noqa: BLE001 - handed to the caller as it is
            box["error"] = e

    thread = threading.Thread(target=run, name="smd-plugin", daemon=True)
    thread.start()
    thread.join(timeout_s(plugin_settings))
    if thread.is_alive():
        _ABANDONED[key] = thread
        raise PluginError("timeout")
    stale = _ABANDONED.get(key)
    if stale is not None and not stale.is_alive():
        _ABANDONED.pop(key, None)
    error = box.get("error")
    if error is not None:
        if not isinstance(error, Exception):  # SystemExit and the like must not end the worker
            raise PluginError("plugin_aborted")
        raise error
    return box.get("out")


def converse(data: str | Path, rel: str | None, turn: dict, plugin_settings: dict) -> Reply:
    """Run the plugin's `converse` and wait at most `timeout_s` (see `_call`)."""
    fn = function(data, rel, "converse")
    return reply(_call(data, rel, fn, (turn,), plugin_settings))


def transcribe(
    data: str | Path, rel: str | None, audio: bytes, mime: str, language: str, plugin_settings: dict
) -> str:
    """A voice note's text from the plugin's optional `transcribe`, with the same wait as a turn.

    Raises `NotImplementedError` when there is no plugin or it defines no `transcribe` (as the
    plugin itself does when its provider cannot listen), `PluginError("bad_transcript")` when it
    returns something that is not a string, and anything else the plugin raises as it is."""
    if not rel:
        raise NotImplementedError
    fn = function(data, rel, "transcribe", required=False)
    if fn is None:
        raise NotImplementedError
    out = _call(data, rel, fn, (audio, mime, language), plugin_settings)
    if not isinstance(out, str):
        raise PluginError("bad_transcript")
    return out


def asks(data: str | Path, rel: str | None) -> bool:
    """Whether the plugin may ask a person to approve an action while it works (it defines
    `answer`). A plugin that cannot be loaded asks nothing."""
    try:
        return function(data, rel, "answer", required=False) is not None
    except PluginError:
        return False


def answer(
    data: str | Path, rel: str | None, request: dict, decision: dict, plugin_settings: dict
) -> dict:
    """A person's answer to an ask, back to the plugin's `answer`, with the same wait as a turn:
    `{"ok": bool | None, "error": str | None}`."""
    fn = function(data, rel, "answer")
    out = _call(data, rel, fn, (request, decision), plugin_settings)
    out = out if isinstance(out, Mapping) else {}
    ok, error = out.get("ok"), out.get("error")
    return {
        "ok": ok if isinstance(ok, bool) else None,
        "error": error if isinstance(error, str) else None,
    }


def forget(data: str | Path, rel: str | None, conversation: str, plugin_settings: dict) -> None:
    """Tell the plugin to drop a conversation, when it defines `forget`."""
    fn = function(data, rel, "forget", required=False)
    if fn is not None:
        _call(data, rel, fn, (conversation,), plugin_settings)


# --- the turn ------------------------------------------------------------------------------
# --- the turn ------------------------------------------------------------------------------


def turn(
    conversation: str | None,
    rules_text: str,
    context_text: str,
    new: Sequence[str | Mapping],
    history: Sequence[Mapping] = (),
    *,
    scope: Sequence[str],
    asker: str | None,
    files: Mapping[str, str],
    sources: Mapping[str, str],
    attachments: Sequence[Mapping],
    calendars: Mapping[str, str],
    chat: int | None = None,
    user: int | None = None,
) -> dict:
    """The dict a plugin's `converse` gets: the new lines oldest first (a line is its text, or
    `{"content", "ts"}`), the chat's earlier log as `history` (`{"role", "content", "ts"}`), the
    data of the people in `scope` only, and `chat`: the Telegram chat id and the user id of the
    person whose message started the turn."""
    lines = []
    for x in new:
        item = {"role": "user", "content": x if isinstance(x, str) else x["content"]}
        if isinstance(x, Mapping) and x.get("ts"):
            item["ts"] = x["ts"]
        lines.append(item)
    return {
        "conversation": conversation,
        "rules": rules_text,
        "context": context_text,
        "scope": list(scope),
        "asker": asker,
        "files": dict(files),
        "sources": dict(sources),
        "attachments": [dict(a) for a in attachments],
        "calendars": dict(calendars),
        "new": lines,
        "history": [dict(h) for h in history],
        "chat": {"id": chat, "user": user},
    }


def rules(
    people: Mapping[str, str],
    language: str,
    instructions: str = "",
    reach: str = "",
) -> str:
    """The shared assistant principles in the household's language, then `rules.md` filled in.
    Plain replacement, not `str.format`: the text holds JSON braces. `people` maps each id to
    a name; `reach` says which group messages reach the assistant."""
    named = [f"{name} (id {pid})" for pid, name in people.items()]
    values = {
        "people": _join(named) or "the household",
        "person_ids": ", ".join(people) or "-",
        "language_name": LANGUAGE_NAMES.get(language, language),
        "reach": reach
        or (
            "In the group, a message reaches you when it mentions you or replies to you; every "
            "voice note, photo and file from a person reaches you too. In a private chat "
            "everything does."
        ),
        "instructions": (instructions or "").strip(),
    }
    text = RULES.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return principles(language) + "\n" + text.rstrip() + "\n"


@cache
def principles(language: str) -> str:
    """The shared assistant principles in `language` (English when there is no copy in it). They
    come first: the rules after them say how they apply to this bot."""
    lang = (language or "en").split("-")[0].split("_")[0].lower()
    path = PRINCIPLES / f"{lang}.md"
    if not path.is_file():
        path = PRINCIPLES / "en.md"
    return path.read_text(encoding="utf-8").rstrip() + "\n"


def extract(text: str, kind: str = "record") -> tuple[str, list]:
    """Take every `kind` block (`record` or `settings`) out of an answer: `(clean text, the
    blocks)`, each block the parsed JSON or `BAD_BLOCK` when it does not parse (or its fence
    never closes). Every block leaves the text, so nobody sees one."""
    blocks: list = []
    for raw in _BLOCK[kind].findall(text):
        try:
            blocks.append(json.loads(raw))
        except ValueError:
            blocks.append(BAD_BLOCK)
    clean = _BLOCK[kind].sub("", text)
    unclosed = _UNCLOSED[kind].search(clean)
    if unclosed:
        clean = clean[: unclosed.start()]
        blocks.append(BAD_BLOCK)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, blocks


# --- context.md ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceState:
    key: str
    fetched: float | None
    stale: bool


def context(
    people: Mapping[str, PersonData],
    names: Mapping[str, str],
    now: datetime,
    *,
    tz: str,
    t: Translator,
    reminders: Sequence[Mapping] = (),
    sources: Sequence[SourceState] = (),
    head: str | None = None,
    pull_ok: bool | None = True,
) -> str:
    """`context.md`, rebuilt every turn for the people in scope, with the household's labels:
    today, each person's age, their pending appointments (next 60 days) and the last done ones,
    the medicines and supplements they take by slot, the latest value of every lab test, the
    reminders set in this chat, each source file's freshness and the data repository's state."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    today = local.date()
    out = [f"# {t('ctx_today')}"]
    wd = today.weekday()
    out.append(
        f"{t.date(today)} {today.year} ({WEEKDAYS[wd]} {today.isoformat()}), {local:%H:%M} {tz}."
    )
    week = [today + timedelta(days=i) for i in range(1, 8)]
    out.append(
        t("ctx_next_days")
        + ": "
        + ", ".join(f"{t('days')[d.weekday()]} {d.isoformat()}" for d in week)
    )

    out += ["", f"# {t('ctx_people')}"]
    for pid, pd in people.items():
        line = f"- {names.get(pid, pid)} (id {pid})"
        birth = as_date(pd.profile.get("birth_date"))
        if birth is not None:
            line += ", " + t("ctx_age", n=_age(birth, today))
        out.append(line)

    out += ["", f"# {t('ctx_appointments', days=APPOINTMENT_DAYS)}"]
    shown = 0
    for pid, pd in people.items():
        pending = sorted(
            (a for a in pd.appointments if _pending_within(a, today, APPOINTMENT_DAYS)),
            key=_appt_key,
        )
        for a in pending:
            out.append(f"- {pid}: " + _appt_line(a, pd, t))
            shown += 1
    if not shown:
        out.append(t("ctx_none"))
    out += ["", f"# {t('ctx_done', n=DONE_SHOWN)}"]
    shown = 0
    for pid, pd in people.items():
        done = [a for a in pd.appointments if isinstance(a, Mapping) and a.get("status") == "done"]
        for a in sorted(done, key=_appt_key)[-DONE_SHOWN:]:
            line = f"- {pid}: " + _appt_line(a, pd, t)
            if a.get("outcome"):
                line += f" — {t('ctx_outcome')}: {a['outcome']}"
            out.append(line)
            shown += 1
    if not shown:
        out.append(t("ctx_none"))

    out += ["", f"# {t('ctx_meds')}"]
    shown = 0
    for pid, pd in people.items():
        taken = [m for m in pd.meds if isinstance(m, Mapping) and active(m, today)]
        order = {s: i for i, s in enumerate(SLOTS)}
        for m in sorted(taken, key=lambda m: (order.get(m.get("slot"), 99), str(m.get("name")))):
            kind = t("ctx_kind_" + str(m.get("kind")))
            parts = [str(m.get("name")), str(m.get("dose") or "")]
            line = f"- {pid}, {t.slot(str(m.get('slot') or 'as_needed'))}: "
            line += " ".join(p for p in parts if p) + f" ({kind}; id {m.get('id')})"
            if m.get("days") not in (None, "daily"):
                line += f"; {t('ctx_days')}: {', '.join(m['days'])}"
            if m.get("food"):
                line += f"; {m['food']}"
            out.append(line)
            shown += 1
    if not shown:
        out.append(t("ctx_none"))

    out += ["", f"# {t('ctx_labs')}"]
    shown = 0
    for pid, pd in people.items():
        latest: dict[str, Mapping] = {}
        for lab in pd.labs:
            test = str(lab.get("test") or "")
            if test and str(lab.get("date", "")) >= str(latest.get(test, {}).get("date", "")):
                latest[test] = lab
        for test in sorted(latest, key=str.casefold):
            lab = latest[test]
            line = f"- {pid}: {test} {lab.get('value')}"
            if lab.get("unit"):
                line += f" {lab['unit']}"
            line += f" ({lab.get('date')})"
            flag = _range_flag(lab)
            if flag:
                line += f", {t('ctx_' + flag)}"
            if lab.get("low") is not None or lab.get("high") is not None:
                line += f" [{_fmt(lab.get('low'))}-{_fmt(lab.get('high'))}]"
            out.append(line)
            shown += 1
    if not shown:
        out.append(t("ctx_none"))

    out += ["", f"# {t('ctx_reminders')}"]
    for r in reminders:
        when = datetime.fromtimestamp(float(r["at"]), zone)
        out.append(f"- {when:%Y-%m-%d %H:%M}: {r['text']}")
    if not reminders:
        out.append(t("ctx_none"))

    out += ["", f"# {t('ctx_sources')}"]
    for s in sources:
        if s.fetched is None:
            out.append(f"- {s.key}: {t('ctx_source_missing')}")
            continue
        at = datetime.fromtimestamp(s.fetched, zone).strftime("%H:%M")
        state = t("ctx_source_stale", time=at) if s.stale else t("ctx_source_ok", time=at)
        out.append(f"- {s.key}: {state}")
    if not sources:
        out.append(t("ctx_none"))

    out += ["", f"# {t('ctx_data')}"]
    state = t("ctx_pull_ok") if pull_ok else t("ctx_pull_failed")
    out.append(f"{t('ctx_head')}: {head or '-'}. {state}")
    return "\n".join(out) + "\n"


def _pending_within(a: object, today: date, days: int) -> bool:
    if not isinstance(a, Mapping) or a.get("status", "pending") != "pending":
        return False
    d = as_date(a.get("date"))
    return d is not None and d <= today + timedelta(days=days) and d >= today - timedelta(days=7)


def _appt_key(a: Mapping) -> tuple:
    return (str(as_date(a.get("date")) or ""), str(clock_text(a.get("time")) or ""))


def _appt_line(a: Mapping, pd: PersonData, t: Translator) -> str:
    d = as_date(a.get("date"))
    when = d.isoformat() if d else "?"
    if a.get("time"):
        when += f" {clock_text(a.get('time'))}"
    parts = [when, str(a.get("what") or "")]
    if a.get("place"):
        parts.append(str(a["place"]))
    doctor = pd.doctor(a.get("doctor")) if a.get("doctor") else None
    if doctor:
        parts.append(" ".join(str(x) for x in (doctor.get("name"), doctor.get("specialty")) if x))
    line = ", ".join(parts)
    prep = [
        f"{p.get('text')} ({p.get('hours_before')} h)"
        for p in a.get("prep") or []
        if isinstance(p, Mapping)
    ]
    if prep:
        line += f"; {t('ctx_prep')}: " + "; ".join(prep)
    return line + f" [id {a.get('id')}]"


def _range_flag(lab: Mapping) -> str | None:
    value, low, high = lab.get("value"), lab.get("low"), lab.get("high")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(low, int | float) and not isinstance(low, bool) and value < low:
        return "low"
    if isinstance(high, int | float) and not isinstance(high, bool) and value > high:
        return "high"
    return None


def _fmt(value: object) -> str:
    return "" if value is None else str(value)


def _age(birth: date, today: date) -> int:
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def _join(items: Sequence[str]) -> str:
    items = [x for x in items if x]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
