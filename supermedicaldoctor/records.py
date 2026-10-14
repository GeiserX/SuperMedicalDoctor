"""The data repository: `settings.yaml`, each person's files, and the `record` block.

Every person has a folder `people/<id>/` with `profile.yaml`, `meds.yaml`, `appointments.yaml`,
`labs.jsonl` and `documents/`. The app reads them before every turn and writes them only through a
`record` block the assistant sends (or a button), checked here all or nothing: an error is a
`BlockError` whose code never quotes a value, because it ends up in the service log.

YAML files are written by loading, changing and dumping one file (`allow_unicode`, no key
sorting); unknown keys are kept. `settings.yaml` is the exception: the app edits it by line, so its
comments survive (`set_lines`).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

SETTINGS_FILE = "settings.yaml"
PEOPLE_DIR = "people"
PERSON_FILES = ("profile.yaml", "meds.yaml", "appointments.yaml", "labs.jsonl")
DOCUMENTS = "documents"

SLOTS = ("waking", "breakfast", "lunch", "dinner", "bedtime", "as_needed")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MED_KINDS = ("medicine", "supplement")
STATUSES = ("pending", "done", "cancelled")
CHATS = ("group", "private")
CONDITION_STATUSES = ("active", "past", "suspected")
HISTORY_SCOPES = ("asked", "all")
HISTORY_DAYS_MAX = 60
SOURCE_KINDS = ("github", "gitea")
EXPORT_KINDS = ("chef",)
LAB_KEYS = ("date", "test", "value", "unit", "low", "high", "lab", "doc", "source")
DOC_EXTS = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/heif": ".heic",
}
PROFILE_LISTS = ("allergies", "intolerances", "conditions", "diet", "doctors", "notes")
NOTES_MAX, NOTE_CHARS = 30, 300
INSTRUCTIONS_MAX = 4000
EDITABLE_SUFFIXES = (".md", ".txt", ".yaml", ".yml", ".json")
EDITABLE_NEVER = (
    "plugins",
    "deploy",
    "tests",
    "export",
    "state",
    ".git",
    ".github",
    ".beads",
    PEOPLE_DIR,
)

_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_HHMM = re.compile(r"(\d{1,2}):(\d{2})")


class BlockError(Exception):
    """A `record` or `settings` block that cannot be applied. The message is a reason code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- small parsers -------------------------------------------------------------------------


def hhmm(value: object) -> clock_time:
    """`"HH:MM"` as a time; raises `ValueError("bad_time")`."""
    m = _HHMM.fullmatch(value) if isinstance(value, str) else None
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("bad_time")
    return clock_time(int(m.group(1)), int(m.group(2)))


def clock_text(value: object) -> object:
    """YAML 1.1 reads an unquoted 20:00 as the integer 1200: give it back as "20:00"."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 24 * 60:
        return f"{value // 60:02d}:{value % 60:02d}"
    return value


def as_date(value: object) -> date | None:
    """A YAML date or an ISO string as a date; None for anything else."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return None


def slug(text: str, limit: int = 40) -> str:
    """«Hierro bisglicinato» -> «hierro-bisglicinato»: ASCII, lowercase, words joined by «-»."""
    folded = unicodedata.normalize("NFKD", str(text))
    ascii_ = "".join(c for c in folded if not unicodedata.combining(c)).lower()
    words = re.findall(r"[a-z0-9]+", ascii_)
    out = ""
    for w in words:
        nxt = f"{out}-{w}" if out else w
        if len(nxt) > limit:
            break
        out = nxt
    return out or (words[0][:limit] if words else "x")


def is_fasting(text: str) -> bool:
    """Whether a preparation line means not eating («En ayunas», «fasting»)."""
    folded = unicodedata.normalize("NFKD", str(text).casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return any(w in folded for w in ("ayun", "sin comer", "no comer", "fasting", "fast "))


# --- settings.yaml -------------------------------------------------------------------------


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    user_id: int | None
    username: str | None = None
    calendar: str | None = None


@dataclass(frozen=True)
class Settings:
    """The keys of `settings.yaml` the app reads; unknown keys are ignored. Values that do not
    parse are kept as found and reported by `settings_errors`."""

    timezone: str = "UTC"
    language: str = "en"
    chat_id: object = None
    thread_id: object = None
    admins: tuple = ()
    history_scope: str = "asked"
    history_days: object = 14
    people: Mapping[str, Person] = field(default_factory=dict)
    people_raw: Mapping = field(default_factory=dict)
    rules: tuple = ()
    day_before_at: object = "20:00"
    followup: object = True
    quiet: object = ("23:00", "07:00")
    calendar_appointments: object = False
    calendar_reminders: object = False
    assistant: Mapping | None = None
    sources: tuple = ()
    exports: tuple = ()
    editable: tuple = ()  # docs the assistant may edit, beside each person's folder

    @classmethod
    def from_dict(cls, raw: object) -> Settings:
        if not isinstance(raw, Mapping):
            raise ValueError("bad_settings")  # noqa: TRY004 - bad data, one exception type
        tg = raw.get("telegram") if isinstance(raw.get("telegram"), Mapping) else {}
        people_raw = raw.get("people") if isinstance(raw.get("people"), Mapping) else {}
        people = {}
        for pid, p in people_raw.items():
            if not isinstance(p, Mapping):
                continue
            username = p.get("username")
            people[str(pid)] = Person(
                id=str(pid),
                name=str(p.get("name") or pid),
                user_id=_int(p.get("user_id")),
                username=str(username).lstrip("@") if username else None,
                calendar=str(p["calendar"]) if p.get("calendar") else None,
            )
        rem = raw.get("reminders") if isinstance(raw.get("reminders"), Mapping) else {}
        cal = raw.get("calendar") if isinstance(raw.get("calendar"), Mapping) else {}
        quiet = rem.get("quiet", ("23:00", "07:00"))
        if isinstance(quiet, list | tuple):
            quiet = tuple(clock_text(q) for q in quiet)
        block = raw.get("assistant")
        sources = raw.get("sources") or ()
        exports = raw.get("exports")
        if exports is None:
            exports = [{"kind": "chef", "path": "export/chef.yaml"}]
        admins = tg.get("admins") or ()
        editable = raw.get("editable") or ()
        return cls(
            timezone=str(raw.get("timezone") or "UTC"),
            language=str(raw.get("language") or "en"),
            chat_id=tg.get("chat_id"),
            thread_id=tg.get("thread_id"),
            admins=tuple(str(a) for a in admins) if isinstance(admins, list | tuple) else admins,
            history_scope=str(tg.get("history_scope") or "asked"),
            history_days=tg.get("history_days", 14),
            people=people,
            people_raw=dict(people_raw),
            rules=tuple(str(r) for r in raw.get("rules") or () if isinstance(r, str)),
            day_before_at=clock_text(rem.get("day_before_at", "20:00")),
            followup=rem.get("followup", True),
            quiet=quiet,
            calendar_appointments=cal.get("appointments", False),
            calendar_reminders=cal.get("reminders", False),
            assistant=dict(block) if isinstance(block, Mapping) else None,
            sources=tuple(sources) if isinstance(sources, list | tuple) else (sources,),
            exports=tuple(exports) if isinstance(exports, list | tuple) else (exports,),
            editable=tuple(editable) if isinstance(editable, list | tuple) else (editable,),
        )

    # convenience, once `settings_errors` said the values are good

    @property
    def chat(self) -> int | None:
        return _int(self.chat_id)

    @property
    def thread(self) -> int | None:
        return _int(self.thread_id)

    @property
    def days(self) -> int:
        n = _int(self.history_days)
        return n if n is not None else 14

    def by_user(self, user_id: object) -> Person | None:
        for p in self.people.values():
            if p.user_id is not None and p.user_id == user_id:
                return p
        return None

    def is_admin(self, pid: str | None) -> bool:
        return pid is not None and pid in tuple(self.admins)

    def plugin(self) -> str | None:
        value = (self.assistant or {}).get("plugin")
        return str(value) if value else None

    def export_paths(self) -> list[str]:
        return [str(e["path"]) for e in self.exports if isinstance(e, Mapping) and e.get("path")]


def load_settings(data: str | Path) -> Settings:
    path = Path(data) / SETTINGS_FILE
    return Settings.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def _inside(root: Path, rel: object) -> bool:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
        return False
    parts = rel.split("/")
    return ".." not in parts and "" not in parts and (root / rel).resolve().is_relative_to(root)


def settings_errors(s: Settings, data: str | Path) -> list[str]:
    """What keeps the bot from running on these settings, as reason codes."""
    root = Path(data).resolve()
    errors = []
    try:
        ZoneInfo(s.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append("bad_timezone")
    if _int(s.chat_id) is None:
        errors.append("bad_chat_id")
    if s.thread_id is not None and _int(s.thread_id) is None:
        errors.append("bad_thread_id")
    if not s.people:
        errors.append("no_people")
    ids_seen: set[int] = set()
    for pid, raw in s.people_raw.items():
        if not isinstance(raw, Mapping) or not _ID.fullmatch(str(pid)):
            errors.append("bad_person_id")
            continue
        uid = raw.get("user_id")
        if isinstance(uid, bool) or not isinstance(uid, int):
            errors.append(f"bad_user_id: {pid}")
        elif uid in ids_seen:
            errors.append(f"duplicate_user_id: {pid}")
        else:
            ids_seen.add(uid)
    if not isinstance(s.admins, tuple) or any(a not in s.people for a in s.admins):
        errors.append("bad_admins")
    if s.history_scope not in HISTORY_SCOPES:
        errors.append("bad_history_scope")
    days = s.history_days
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= HISTORY_DAYS_MAX:
        errors.append("bad_history_days")
    try:
        hhmm(s.day_before_at)
    except ValueError:
        errors.append("bad_day_before_at")
    quiet = s.quiet
    try:
        if not isinstance(quiet, tuple) or len(quiet) != 2:
            raise ValueError
        hhmm(quiet[0])
        hhmm(quiet[1])
    except ValueError:
        errors.append("bad_quiet")
    for key in ("followup", "calendar_appointments", "calendar_reminders"):
        if not isinstance(getattr(s, key), bool):
            errors.append(f"bad_{key}")
    plugin = (s.assistant or {}).get("plugin")
    if plugin:
        path = (root / str(plugin)).resolve()
        if root not in path.parents:
            errors.append("plugin_outside_data")
        elif not path.is_file():
            errors.append("plugin_missing")
    instructions = (s.assistant or {}).get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        errors.append("bad_instructions")
    names = set()
    for src in s.sources:
        code = _source_error(src, s, root)
        if code:
            errors.append(code)
        elif src["name"] in names:
            errors.append("duplicate_source")
        else:
            names.add(src["name"])
    for ex in s.exports:
        ok = isinstance(ex, Mapping) and ex.get("kind") in EXPORT_KINDS
        if not ok or not _inside(root, ex.get("path")):
            errors.append("bad_export")
    for path in s.editable:
        if (
            not _inside(root, path)
            or not str(path).lower().endswith(EDITABLE_SUFFIXES)
            or str(path).split("/")[0] in EDITABLE_NEVER
            or path
            in (
                SETTINGS_FILE,
                ".gitignore",
                "docs/DESIGN.md",
                "docs/CONTRACT.md",
            )
        ):
            errors.append("bad_editable")
    return errors


def _source_error(src: object, s: Settings, root: Path) -> str | None:
    if not isinstance(src, Mapping):
        return "bad_source"
    name = src.get("name")
    if not isinstance(name, str) or not _ID.fullmatch(name):
        return "bad_source_name"
    if src.get("kind") not in SOURCE_KINDS:
        return f"bad_source_kind: {name}"
    repo = src.get("repo")
    if not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return f"bad_source_repo: {name}"
    if src.get("kind") == "gitea":
        base = src.get("base_url")
        if not isinstance(base, str) or not re.match(r"https?://[^/\s]+", base):
            return f"bad_source_base_url: {name}"
    if src.get("ref") is not None and not isinstance(src.get("ref"), str):
        return f"bad_source_ref: {name}"
    if src.get("token_env") is not None and not isinstance(src.get("token_env"), str):
        return f"bad_source_token_env: {name}"
    files = src.get("files")
    if not isinstance(files, list) or not files:
        return f"bad_source_files: {name}"
    for f in files:
        if not isinstance(f, Mapping) or not _inside(root, f.get("path")):
            return f"bad_source_path: {name}"
        who = f.get("person")
        who = who if isinstance(who, list) else [who]
        if not who or any(w not in s.people for w in who):
            return f"bad_source_person: {name}"
    return None


def file_people(f: Mapping) -> list[str]:
    who = f.get("person")
    return [str(w) for w in (who if isinstance(who, list) else [who]) if w is not None]


def set_lines(text: str, values: Mapping[tuple[str, str], object]) -> str:
    """`settings.yaml` with each `section: key:` value replaced, by line: every comment and every
    other line stays byte-identical. A key that is not there is added at the end of its section,
    and a section that is not there at the end of the file. A string is written as a YAML
    double-quoted scalar on one line; a block scalar (`|`, `>`) it replaces goes with it."""
    lines = text.splitlines(keepends=True)
    for (section, key), value in values.items():
        start = next(
            (
                i
                for i, x in enumerate(lines)
                if re.fullmatch(rf"{section}:[ \t]*(#.*)?", x.rstrip())
            ),
            None,
        )
        if start is None:
            if any(re.match(rf"{section}:", x) for x in lines):
                raise ValueError("flow_section")  # `section: {...}`: not edited by line
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines += [f"{section}:\n", f"  {key}: {_scalar(value)}\n"]
            continue
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][:1] in " \t#"):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1  # trailing blank lines belong to no section
        child = re.compile(
            rf"(?P<ind>[ \t]+){re.escape(key)}:(?P<sp>[ \t]*)(?P<val>[^#\r\n]*?)"
            rf"(?P<rest>[ \t]+#.*)?"
        )
        indent = "  "
        for i in range(start + 1, end):
            m = re.match(r"[ \t]+(?=\S)", lines[i])
            if m and not lines[i].lstrip().startswith("#"):
                indent = m.group(0)
                break
        done = False
        for i in range(start + 1, end):
            body = lines[i].rstrip("\r\n")
            m = child.fullmatch(body)
            if m is None or m["ind"] != indent:
                continue
            j = i + 1
            if m["val"].strip()[:1] in ("|", ">"):
                while j < end and (
                    not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > len(indent)
                ):
                    j += 1
            eol = lines[i][len(body) :] or "\n"
            rendered = _scalar(value, indent)
            if "\n" in rendered:  # a block scalar: the comment goes on the key's line
                head, _, tail = rendered.partition("\n")
                rendered = f"{head}{m['rest'] or ''}\n{tail}"
                lines[i:j] = [f"{indent}{key}:{m['sp'] or ' '}{rendered}{eol}"]
            else:
                lines[i:j] = [f"{indent}{key}:{m['sp'] or ' '}{rendered}{m['rest'] or ''}{eol}"]
            done = True
            break
        if not done:
            lines.insert(end, f"{indent}{key}: {_scalar(value, indent)}\n")
    return "".join(lines)


def _scalar(value: object, indent: str = "  ") -> str:
    """A YAML scalar for `set_lines`. A text of several lines becomes a `|-` block scalar
    indented under its key, when it can round-trip as one (no line that starts with a space,
    no trailing spaces, no tabs); anything else is a double-quoted string on one line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    lines = text.split("\n")
    if (
        len(lines) > 1
        and lines[0].strip()
        and all(x == x.strip() for x in lines)
        and "\t" not in text
        and not text.endswith("\n")
    ):
        inner = indent + "  "
        return "|-\n" + "\n".join(inner + x if x else "" for x in lines)
    return json.dumps(text, ensure_ascii=False)


SETTINGS_BLOCK_KEYS = {
    ("reminders", "day_before_at"),
    ("reminders", "followup"),
    ("assistant", "instructions"),
    ("assistant", "instructions_add"),
}
INSTRUCTIONS = ("assistant", "instructions")
INSTRUCTIONS_ADD = ("assistant", "instructions_add")
ADD_LINES_MAX, ADD_LINE_CHARS = 20, 500  # lines one `instructions_add` may append


def validate_settings(block: object, admin: bool) -> dict[tuple[str, str], object]:
    """A `settings` block, all or nothing: `{"reminders": {"day_before_at": "HH:MM",
    "followup": bool}, "assistant": {"instructions": str, "instructions_add": [str]}}`, the
    `assistant` keys for admins only. `instructions` is the whole new text, at most 4,000
    characters; `instructions_add` lists lines to append, and the two never come together.
    Dotted keys (`"reminders.followup"`) are accepted too."""
    if not isinstance(block, Mapping) or not block:
        raise BlockError("bad_settings")
    flat: dict[tuple[str, str], object] = {}
    for k, v in block.items():
        if isinstance(v, Mapping) and "." not in str(k):
            for kk, vv in v.items():
                flat[(str(k), str(kk))] = vv
        elif "." in str(k):
            a, _, b = str(k).partition(".")
            flat[(a, b)] = v
        else:
            raise BlockError("unknown_key")
    if not flat or any(k not in SETTINGS_BLOCK_KEYS for k in flat):
        raise BlockError("unknown_key")
    out: dict[tuple[str, str], object] = {}
    for k, v in flat.items():
        if k == ("reminders", "day_before_at"):
            try:
                out[k] = hhmm(v).strftime("%H:%M")
            except ValueError:
                raise BlockError("bad_day_before_at") from None
        elif k == ("reminders", "followup"):
            if not isinstance(v, bool):
                raise BlockError("bad_followup")
            out[k] = v
        else:
            if not admin:
                raise BlockError("admins_only")
            if k == INSTRUCTIONS_ADD:
                lines = v if isinstance(v, list) else None
                if (
                    not lines
                    or len(lines) > ADD_LINES_MAX
                    or any(not isinstance(x, str) or not x.strip() for x in lines)
                    or any(len(x) > ADD_LINE_CHARS for x in lines)
                ):
                    raise BlockError("bad_instructions")
                out[k] = [x.strip() for x in lines]
            elif not isinstance(v, str) or len(v) > INSTRUCTIONS_MAX:
                raise BlockError("bad_instructions")
            else:
                out[k] = v
    if INSTRUCTIONS in out and INSTRUCTIONS_ADD in out:
        raise BlockError("bad_instructions")
    return out


def instruction_lines(text: str) -> list[str]:
    """The non-empty lines of an instructions text, stripped."""
    return [x.strip() for x in str(text or "").splitlines() if x.strip()]


def add_instructions(current: str, lines: list[str]) -> str:
    """`current` with `lines` appended, each once: a line already there is not repeated."""
    out = instruction_lines(current)
    seen = {x.casefold() for x in out}
    for line in lines:
        if line.casefold() not in seen:
            out.append(line)
            seen.add(line.casefold())
    return "\n".join(out)


def instruction_diff(old: str, new: str) -> tuple[list[str], list[str]]:
    """The lines `new` adds and the lines it drops, as `(added, removed)`, each in its own
    order. A line repeated counts once per copy; blank lines and indentation do not count."""
    from collections import Counter

    before, after = instruction_lines(old), instruction_lines(new)
    gone = Counter(before) - Counter(after)
    came = Counter(after) - Counter(before)
    removed = []
    for x in before:
        if gone[x] > 0:
            removed.append(x)
            gone[x] -= 1
    added = []
    for x in after:
        if came[x] > 0:
            added.append(x)
            came[x] -= 1
    return added, removed


# --- a person's files ----------------------------------------------------------------------


@dataclass
class PersonData:
    id: str
    profile: dict = field(default_factory=dict)
    meds: list = field(default_factory=list)
    appointments: list = field(default_factory=list)
    labs: list = field(default_factory=list)

    def doctor_ids(self) -> set[str]:
        return {
            str(d.get("id")) for d in self.profile.get("doctors") or [] if isinstance(d, Mapping)
        }

    def doctor(self, did: object) -> dict | None:
        for d in self.profile.get("doctors") or []:
            if isinstance(d, Mapping) and d.get("id") == did:
                return dict(d)
        return None


def person_dir(pid: str) -> str:
    return f"{PEOPLE_DIR}/{pid}"


def _yaml_file(path: Path, default):
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError(f"bad_yaml: {path.name}") from None
    return default if value is None else value


def load_labs_text(text: str) -> list[dict]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            raise ValueError(f"bad_json: labs.jsonl line {n}") from None
        if not isinstance(obj, dict):
            raise ValueError(f"bad_line: labs.jsonl line {n}")  # noqa: TRY004 - bad data, one exception type
        out.append(obj)
    return out


def load_person(data: str | Path, pid: str) -> PersonData:
    """One person's files; a missing file is empty. Raises `ValueError` with a reason code when
    a file does not parse."""
    base = Path(data) / person_dir(pid)
    profile = _yaml_file(base / "profile.yaml", {})
    meds = _yaml_file(base / "meds.yaml", [])
    appts = _yaml_file(base / "appointments.yaml", [])
    if not isinstance(profile, dict):
        raise ValueError("bad_profile: profile.yaml")  # noqa: TRY004 - bad data, one exception type
    if not isinstance(meds, list):
        raise ValueError("not_a_list: meds.yaml")  # noqa: TRY004 - bad data, one exception type
    if not isinstance(appts, list):
        raise ValueError("not_a_list: appointments.yaml")  # noqa: TRY004 - bad data, one exception type
    try:
        labs_text = (base / "labs.jsonl").read_text(encoding="utf-8")
    except FileNotFoundError:
        labs_text = ""
    return PersonData(pid, profile, meds, appts, load_labs_text(labs_text))


def person_errors(pd: PersonData) -> list[str]:
    """What `smd check` reports about one person's files: `<file> item <n>: <code>`."""
    errors = []
    seen: set = set()
    for n, m in enumerate(pd.meds, 1):
        where = f"meds.yaml item {n}"
        if not isinstance(m, Mapping):
            errors.append(f"{where}: not_a_mapping")
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not _ID.fullmatch(mid):
            errors.append(f"{where}: bad_id")
        elif mid in seen:
            errors.append(f"{where}: duplicate_id")
        seen.add(mid)
        if not isinstance(m.get("name"), str) or not m.get("name"):
            errors.append(f"{where}: missing_name")
        if m.get("kind") not in MED_KINDS:
            errors.append(f"{where}: bad_kind")
        if m.get("slot") is not None and m.get("slot") not in SLOTS:
            errors.append(f"{where}: bad_slot")
        if not _days_ok(m.get("days", "daily")):
            errors.append(f"{where}: bad_days")
        for key in ("since", "until"):
            if m.get(key) is not None and as_date(m.get(key)) is None:
                errors.append(f"{where}: bad_{key}")
        if m.get("prescribed_by") is not None and m["prescribed_by"] not in pd.doctor_ids():
            errors.append(f"{where}: unknown_doctor")
    seen = set()
    for n, a in enumerate(pd.appointments, 1):
        where = f"appointments.yaml item {n}"
        if not isinstance(a, Mapping):
            errors.append(f"{where}: not_a_mapping")
            continue
        aid = a.get("id")
        if not isinstance(aid, str) or not aid:
            errors.append(f"{where}: bad_id")
        elif aid in seen:
            errors.append(f"{where}: duplicate_id")
        seen.add(aid)
        if as_date(a.get("date")) is None:
            errors.append(f"{where}: bad_date")
        if a.get("time") is not None:
            try:
                hhmm(clock_text(a.get("time")))
            except ValueError:
                errors.append(f"{where}: bad_time")
        if not isinstance(a.get("what"), str) or not a.get("what"):
            errors.append(f"{where}: missing_what")
        if a.get("status", "pending") not in STATUSES:
            errors.append(f"{where}: bad_status")
        if a.get("chat", "group") not in CHATS:
            errors.append(f"{where}: bad_chat")
        if a.get("doctor") is not None and a["doctor"] not in pd.doctor_ids():
            errors.append(f"{where}: unknown_doctor")
        try:
            _prep(a.get("prep") or [])
        except BlockError as e:
            errors.append(f"{where}: {e.code}")
    for n, lab in enumerate(pd.labs, 1):
        where = f"labs.jsonl line {n}"
        if as_date(lab.get("date")) is None:
            errors.append(f"{where}: bad_date")
        if not isinstance(lab.get("test"), str) or not lab.get("test"):
            errors.append(f"{where}: missing_test")
        value = lab.get("value")
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            errors.append(f"{where}: bad_value")
    p = pd.profile
    for key in PROFILE_LISTS:
        if p.get(key) is not None and not isinstance(p.get(key), list):
            errors.append(f"profile.yaml {key}: not_a_list")
    if p.get("birth_date") is not None and as_date(p.get("birth_date")) is None:
        errors.append("profile.yaml: bad_birth_date")
    return errors


def _days_ok(days: object) -> bool:
    if days == "daily":
        return True
    return isinstance(days, list) and bool(days) and all(d in WEEKDAYS for d in days)


def active(med: Mapping, today: date) -> bool:
    """A medicine or supplement still taken: no `until`, or one in the future."""
    until = as_date(med.get("until"))
    return until is None or until > today


def appt_time(a: Mapping) -> clock_time | None:
    value = clock_text(a.get("time"))
    if value is None:
        return None
    try:
        return hhmm(value)
    except ValueError:
        return None


def appt_start(a: Mapping, tz: str) -> datetime | None:
    """The appointment's start, aware, or None without a time."""
    d, t = as_date(a.get("date")), appt_time(a)
    if d is None or t is None:
        return None
    return datetime.combine(d, t, tzinfo=ZoneInfo(tz))


def before(start: datetime, hours: int) -> datetime:
    """`hours` real hours before `start`, in `start`'s zone. Subtracting from an aware datetime
    counts wall-clock hours, which is an hour off across a daylight-saving change."""
    return (start.astimezone(UTC) - timedelta(hours=hours)).astimezone(start.tzinfo)


def dump_yaml(value: object) -> str:
    return yaml.safe_dump(
        value, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096
    )


def dump_labs(labs: Sequence[Mapping]) -> str:
    """`labs.jsonl`: one value per line, sorted by date then test, keys in the contract's
    order, empty optional keys left out."""
    rows = []
    for lab in labs:
        row = {}
        for key in LAB_KEYS:
            value = lab.get(key)
            if key == "date":
                value = (as_date(value) or value).isoformat() if as_date(value) else value
            if value is None or value == "":
                continue
            row[key] = value
        for key, value in lab.items():  # unknown keys are kept, after the known ones
            if key not in LAB_KEYS and value is not None and value != "":
                row[key] = value
        rows.append(row)
    rows.sort(key=lambda r: (str(r.get("date", "")), str(r.get("test", "")).casefold()))
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


# --- the record block ----------------------------------------------------------------------

LIMITS = {"appointments": 20, "meds": 30, "labs": 300, "documents": 10}
CHARS = {
    "what": 120,
    "place": 200,
    "name": 120,
    "dose": 60,
    "food": 300,
    "why": 200,
    "outcome": 500,
    "text": 200,
    "test": 80,
    "unit": 20,
    "lab": 120,
    "title": 120,
    "value": 40,
}
APPT_FIELDS = {"date", "time", "what", "doctor", "place", "prep"}
MED_FIELDS = {"name", "kind", "dose", "slot", "days", "food", "why", "prescribed_by"}
LAB_FIELDS = {"date", "test", "value", "unit", "low", "high", "lab", "doc"}


@dataclass(frozen=True)
class Attachment:
    name: str  # "inbox/<message id>-<safe name>"
    mime: str
    data: bytes
    who: str | None = None  # the person who sent it


@dataclass
class Applied:
    """What one `record` block changed: the files to write (text, or bytes for a document), what
    the «Anotado:» lines say, the appointments it added (offered for the calendar) and the
    commit's short list."""

    person: str
    files: dict = field(default_factory=dict)
    items: list = field(default_factory=list)
    added: list = field(default_factory=list)
    summary: list = field(default_factory=list)
    data: PersonData | None = None  # the person's files after the block

    def __bool__(self) -> bool:
        return bool(self.items)


def _text(value: object, key: str, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise BlockError(f"missing_{key}")
        return None
    if not isinstance(value, str):
        raise BlockError(f"bad_{key}")
    value = " ".join(value.split())
    if len(value) > CHARS.get(key, 300):
        raise BlockError(f"too_long_{key}")
    return value


def _date(value: object, key: str = "date") -> date:
    d = as_date(value) if isinstance(value, str) else None
    if d is None:
        raise BlockError(f"bad_{key}")
    return d


def _prep(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > 5:
        raise BlockError("bad_prep")
    out = []
    for p in value:
        if not isinstance(p, Mapping) or set(p) - {"hours_before", "text"}:
            raise BlockError("bad_prep")
        hours = p.get("hours_before")
        if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 336:
            raise BlockError("bad_prep")
        out.append({"hours_before": hours, "text": _text(p.get("text"), "text", True)})
    return out


def _appt_id(d: date, t: str | None, what: str, taken: set[str]) -> str:
    base = f"{d.isoformat()}-{t.replace(':', '') if t else 'x'}-{slug(what, 20).split('-')[0]}"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


def _med_id(name: str, taken: set[str]) -> str:
    base = slug(name)
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


def apply_record(
    block: object,
    *,
    people: Mapping[str, PersonData],
    scope: Sequence[str],
    asker: str | None,
    private: bool,
    attachments: Mapping[str, Attachment],
    today: date,
    read: Callable[[str], bytes | None] = lambda path: None,
) -> Applied:
    """Check one `record` block and work out what it changes, all or nothing. Nothing is written
    here: `Applied.files` holds the new texts and bytes. `read(path)` gives the bytes of a file
    already in the repo (a document filed before), or None. Raises `BlockError` with a reason
    code."""
    if not isinstance(block, Mapping):
        raise BlockError("bad_block")
    allowed = {"person", "appointments", "meds", "labs", "documents", "profile"}
    if set(block) - allowed:
        raise BlockError("unknown_key")
    pid = block.get("person")
    if not isinstance(pid, str) or pid not in people or pid not in scope:
        raise BlockError("person_out_of_scope")
    if private and pid != asker:
        raise BlockError("not_the_asker")
    for key, limit in LIMITS.items():
        value = block.get(key)
        if value is not None and (not isinstance(value, list) or len(value) > limit):
            raise BlockError(f"bad_{key}")
    old = people[pid]
    if any(not isinstance(x, dict) for x in (*old.meds, *old.appointments, *old.labs)):
        raise BlockError("bad_file")  # a hand edit left an item that is not a mapping
    pd = PersonData(
        pid, _copy(old.profile), _copy(old.meds), _copy(old.appointments), _copy(old.labs)
    )
    out = Applied(pid)
    base = person_dir(pid)
    chat = "private" if private else "group"

    docs = _documents(block, attachments, out, base, read)
    _profile_ops(block.get("profile"), pd, out)
    _appointments(block.get("appointments") or [], pd, out, chat)
    _meds(block.get("meds") or [], pd, out, today)
    _labs(block.get("labs") or [], pd, out, today, docs, attachments, base)

    if pd.profile != old.profile:
        out.files[f"{base}/profile.yaml"] = dump_yaml(pd.profile)
    if pd.meds != old.meds:
        out.files[f"{base}/meds.yaml"] = dump_yaml(pd.meds)
    if pd.appointments != old.appointments:
        out.files[f"{base}/appointments.yaml"] = dump_yaml(pd.appointments)
    if pd.labs != old.labs:
        out.files[f"{base}/labs.jsonl"] = dump_labs(pd.labs)
    out.data = pd
    return out


def _copy(value):
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def _documents(block, attachments, out: Applied, base: str, read) -> dict[str, str]:
    """Copy the turn's attachments the block names into `documents/`; returns attachment name
    -> its path in the repo, for the lab lines that point at it."""
    titles: dict[str, tuple[date, str]] = {}
    for d in block.get("documents") or []:
        if not isinstance(d, Mapping) or set(d) - {"file", "date", "title"}:
            raise BlockError("bad_documents")
        name = d.get("file")
        if not isinstance(name, str) or name not in attachments:
            raise BlockError("not_an_attachment")
        titles[name] = (_date(d.get("date")), _text(d.get("title"), "title", True))
    for lab in block.get("labs") or []:
        name = lab.get("doc") if isinstance(lab, Mapping) else None
        if name is None:
            continue
        if not isinstance(name, str) or name not in attachments:
            raise BlockError("not_an_attachment")
        if name not in titles:
            stem = Path(name).stem.split("-", 1)[-1] or "doc"
            titles[name] = (_date(lab.get("date")), stem)
    paths: dict[str, str] = {}
    for name, (d, title) in titles.items():
        att = attachments[name]
        ext = Path(name).suffix.lower() or DOC_EXTS.get(att.mime, "")
        if ext not in (".pdf", ".jpg", ".jpeg", ".png", ".heic"):
            raise BlockError("bad_document_kind")
        stem = f"{d.isoformat()}-{slug(title)}"
        path, n = f"{base}/{DOCUMENTS}/{stem}{ext}", 2
        while True:
            there = out.files.get(path, read(path))
            if there is None or there == att.data:
                break
            path, n = f"{base}/{DOCUMENTS}/{stem}-{n}{ext}", n + 1
        paths[name] = path
        if out.files.get(path, read(path)) != att.data:
            out.files[path] = att.data
            out.items.append(("doc", {"title": title, "date": d, "path": path}))
            out.summary.append(f"document {Path(path).name}")
    return paths


def _profile_item(key: str, value: object):
    """One item of a profile list, checked: strings for intolerances, diet and notes; mappings
    (or a bare string) for allergies, conditions and doctors."""
    if key in ("intolerances", "diet", "notes"):
        limit = NOTE_CHARS if key == "notes" else 300
        text = value if isinstance(value, str) else None
        if not text or len(text) > limit:
            raise BlockError(f"bad_{key}")
        return " ".join(text.split())
    if isinstance(value, str):
        value = {"allergies": {"what": value}, "conditions": {"name": value}}.get(key, value)
    if not isinstance(value, Mapping):
        raise BlockError(f"bad_{key}")
    item = {}
    if key == "allergies":
        if set(value) - {"what", "reaction", "since"}:
            raise BlockError("bad_allergies")
        item["what"] = _text(value.get("what"), "what", True)
        for k in ("reaction", "since"):
            if value.get(k) is not None:
                item[k] = value[k] if isinstance(value[k], int) else _text(value[k], "text")
    elif key == "conditions":
        if set(value) - {"name", "since", "status"}:
            raise BlockError("bad_conditions")
        item["name"] = _text(value.get("name"), "name", True)
        if value.get("since") is not None:
            since = value["since"]
            item["since"] = since if isinstance(since, int) else _text(since, "text")
        status = value.get("status", "active")
        if status not in CONDITION_STATUSES:
            raise BlockError("bad_conditions")
        item["status"] = status
    else:  # doctors
        if set(value) - {"id", "name", "specialty", "center", "phone"}:
            raise BlockError("bad_doctors")
        did = value.get("id")
        if not isinstance(did, str) or not _ID.fullmatch(did):
            raise BlockError("bad_doctors")
        item["id"] = did
        item["name"] = _text(value.get("name"), "name", True)
        for k in ("specialty", "center", "phone"):
            item[k] = _text(value.get(k), "text") if value.get(k) is not None else None
    return item


def _same_item(key: str, a: object, b: object) -> bool:
    field_ = {"allergies": "what", "conditions": "name", "doctors": "id"}.get(key)
    if field_ and isinstance(a, Mapping) and isinstance(b, Mapping):
        return str(a.get(field_, "")).casefold() == str(b.get(field_, "")).casefold()
    if field_ and isinstance(a, Mapping) and isinstance(b, str):
        return str(a.get(field_, "")).casefold() == b.casefold()
    if isinstance(a, str) and isinstance(b, str):
        return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()
    return a == b


def _profile_ops(ops: object, pd: PersonData, out: Applied) -> None:
    if ops is None:
        return
    if not isinstance(ops, Mapping) or set(ops) - set(PROFILE_LISTS):
        raise BlockError("bad_profile")
    for key, change in ops.items():
        if not isinstance(change, Mapping) or set(change) - {"add", "remove"}:
            raise BlockError("bad_profile")
        current = pd.profile.get(key)
        current = list(current) if isinstance(current, list) else []
        for raw in change.get("remove") or []:
            keep = [x for x in current if not _same_item(key, x, raw)]
            if len(keep) != len(current):
                gone = next(x for x in current if _same_item(key, x, raw))
                out.items.append(("profile_remove", {"key": key, "value": gone}))
                out.summary.append(f"profile {key} remove")
            current = keep
        for raw in change.get("add") or []:
            item = _profile_item(key, raw)
            if any(_same_item(key, x, item) for x in current):
                if key != "doctors" or item in current:
                    continue
                current = [x for x in current if not _same_item(key, x, item)]
            current.append(item)
            out.items.append(("profile_add", {"key": key, "value": item}))
            out.summary.append(f"profile {key} add")
        if key == "notes":
            current = current[-NOTES_MAX:]
        if current or key in pd.profile:
            pd.profile[key] = current


def _appointments(ops: list, pd: PersonData, out: Applied, chat: str) -> None:
    doctors = pd.doctor_ids()
    for op in ops:
        if not isinstance(op, Mapping):
            raise BlockError("bad_appointments")
        kind = op.get("op")
        taken = {str(a.get("id")) for a in pd.appointments}
        if kind == "add":
            if set(op) - (APPT_FIELDS | {"op"}):
                raise BlockError("unknown_key")
            fields = _appt_fields(op, doctors)
            if "date" not in fields or "what" not in fields:
                raise BlockError("missing_date" if "date" not in fields else "missing_what")
            twin = next(
                (
                    a
                    for a in pd.appointments
                    if as_date(a.get("date")) == fields["date"]
                    and clock_text(a.get("time")) == fields.get("time")
                    and str(a.get("what", "")).casefold() == fields["what"].casefold()
                    and a.get("status", "pending") == "pending"
                ),
                None,
            )
            if twin is not None:
                continue  # the same appointment again (an answer applied twice): nothing new
            appt = {
                "id": _appt_id(fields["date"], fields.get("time"), fields["what"], taken),
                "date": fields["date"],
                "time": fields.get("time"),
                "what": fields["what"],
                "doctor": fields.get("doctor"),
                "place": fields.get("place"),
                "prep": fields.get("prep", []),
                "chat": chat,
                "status": "pending",
                "outcome": "",
            }
            pd.appointments.append(appt)
            out.items.append(("appt_add", dict(appt)))
            out.added.append(dict(appt))
            out.summary.append(f"appointment add {appt['id']}")
            continue
        target = _find(pd.appointments, op.get("id"))
        if kind == "change":
            if set(op) - (APPT_FIELDS | {"op", "id"}):
                raise BlockError("unknown_key")
            fields = _appt_fields(op, doctors)
            before = dict(target)
            for k, v in fields.items():
                target[k] = v
            if target != before:
                out.items.append(("appt_change", {"before": before, "after": dict(target)}))
                out.summary.append(f"appointment change {target['id']}")
        elif kind == "cancel":
            if set(op) - {"op", "id"}:
                raise BlockError("unknown_key")
            if target.get("status") != "cancelled":
                target["status"] = "cancelled"
                out.items.append(("appt_cancel", dict(target)))
                out.summary.append(f"appointment cancel {target['id']}")
        elif kind == "done":
            if set(op) - {"op", "id", "outcome"}:
                raise BlockError("unknown_key")
            outcome = _text(op.get("outcome"), "outcome") or ""
            if target.get("status") != "done" or (outcome and target.get("outcome") != outcome):
                target["status"] = "done"
                if outcome:
                    target["outcome"] = outcome
                out.items.append(("appt_done", dict(target)))
                out.summary.append(f"appointment done {target['id']}")
        else:
            raise BlockError("bad_op")


def _find(items: list, iid: object) -> dict:
    for item in items:
        if isinstance(item, dict) and iid is not None and item.get("id") == iid:
            return item
    raise BlockError("unknown_id")


def _appt_fields(op: Mapping, doctors: set[str]) -> dict:
    fields: dict = {}
    if "date" in op:
        fields["date"] = _date(op["date"])
    if "time" in op:
        if op["time"] is None:
            fields["time"] = None
        else:
            try:
                fields["time"] = hhmm(op["time"]).strftime("%H:%M")
            except ValueError:
                raise BlockError("bad_time") from None
    if "what" in op:
        fields["what"] = _text(op["what"], "what", True)
    if "place" in op:
        fields["place"] = _text(op["place"], "place")
    if "doctor" in op:
        if op["doctor"] is not None and op["doctor"] not in doctors:
            raise BlockError("unknown_doctor")
        fields["doctor"] = op["doctor"]
    if "prep" in op:
        fields["prep"] = _prep(op["prep"] if op["prep"] is not None else [])
    return fields


def _med_fields(op: Mapping, doctors: set[str]) -> dict:
    fields: dict = {}
    if "name" in op:
        fields["name"] = _text(op["name"], "name", True)
    if "kind" in op:
        if op["kind"] not in MED_KINDS:
            raise BlockError("bad_kind")
        fields["kind"] = op["kind"]
    for key in ("dose", "food", "why"):
        if key in op:
            fields[key] = _text(op[key], key)
    if "slot" in op:
        if op["slot"] is not None and op["slot"] not in SLOTS:
            raise BlockError("bad_slot")
        fields["slot"] = op["slot"]
    if "days" in op:
        if not _days_ok(op["days"]):
            raise BlockError("bad_days")
        fields["days"] = op["days"] if op["days"] == "daily" else list(op["days"])
    if "prescribed_by" in op:
        if op["prescribed_by"] is not None and op["prescribed_by"] not in doctors:
            raise BlockError("unknown_doctor")
        fields["prescribed_by"] = op["prescribed_by"]
    return fields


def _meds(ops: list, pd: PersonData, out: Applied, today: date) -> None:
    doctors = pd.doctor_ids()
    for op in ops:
        if not isinstance(op, Mapping):
            raise BlockError("bad_meds")
        kind = op.get("op")
        if kind == "add":
            if set(op) - (MED_FIELDS | {"op"}):
                raise BlockError("unknown_key")
            fields = _med_fields(op, doctors)
            if "name" not in fields or "kind" not in fields:
                raise BlockError("missing_name" if "name" not in fields else "missing_kind")
            same = next(
                (
                    m
                    for m in pd.meds
                    if isinstance(m, dict)
                    and slug(str(m.get("name", ""))) == slug(fields["name"])
                    and active(m, today)
                ),
                None,
            )
            if same is not None:  # already taken: the add is a change of what it says
                before = dict(same)
                same.update(fields)
                if same != before:
                    out.items.append(("med_change", {"before": before, "after": dict(same)}))
                    out.summary.append(f"med change {same.get('id')}")
                continue
            taken = {str(m.get("id")) for m in pd.meds if isinstance(m, dict)}
            med = {
                "id": _med_id(fields["name"], taken),
                "name": fields["name"],
                "kind": fields["kind"],
                "dose": fields.get("dose"),
                "slot": fields.get("slot"),
                "days": fields.get("days", "daily"),
                "food": fields.get("food"),
                "why": fields.get("why"),
                "prescribed_by": fields.get("prescribed_by"),
                "since": today,
                "until": None,
                "source": "chat",
            }
            pd.meds.append(med)
            out.items.append(("med_add", dict(med)))
            out.summary.append(f"med add {med['id']}")
            continue
        target = _find(pd.meds, op.get("id"))
        if kind == "change":
            if set(op) - (MED_FIELDS | {"op", "id"}):
                raise BlockError("unknown_key")
            before = dict(target)
            target.update(_med_fields(op, doctors))
            if target != before:
                out.items.append(("med_change", {"before": before, "after": dict(target)}))
                out.summary.append(f"med change {target['id']}")
        elif kind == "stop":
            if set(op) - {"op", "id", "until"}:
                raise BlockError("unknown_key")
            until = _date(op["until"], "until") if op.get("until") is not None else today
            if until > today + timedelta(days=366):
                raise BlockError("bad_until")
            if as_date(target.get("until")) != until:
                target["until"] = until
                out.items.append(("med_stop", dict(target)))
                out.summary.append(f"med stop {target['id']}")
        else:
            raise BlockError("bad_op")


def _number(value: object, key: str):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BlockError(f"bad_{key}")
    return value


def _labs(ops, pd, out: Applied, today: date, docs, attachments, base) -> None:
    changed = []
    for raw in ops:
        if not isinstance(raw, Mapping) or set(raw) - LAB_FIELDS:
            raise BlockError("bad_labs")
        d = _date(raw.get("date"))
        if d > today:
            raise BlockError("future_date")
        value = raw.get("value")
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise BlockError("bad_value")
        if isinstance(value, str):
            value = _text(value, "value", True)
        row = {
            "date": d.isoformat(),
            "test": _text(raw.get("test"), "test", True),
            "value": value,
            "unit": _text(raw.get("unit"), "unit"),
            "low": _number(raw.get("low"), "low"),
            "high": _number(raw.get("high"), "high"),
            "lab": _text(raw.get("lab"), "lab"),
            "doc": docs.get(raw["doc"]) if raw.get("doc") else None,
            "source": "chat",
        }
        row = {k: v for k, v in row.items() if v is not None and v != ""}
        key = (row["date"], row["test"].casefold())
        rest = [
            x
            for x in pd.labs
            if (str(as_date(x.get("date")) or x.get("date")), str(x.get("test", "")).casefold())
            != key
        ]
        same = len(rest) == len(pd.labs) - 1 and any(_lab_equal(x, row) for x in pd.labs)
        if same:
            continue
        pd.labs = [*rest, row]
        changed.append(row)
    if changed:
        out.items.append(("labs", {"values": changed}))
        out.summary.append(f"labs {len(changed)}")


def _lab_equal(a: Mapping, b: Mapping) -> bool:
    def norm(x):
        return {k: v for k, v in x.items() if v is not None and v != "" and k != "source"}

    a2 = norm({**a, "date": str(as_date(a.get("date")) or a.get("date"))})
    return a2 == norm(b)


def load_people(data: str | Path, ids: Sequence[str]) -> dict[str, PersonData]:
    return {pid: load_person(data, pid) for pid in ids}


def person_texts(data: str | Path, pid: str) -> dict[str, str]:
    """Every file under `people/<pid>/` except `documents/`, as text, for the turn's `files`."""
    base = Path(data) / person_dir(pid)
    out = {}
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base)
        if not path.is_file() or rel.parts[0] == DOCUMENTS or path.name.startswith("."):
            continue
        try:
            out[f"{person_dir(pid)}/{rel.as_posix()}"] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return out


def settings_text_for_turn(text: str) -> str:
    """`settings.yaml` as the assistant sees it: without `assistant.instructions`, which reach
    it inside the rules."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return ""
    if not isinstance(raw, dict):
        return ""
    block = raw.get("assistant")
    if isinstance(block, dict) and "instructions" in block:
        raw["assistant"] = {k: v for k, v in block.items() if k != "instructions"}
    return dump_yaml(raw)
