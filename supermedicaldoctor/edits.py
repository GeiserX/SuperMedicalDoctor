"""File edits the assistant asks for with an ```edit block: keeping the household's files current.

A block lists edits inside the data checkout, each a `path` and one of: `content` (the whole new
text), `old` and `new` (one exact replacement), or `delete: true`. `asked` says whether the person
asked for the change («apúntalo»); without it the edit is only offered.

Where an edit may land is decided here, never by the assistant:

- never: `plugins/`, `deploy/`, `tests/`, `export/` (regenerated), `state/`, `.git`,
  `.gitignore`, `docs/DESIGN.md`, `docs/CONTRACT.md`, a person's `labs.jsonl` (the lab path
  writes it) and `documents/` (originals, filed by the record path);
- `people/<id>/**`: in the group, any person's folder; in a private chat only the asker's;
  an admin's anywhere;
- `settings.yaml`: admins only, and a change that drops instruction lines is asked first;
- `docs/HEALTH.md` and each path in `settings.editable`: in the group, or by an admin.

Every edit is a text file (`.md`, `.txt`, `.yaml`, `.yml`, `.json`), at most `EDIT_CHARS` of
new text, at most `EDITS_MAX` edits and `TURN_CHARS` in one turn. Nothing here writes to the
data directory: `check` applies the plan to a copy and runs the same checks as `smd check`.
"""

from __future__ import annotations

import difflib
import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from supermedicaldoctor import records
from supermedicaldoctor.records import BlockError, Settings

EDITS_MAX = 10
EDIT_CHARS = 20_000
TURN_CHARS = 60_000
TEXT_SUFFIXES = (".md", ".txt", ".yaml", ".yml", ".json")
DEFAULT_EDITABLE = ("docs/HEALTH.md",)
NEVER_DIRS = ("plugins", "deploy", "tests", "export", "state", ".git", ".github", ".beads")
NEVER_FILES = (".gitignore", "docs/DESIGN.md", "docs/CONTRACT.md")
DIFF_LINES = 30


@dataclass(frozen=True)
class Edit:
    path: str
    content: str | None = None  # the whole new text
    old: str | None = None  # an exact replacement: `old` occurs once and becomes `new`
    new: str | None = None
    delete: bool = False


@dataclass(frozen=True)
class Planned:
    path: str
    before: str | None  # the text on disk, None for a new file
    after: str | None  # the text to write, None to delete the file

    @property
    def big(self) -> bool:
        """A deletion of the whole file, or of more than half of its lines."""
        if self.after is None:
            return True
        old = [x for x in (self.before or "").splitlines() if x.strip()]
        if not old:
            return False
        kept = sum(
            b - a for tag, a, b, _, _ in _opcodes(self.before or "", self.after) if tag == "equal"
        )
        return len(old) - kept > len(old) / 2


def _opcodes(before: str, after: str):
    old = [x for x in before.splitlines() if x.strip()]
    new = [x for x in after.splitlines() if x.strip()]
    return difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes()


def parse(blocks: Sequence[object]) -> tuple[list[Edit], bool]:
    """Every ```edit block of an answer, merged: `(edits, asked)`. `asked` is true only when
    every block says the person asked. Raises `BlockError` with a reason code."""
    edits: list[Edit] = []
    asked = bool(blocks)
    for block in blocks:
        if not isinstance(block, Mapping) or set(block) - {"edits", "asked", "why"}:
            raise BlockError("bad_edit")
        items = block.get("edits")
        if not isinstance(items, list) or not items:
            raise BlockError("bad_edit")
        asked = asked and block.get("asked") is True
        for item in items:
            if not isinstance(item, Mapping) or set(item) - {
                "path",
                "content",
                "old",
                "new",
                "delete",
            }:
                raise BlockError("bad_edit")
            path = item.get("path")
            if not isinstance(path, str) or not path:
                raise BlockError("bad_path")
            forms = [
                "content" in item,
                "old" in item or "new" in item,
                item.get("delete") is True,
            ]
            if sum(forms) != 1:
                raise BlockError("bad_edit")
            for key in ("content", "old", "new"):
                if key in item and not isinstance(item[key], str):
                    raise BlockError("bad_edit")
            if forms[1] and ("old" not in item or "new" not in item or not item["old"]):
                raise BlockError("bad_edit")
            edits.append(
                Edit(
                    path=path.strip(),
                    content=item.get("content"),
                    old=item.get("old"),
                    new=item.get("new"),
                    delete=forms[2],
                )
            )
    if len(edits) > EDITS_MAX:
        raise BlockError("too_many_edits")
    if any(len(e.content or e.new or "") > EDIT_CHARS for e in edits):
        raise BlockError("edit_too_large")
    if sum(len(e.content or e.new or "") for e in edits) > TURN_CHARS:
        raise BlockError("edits_too_large")
    return edits, asked


def normal(path: str) -> str:
    """The path as the repo knows it; `BlockError("bad_path")` for anything that could leave
    the data directory."""
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or "\\" in path or not p.parts or path.endswith("/"):
        raise BlockError("bad_path")
    return p.as_posix()


def allowed(
    path: str,
    settings: Settings,
    *,
    scope: Sequence[str],
    asker: str | None,
    private: bool,
    admin: bool,
) -> None:
    """Raise `BlockError` with a reason code unless this edit may land at `path`."""
    parts = PurePosixPath(path).parts
    if parts[0] in NEVER_DIRS or path in NEVER_FILES:
        raise BlockError("path_never")
    if PurePosixPath(path).suffix.lower() not in TEXT_SUFFIXES:
        raise BlockError("not_a_text_file")
    if parts[0] == records.PEOPLE_DIR:
        if len(parts) < 3 or parts[1] not in settings.people:
            raise BlockError("bad_path")
        if parts[2] in (records.DOCUMENTS, "labs.jsonl"):
            raise BlockError("path_never")
        pid = parts[1]
        if admin:
            return
        if pid not in scope or (private and pid != asker):
            raise BlockError("not_your_folder")
        return
    if path == records.SETTINGS_FILE:
        if not admin:
            raise BlockError("admins_only")
        return
    if path in editable(settings):
        if private and not admin:
            raise BlockError("group_or_admin")
        return
    raise BlockError("path_not_editable")


def editable(settings: Settings) -> tuple[str, ...]:
    extra = tuple(str(x) for x in settings.editable if isinstance(x, str))
    return DEFAULT_EDITABLE + extra


def plan(data: str | Path, edits: Sequence[Edit]) -> list[Planned]:
    """What each edit turns its file into, from the files on disk now. Several edits of one
    file apply in order. A file that does not change is left out."""
    root = Path(data)
    texts: dict[str, str | None] = {}
    originals: dict[str, str | None] = {}
    for e in edits:
        path = e.path
        if path not in texts:
            try:
                raw = (root / path).read_bytes()
            except FileNotFoundError:
                raw = None
            except IsADirectoryError:
                raise BlockError("bad_path") from None
            try:
                originals[path] = texts[path] = raw.decode("utf-8") if raw is not None else None
            except UnicodeDecodeError:
                raise BlockError("not_a_text_file") from None
        current = texts[path]
        if e.delete:
            if current is None:
                raise BlockError("no_such_file")
            texts[path] = None
        elif e.content is not None:
            texts[path] = (
                e.content if e.content.endswith("\n") or not e.content else e.content + "\n"
            )
        else:
            if current is None or current.count(e.old) != 1:
                raise BlockError("old_text_not_found_once")
            texts[path] = current.replace(e.old, e.new, 1)
    return [Planned(p, originals[p], texts[p]) for p in texts if texts[p] != originals[p]]


def diff(p: Planned, limit: int = DIFF_LINES) -> list[str]:
    """The lines that go («−») and come («+»), at most `limit`, with a last line saying how
    many more there were."""
    old = [x for x in (p.before or "").splitlines() if x.strip()]
    new = [x for x in (p.after or "").splitlines() if x.strip()]
    out = []
    for tag, a, b, c, d in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            out += [f"− {x.strip()}" for x in old[a:b]]
        if tag in ("replace", "insert"):
            out += [f"+ {x.strip()}" for x in new[c:d]]
    if len(out) > limit:
        out = out[:limit] + [f"… +{len(out) - limit}"]
    return out


def instructions_dropped(p: Planned) -> bool:
    """Whether an edit of `settings.yaml` drops or rewrites a line of the standing
    instructions: the instructions guard asks before that, here too."""
    if p.path != records.SETTINGS_FILE:
        return False

    def instructions(text: str | None) -> str:
        try:
            raw = yaml.safe_load(text or "") or {}
        except yaml.YAMLError:
            return ""
        block = raw.get("assistant") if isinstance(raw, dict) else None
        return str((block or {}).get("instructions") or "") if isinstance(block, dict) else ""

    return bool(records.instruction_diff(instructions(p.before), instructions(p.after))[1])


def copy_data(data: str | Path, tmp: str | Path) -> Path:
    """A copy of the data directory to try a plan on: without git, state and documents."""
    copy = Path(tmp) / "data"
    shutil.copytree(data, copy, ignore=shutil.ignore_patterns(".git", "state", records.DOCUMENTS))
    return copy


def check(data: str | Path, planned: Sequence[Planned]) -> list[str]:
    """`check_copy` on a fresh copy of the data directory."""
    with tempfile.TemporaryDirectory(prefix="smd-edit-") as tmp:
        return check_copy(copy_data(data, tmp), planned)


def check_copy(copy: Path, planned: Sequence[Planned]) -> list[str]:
    """Apply the plan to `copy` and run what `smd check` runs: the settings, every person's
    files, and that each edited YAML or JSON file still parses. The reason codes, or [] when
    the copy is sound."""
    for p in planned:
        target = copy / p.path
        if p.after is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(p.after, encoding="utf-8")
    errors = []
    for p in planned:
        if p.after is None:
            continue
        suffix = PurePosixPath(p.path).suffix.lower()
        try:
            if suffix in (".yaml", ".yml"):
                yaml.safe_load(p.after)
            elif suffix == ".json":
                json.loads(p.after)
        except (yaml.YAMLError, ValueError):
            errors.append("does_not_parse")
    try:
        s = records.load_settings(copy)
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        return [*errors, "bad_settings"]
    errors += [f"settings: {e}" for e in records.settings_errors(s, copy)]
    for pid in s.people:
        try:
            errors += [
                f"person: {e}" for e in records.person_errors(records.load_person(copy, pid))
            ]
        except ValueError as e:
            errors.append(f"person: {e}")
    return errors


def paths_text(planned: Sequence[Planned]) -> str:
    return ", ".join(p.path for p in planned)
