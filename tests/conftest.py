"""Fixtures: a fictional household (Lucía and Pablo) in a real data repository with a bare remote
and a person's second clone, a fake Telegram, and a clock moved by hand. Every name, id, date and
value here is invented."""

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from supermedicaldoctor.bot import Bot
from supermedicaldoctor.telegram import TelegramError

HUMAN = ["-c", "user.name=Someone", "-c", "user.email=someone@example.invalid"]
ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
FIXTURES = Path(__file__).parent / "fixtures"
MADRID = ZoneInfo("Europe/Madrid")
GROUP, LUCIA, PABLO, STRANGER, BOT_ID = -1001234567, 111, 222, 333, 999
TODAY = date(2027, 3, 10)  # a Wednesday

SETTINGS = """\
timezone: Europe/Madrid
language: es
telegram:
  chat_id: -1001234567       # the group
  thread_id: null
  admins: [pablo]
  history_scope: all
  history_days: 14
people:
  lucia: {name: Lucía, user_id: 111, username: lucia_tg, calendar: cal-lucia}
  pablo: {name: Pablo, user_id: 222, username: null, calendar: cal-pablo}
rules: []
reminders:
  day_before_at: "20:00"     # the evening before
  followup: true
  quiet: ["23:00", "07:00"]
calendar:
  appointments: true
  reminders: true
assistant:
  plugin: plugins/fake.py
  timeout_s: 5
  model: null
  instructions: "Secret household instruction."
sources: []
exports:
  - {kind: chef, path: export/chef.yaml}
"""

LUCIA_PROFILE = """\
name: Lucía
birth_date: 1990-01-01
sex: female
allergies:
  - {what: penicilina, reaction: urticaria, since: 2010}
intolerances: [lactosa]
conditions:
  - {name: Hipotiroidismo, since: 2019, status: active}
diet:
  - "Sin pomelo: interacciona con un medicamento que toma"
doctors:
  - {id: endo, name: "Dra. Ejemplo", specialty: Endocrinología, center: "Hospital Ejemplo", phone: null}
notes: []
"""

LUCIA_MEDS = """\
- id: levotiroxina
  name: Levotiroxina
  kind: medicine
  dose: "50 µg"
  slot: waking
  days: daily
  food: "En ayunas, 30 min antes del desayuno"
  why: Hipotiroidismo
  prescribed_by: endo
  since: 2024-03-01
  until: null
  source: seed
- id: vitamina-d
  name: Vitamina D
  kind: supplement
  dose: "2000 UI"
  slot: breakfast
  days: daily
  food: "Con grasa"
  why: Déficit
  prescribed_by: null
  since: 2025-01-01
  until: null
  source: seed
- id: hierro-viejo
  name: Hierro viejo
  kind: supplement
  dose: "20 mg"
  slot: waking
  days: daily
  food: null
  why: null
  prescribed_by: null
  since: 2024-01-01
  until: 2024-06-01
  source: seed
"""

LUCIA_APPTS = """\
- id: 2027-03-12-0930-analitica
  date: 2027-03-12
  time: "09:30"
  what: Analítica de control
  doctor: endo
  place: Centro de salud Ejemplo
  prep:
    - {hours_before: 48, text: Sin hierro}
    - {hours_before: 8, text: En ayunas}
  chat: group
  status: pending
  outcome: ""
"""

LUCIA_LABS = (
    '{"date": "2026-09-01", "test": "Ferritina", "value": 18, "unit": "ng/mL", "low": 30, '
    '"high": 400, "source": "seed"}\n'
)


def git(path: Path, *args: str) -> str:
    """git as a person, cut off from the host's own configuration."""
    r = subprocess.run(
        ["git", "-C", str(path), *HUMAN, *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **ISOLATED},
    )
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout


@dataclass
class DataRepo:
    remote: Path
    data: Path
    human: Path

    def remote_file(self, path: str) -> str:
        return git(self.remote, "show", f"main:{path}")

    def remote_log(self) -> list[str]:
        return git(self.remote, "log", "--format=%s", "main").splitlines()

    def human_pull(self) -> None:
        git(self.human, "pull", "--quiet", "--rebase")


def write_seed(root: Path) -> None:
    (root / "people" / "lucia").mkdir(parents=True)
    (root / "people" / "pablo").mkdir(parents=True)
    (root / "settings.yaml").write_text(SETTINGS, encoding="utf-8")
    (root / "people/lucia/profile.yaml").write_text(LUCIA_PROFILE, encoding="utf-8")
    (root / "people/lucia/meds.yaml").write_text(LUCIA_MEDS, encoding="utf-8")
    (root / "people/lucia/appointments.yaml").write_text(LUCIA_APPTS, encoding="utf-8")
    (root / "people/lucia/labs.jsonl").write_text(LUCIA_LABS, encoding="utf-8")
    (root / "people/pablo/profile.yaml").write_text(
        "name: Pablo\nbirth_date: 1988-05-05\nallergies: []\nnotes: []\n", encoding="utf-8"
    )
    (root / "people/pablo/meds.yaml").write_text(
        "- {id: omega-3, name: Omega 3, kind: supplement, dose: 1 g, slot: dinner, days: daily,"
        " food: null, why: null, prescribed_by: null, since: 2026-01-01, until: null,"
        " source: seed}\n",
        encoding="utf-8",
    )
    (root / "people/pablo/appointments.yaml").write_text("[]\n", encoding="utf-8")
    (root / "plugins").mkdir()
    shutil.copy(FIXTURES / "fake_plugin.py", root / "plugins" / "fake.py")
    (root / ".gitignore").write_text("state/\n", encoding="utf-8")


@pytest.fixture(scope="session")
def _data_repo_template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("template")
    remote, seed = root / "remote.git", root / "seed"
    git(root, "init", "--quiet", "--bare", "-b", "main", str(remote))
    git(root, "init", "--quiet", "-b", "main", str(seed))
    write_seed(seed)
    git(seed, "add", ".")
    git(seed, "commit", "--quiet", "-m", "seed")
    git(seed, "push", "--quiet", str(remote), "main")
    for name in ("data", "human"):
        git(root, "clone", "--quiet", "remote.git", name)
        git(root / name, "remote", "set-url", "origin", "../remote.git")
    shutil.rmtree(seed)
    return root


@pytest.fixture
def data_repo(_data_repo_template, tmp_path, monkeypatch) -> DataRepo:
    for key, value in ISOLATED.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "repos"
    shutil.copytree(_data_repo_template, root, symlinks=True)
    return DataRepo(remote=root / "remote.git", data=root / "data", human=root / "human")


class FakeTelegram:
    """Stands in for `Telegram`: records every call, hands out increasing message ids, and
    raises a scripted `TelegramError` on demand. `files` maps a `file_id` to the bytes `getFile`
    and `download` hand back."""

    def __init__(self):
        self.me = {
            "id": BOT_ID,
            "is_bot": True,
            "username": "example_doctor_bot",
            "can_read_all_group_messages": False,
        }
        self.calls: list[tuple[str, dict]] = []
        self.errors: dict[str, list[TelegramError]] = {}
        self.updates: list = []
        self.member: dict = {"status": "administrator"}
        self.files: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._next_id = 1000

    def fail_next(self, method: str, code: int, description: str, parameters=None) -> None:
        self.errors.setdefault(method, []).append(
            TelegramError(method, code, description, parameters)
        )

    def call(self, method: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        with self._lock:
            self.calls.append((method, params))
            pending = self.errors.get(method)
            if pending:
                raise pending.pop(0)
            if method == "getMe":
                return dict(self.me)
            if method == "getChatMember":
                return dict(self.member)
            if method == "getFile":
                file_id = params["file_id"]
                if file_id not in self.files:
                    raise TelegramError(method, 400, "Bad Request: invalid file_id")
                size = len(self.files[file_id])
                return {"file_id": file_id, "file_size": size, "file_path": f"files/{file_id}"}
            if method == "getUpdates":
                batch = self.updates.pop(0) if self.updates else []
                return batch() if callable(batch) else batch
            if method == "sendMessage":
                self._next_id += 1
                return {
                    "message_id": self._next_id,
                    "chat": {"id": params["chat_id"]},
                    "text": params.get("text", ""),
                }
            return True

    def download(self, file_path: str, limit: int | None = None) -> bytes:
        file_id = file_path.removeprefix("files/")
        with self._lock:
            self.calls.append(("download", {"file_path": file_path, "limit": limit}))
            pending = self.errors.get("download")
            if pending:
                raise pending.pop(0)
        data = self.files[file_id]
        if limit is not None and len(data) > limit:
            raise TelegramError("download", 413, "file too large")
        return data

    def of(self, method: str) -> list[dict]:
        with self._lock:
            return [p for m, p in self.calls if m == method]

    def texts(self) -> list[str]:
        return [p["text"] for p in self.of("sendMessage")]


class Clock:
    def __init__(self, start: datetime):
        self.at = start

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at += timedelta(**kw)


def at(day: date, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MADRID)


@pytest.fixture
def make_bot(data_repo):
    """A bot on the fictional household at 10:00 on `today`, its settings optionally replaced
    (committed and pushed, as a person would)."""
    bots = []

    def make(today=TODAY, settings=None, hour=10, sources=None):
        if settings is not None:
            (data_repo.human / "settings.yaml").write_text(settings, encoding="utf-8")
            git(data_repo.human, "commit", "--quiet", "-am", "settings")
            git(data_repo.human, "push", "--quiet")
            git(data_repo.data, "pull", "--quiet")
        tg = FakeTelegram()
        bot = Bot(data_repo.data, tg, now=Clock(at(today, hour)), sources=sources)
        bot.debounce_s = 0
        bot.start()
        bots.append(bot)
        return bot

    yield make
    for bot in bots:
        bot.close()
