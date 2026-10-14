"""The data directory: its git checkout, and the SQLite plumbing beside it.

`<data>` is a checkout of the household's private data repository. The bot answers from its
latest commit: it pulls before every turn and before every write (a pull in the last 30 s is
skipped, so a burst costs one) and every 10 minutes on the tick. Each change it makes is one
commit, pushed at once; a push that fails is retried on the tick. Every git sequence holds
`DATA_LOCK`, so a pull never lands in the middle of a write.

The service log gets event kinds only: never a value, a file name or git's own output.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

log = logging.getLogger("smd.repo")

DATA_LOCK = threading.RLock()
STATE_DB = "state/smd.sqlite"
SETTINGS_FILE = "settings.yaml"

PULL_FRESH_S = 30  # a pull this recent is not repeated before a turn or a write
PULL_EVERY_S = 600  # the tick pulls this often, so a hand-added appointment gets its reminders
RETRY_S = 60  # after a failed push, the tick waits this long before trying again
GIT_TIMEOUT_S = 60

BOT_NAME = "SuperMedicalDoctor"
BOT_EMAIL = "supermedicaldoctor@localhost"
LOG_PREFIX = "log("  # a record from the chat: «log(<person>): <short list>»
UNDO_PREFIX = "undo("
SETTINGS_PREFIX = "settings: "  # never undone by /deshacer
EXPORT_PREFIX = "export: "


# --- SQLite kv -----------------------------------------------------------------------------

KV_KEYS = frozenset({"offset"})
KV_PREFIXES = (
    "conv:",
    "pending:",
    "said:",
    "options:",
    "ctx:",
    "remind:",
    "sent:",
    "undo:",
    "own:",
)
CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, ts INTEGER NOT NULL,
  role TEXT NOT NULL, who TEXT, text TEXT NOT NULL, body TEXT, mid INTEGER, asked INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_by_chat ON chat (chat, id);
CREATE TABLE IF NOT EXISTS reminder (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, uid INTEGER, at REAL NOT NULL,
  text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', by INTEGER, created INTEGER NOT NULL,
  thread INTEGER
);
"""


class KV:
    """One table `kv(key, value)`. It holds exactly `offset`, `conv:<chat>`, `pending:<chat>`,
    `said:<chat>` (the last answered messages, so an edit can be compared), `options:<chat>`
    (the choices under recent answers), `remind:<chat>` (offers waiting for a tap), `ctx:<chat>`
    (the last chat log row a turn took as context), `sent:<person>:<appointment>:<kind>` (a
    reminder the app posted by itself), `undo:<chat>` (the record commits made from that chat)
    and `own:<chat>` (the ids of the messages the bot sent there, so it can edit them); any
    other key is refused.

    Two more tables: `chat`, the chat log (every message of the chats the bot serves that it
    keeps, and its answers, for the assistant's recap), and `reminder`, the one-off reminders set
    from the chat. Safe across threads."""

    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))  # their words sit here
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._db.executescript(CHAT_SCHEMA)

    @staticmethod
    def _check(key: str) -> None:
        if key not in KV_KEYS and not key.startswith(KV_PREFIXES):
            raise ValueError("unknown_kv_key")

    def get(self, key: str, default: str | None = None) -> str | None:
        self._check(key)
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self._check(key)
        with self._lock:
            self._db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def delete(self, key: str) -> None:
        self._check(key)
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE key = ?", (key,))

    def keys(self, prefix: str) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT key FROM kv WHERE substr(key, 1, ?) = ? ORDER BY key", (len(prefix), prefix)
            ).fetchall()
        return [r[0] for r in rows]

    def get_json(self, key: str, default=None):
        raw = self.get(key)
        return default if raw is None else json.loads(raw)

    def set_json(self, key: str, value) -> None:
        self.set(key, json.dumps(value, ensure_ascii=False))

    def update_json(self, key: str, fn: Callable, default=None):
        """Read, change and write one value as a single step; `fn` returning None deletes it."""
        self._check(key)
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            value = fn(json.loads(row[0]) if row else default)
            if value is None:
                self._db.execute("DELETE FROM kv WHERE key = ?", (key,))
            else:
                self._db.execute(
                    "INSERT INTO kv (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
        return value

    # --- the chat log ------------------------------------------------------------------

    def add_chat(
        self,
        chat: int,
        role: str,
        who: str | None,
        text: str,
        ts: int,
        *,
        body: str | None = None,
        mid: int | None = None,
        asked: bool = True,
    ) -> int:
        """Log one line: `role` user or assistant; `asked` False for a group message that did
        not call the bot. Returns its row id."""
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO chat (chat, ts, role, who, text, body, mid, asked) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chat, int(ts), role, who, text, body, mid, int(bool(asked))),
            )
            return int(cur.lastrowid)

    def chat_rows(self, chat: int, since: int, upto: int | None = None) -> list[dict]:
        """The chat's rows written at `since` (epoch seconds) or later, with a row id up to
        `upto`, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, role, who, text, body, mid, asked FROM chat "
                "WHERE chat = ? AND ts >= ? AND id <= ? ORDER BY id",
                (chat, int(since), upto if upto is not None else 2**62),
            ).fetchall()
        keys = ("id", "ts", "role", "who", "text", "body", "mid", "asked")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def chat_last(self, chat: int) -> int:
        with self._lock:
            row = self._db.execute("SELECT MAX(id) FROM chat WHERE chat = ?", (chat,)).fetchone()
        return int(row[0] or 0)

    def chat_find(self, chat: int, mid: int) -> dict | None:
        """The newest user row of a Telegram message, or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, text, body, asked FROM chat WHERE chat = ? AND mid = ? AND role = "
                "'user' ORDER BY id DESC LIMIT 1",
                (chat, mid),
            ).fetchone()
        return dict(zip(("id", "text", "body", "asked"), row, strict=True)) if row else None

    def chat_edit(self, row: int, text: str, body: str, asked: bool | None = None) -> None:
        with self._lock:
            if asked is None:
                self._db.execute(
                    "UPDATE chat SET text = ?, body = ? WHERE id = ?", (text, body, row)
                )
            else:
                self._db.execute(
                    "UPDATE chat SET text = ?, body = ?, asked = ? WHERE id = ?",
                    (text, body, int(asked), row),
                )

    def chat_clear(self, chat: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM chat WHERE chat = ?", (chat,))

    def chat_prune(self, before: int) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM chat WHERE ts < ?", (int(before),)).rowcount

    # --- one-off reminders ------------------------------------------------------------

    def add_reminder(
        self, chat: int, uid: int | None, at: float, text: str, by, now: int, thread=None
    ) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO reminder (chat, uid, at, text, by, created, thread) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat, uid, float(at), text[:500], by, int(now), thread),
            )
            return int(cur.lastrowid)

    def reminders(self, chat: int | None = None, state: str = "pending") -> list[dict]:
        q = "SELECT id, chat, uid, at, text, state, thread FROM reminder WHERE state = ?"
        args: list = [state]
        if chat is not None:
            q += " AND chat = ?"
            args.append(chat)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY at, id", args).fetchall()
        keys = ("id", "chat", "uid", "at", "text", "state", "thread")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def due_reminders(self, now: float) -> list[dict]:
        return [r for r in self.reminders() if r["at"] <= now]

    def settle_reminder(self, rid: int, state: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE reminder SET state = ? WHERE id = ? AND state = 'pending'", (state, rid)
            )
            return cur.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._db.close()


# --- git -----------------------------------------------------------------------------------


class GitError(RuntimeError):
    """A failed git step. The message is a reason code, never git's output."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def git_env(token: str = "") -> dict[str, str]:
    """The environment for one git call. The token travels as an `http.extraHeader` in the
    environment, never on the command line, where any process could read it, nor in the
    checkout's config. Commits are authored by the bot."""
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME=BOT_NAME,
        GIT_AUTHOR_EMAIL=BOT_EMAIL,
        GIT_COMMITTER_NAME=BOT_NAME,
        GIT_COMMITTER_EMAIL=BOT_EMAIL,
        GIT_TERMINAL_PROMPT="0",
    )
    if token:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
        env["GIT_CONFIG_COUNT"] = str(n + 1)
        env[f"GIT_CONFIG_KEY_{n}"] = "http.extraHeader"
        env[f"GIT_CONFIG_VALUE_{n}"] = f"Authorization: token {token}"
    return env


def _write(path: Path, content: str | bytes) -> None:
    """Write through a temp file and `os.replace`, keeping the old mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(content, bytes):
        tmp.write_bytes(content)
    else:
        tmp.write_text(content, encoding="utf-8")
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)


class Repo:
    def __init__(
        self, data: str | Path, token: str = "", clock: Callable[[], float] = time.monotonic
    ):
        self.data = Path(data)
        self.token = token
        self.clock = clock
        self.pull_ok: bool | None = None  # the last pull worked; None: none tried yet
        self.pulled_at: float | None = None  # when (clock) a pull last worked
        self.settings_pulled = False  # a pull changed settings.yaml: the bot reloads it
        self.remote_ok: bool | None = None  # the startup `push --dry-run`, kept for /estado
        self.last_error: str | None = None
        self._tried_at: float | None = None  # when a pull was last tried
        self._retry_at: float | None = None

    # --- git plumbing --------------------------------------------------------------------

    def _git(self, *args: str, timeout: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.data), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=git_env(self.token),
            check=False,
        )

    def _run(self, *args: str, code: str) -> str:
        r = self._git(*args)
        if r.returncode != 0:
            raise GitError(code)
        return r.stdout

    def _read(self, rel: str) -> bytes | None:
        try:
            return (self.data / rel).read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            return None

    def is_checkout(self) -> bool:
        return (self.data / ".git").exists()

    def head(self) -> str | None:
        r = self._git("rev-parse", "--short", "HEAD")
        return r.stdout.strip() or None if r.returncode == 0 else None

    def check_remote(self) -> bool:
        """`git push --dry-run` at start; the answer is kept in memory for /estado."""
        try:
            self.remote_ok = self._git("push", "--dry-run", "--quiet").returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            self.remote_ok = False
        log.info("git push --dry-run %s", "ok" if self.remote_ok else "failed")
        return self.remote_ok

    def unpushed(self) -> int | None:
        """Commits on this branch that the remote does not have; None without an upstream."""
        r = self._git("rev-list", "--count", "@{u}..HEAD")
        return int(r.stdout) if r.returncode == 0 else None

    # --- pull ----------------------------------------------------------------------------

    def pull(self, fresh_s: float = PULL_FRESH_S) -> bool:
        """`git pull --rebase` unless a pull was tried in the last `fresh_s` seconds. With
        nothing of the bot's own waiting to be pushed it is a fast-forward. A failure aborts the
        rebase and keeps the last good copy: the bot answers from it and says so. Returns
        whether the copy on disk is the latest the bot could get."""
        with DATA_LOCK:
            now = self.clock()
            if self._tried_at is not None and now - self._tried_at < fresh_s:
                return bool(self.pull_ok)
            self._tried_at = now
            if not self.is_checkout():
                self.pull_ok = False
                return False
            before = self._read(SETTINGS_FILE)
            try:
                r = self._git("pull", "--rebase", "--autostash", "--quiet")
            except (subprocess.TimeoutExpired, OSError) as e:
                self._abort_rebase()
                self.pull_ok = False
                log.warning("git pull failed: %s", type(e).__name__)
                return False
            if r.returncode != 0:
                self._abort_rebase()
                self.pull_ok = False
                log.warning("git pull failed")
                return False
            self.pull_ok, self.pulled_at = True, now
            if self._read(SETTINGS_FILE) != before:
                self.settings_pulled = True
            return True

    def _abort_rebase(self) -> None:
        try:
            self._git("rebase", "--abort")  # fails harmlessly when no rebase is in progress
        except (subprocess.TimeoutExpired, OSError):
            pass

    # --- push ----------------------------------------------------------------------------

    def _push(self) -> bool:
        """Push; when the remote moved on, rebase on it once and push again. A push that still
        fails keeps the commit for the tick to push later."""
        for attempt in range(2):
            try:
                if self._git("push", "--quiet").returncode == 0:
                    self.last_error, self._retry_at = None, None
                    log.info("git push ok")
                    return True
                if attempt == 0:
                    r = self._git("pull", "--rebase", "--autostash", "--quiet")
                    if r.returncode != 0:
                        self._abort_rebase()
                        break
            except (subprocess.TimeoutExpired, OSError):
                self._abort_rebase()
                break
        self.last_error = "push_failed"
        self._retry_at = self.clock() + RETRY_S
        log.warning("git push failed")
        return False

    def flush(self, force: bool = False) -> str:
        """On the tick: push what an earlier push left behind. Returns `idle`, `backoff`,
        `pushed` or `failed`."""
        with DATA_LOCK:
            if not self.is_checkout():
                return "idle"
            if not force and self._retry_at is not None and self.clock() < self._retry_at:
                return "backoff"
            if not self.unpushed():
                return "idle"
            return "pushed" if self._push() else "failed"

    # --- writes --------------------------------------------------------------------------

    def commit(self, files: Mapping[str, str | bytes | None], message: str) -> str | None:
        """Write `files` (text or bytes, by path in the repo; None deletes the file), commit
        them alone as `message` and push. Pulls first unless a pull is fresh. Returns the commit's id, or None when
        nothing changed. A commit that fails puts every file back as it was and raises
        `GitError`; a push that fails keeps the commit for the tick."""
        with DATA_LOCK:
            self.pull()
            paths = list(files)
            old = {p: self._read(p) for p in paths}
            for p, content in files.items():
                if content is None:
                    (self.data / p).unlink(missing_ok=True)
                else:
                    _write(self.data / p, content)
            try:
                self._run("add", "-A", "--", *paths, code="add_failed")
                if self._git("diff", "--cached", "--quiet", "--", *paths).returncode == 0:
                    return None
                self._run("commit", "--quiet", "-m", message, "--", *paths, code="commit_failed")
            except (GitError, subprocess.TimeoutExpired, OSError):
                self._restore(old)
                raise
            sha = self._run("rev-parse", "HEAD", code="rev_parse_failed").strip()
            log.info("git commit ok files=%d", len(paths))
            self._push()
            return sha

    def _restore(self, old: Mapping[str, bytes | None]) -> None:
        for p, content in old.items():
            try:
                self._git("reset", "--quiet", "--", p)
            except (subprocess.TimeoutExpired, OSError):
                pass
            if content is None:
                (self.data / p).unlink(missing_ok=True)
            else:
                _write(self.data / p, content)

    def write_settings(self, fn: Callable[[str], str], message: str) -> str:
        """Rewrite `settings.yaml` with `fn` (old text to new text, raising `ValueError` to
        refuse), commit it alone as `settings: <message>` and push. Returns the new text."""
        with DATA_LOCK:
            self.pull()
            old = (self.data / SETTINGS_FILE).read_text(encoding="utf-8")
            new = fn(old)
            if new == old:
                return old
            self.commit({SETTINGS_FILE: new}, SETTINGS_PREFIX + message)
            return new

    def changed_by(self, sha: str) -> list[str]:
        """The paths a commit touched."""
        out = self._run("show", "--name-only", "--format=", sha, code="show_failed")
        return [x for x in out.splitlines() if x.strip()]

    def subject(self, sha: str) -> str | None:
        r = self._git("log", "-1", "--format=%s", sha)
        return r.stdout.strip() if r.returncode == 0 else None

    def revert(self, sha: str, regen: Callable[[], Mapping[str, str]], message: str) -> str | None:
        """`git revert` one commit of the bot's, without committing at once: the generated
        files (the exports) are written again from the reverted data, so a later commit that
        also changed them never blocks the undo. A conflict in anything else aborts it
        (`GitError("undo_conflict")`). Returns the new commit's id, None when there was nothing
        to undo, and pushes it."""
        with DATA_LOCK:
            self.pull()
            if self._git("merge-base", "--is-ancestor", sha, "HEAD").returncode != 0:
                return None
            generated = set(regen())
            r = self._git("revert", "--no-commit", sha)
            if r.returncode != 0:
                conflicts = self._git("diff", "--name-only", "--diff-filter=U").stdout.split()
                if not conflicts or any(c not in generated for c in conflicts):
                    self._git("revert", "--abort")
                    self._git("reset", "--quiet", "--merge")
                    raise GitError("undo_conflict")
            try:
                for path, text in regen().items():
                    _write(self.data / path, text)
                    self._run("add", "--", path, code="add_failed")
                if self._git("diff", "--cached", "--quiet").returncode == 0:
                    self._git("revert", "--abort")
                    return None
                self._run("commit", "--quiet", "-m", message, code="commit_failed")
            except (GitError, subprocess.TimeoutExpired, OSError):
                self._git("revert", "--abort")
                self._git("reset", "--quiet", "--merge")
                raise
            self._git("revert", "--quit")
            new = self._run("rev-parse", "HEAD", code="rev_parse_failed").strip()
            log.info("git revert ok")
            self._push()
            return new
