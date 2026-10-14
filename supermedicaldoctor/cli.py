"""The `smd` command: run the bot, check a data directory, print the chef export, and post the
introduction.

Output names counts and reason codes, never a value from the data.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import yaml

from supermedicaldoctor import __version__, export, records

ALIVE = Path("/tmp/smd.alive")  # the Dockerfile's HEALTHCHECK reads its age


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smd", description="SuperMedicalDoctor")
    parser.add_argument("--version", action="version", version=f"smd {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    data = argparse.ArgumentParser(add_help=False)
    data.add_argument("--data", default=os.environ.get("SMD_DATA", "/data"), type=Path)
    sub.add_parser("run", parents=[data], help="run the bot (TELEGRAM_BOT_TOKEN, SMD_GIT_TOKEN)")
    sub.add_parser("check", parents=[data], help="validate settings.yaml and every person's files")
    sub.add_parser("export", parents=[data], help="print the chef export the data gives now")
    say = sub.add_parser("say", parents=[data], help="send a message as the bot")
    say.add_argument("--intro", action="store_true", help="the introduction, to the group")
    a = parser.parse_args(argv)
    return {"run": cmd_run, "check": cmd_check, "export": cmd_export, "say": cmd_say}[a.cmd](a)


def _settings(data: Path):
    try:
        s = records.load_settings(data)
    except FileNotFoundError:
        return None, ["missing_settings"]
    except (ValueError, TypeError, yaml.YAMLError):
        return None, ["bad_settings"]  # the parser's message may quote a value
    return s, records.settings_errors(s, data)


def cmd_check(a) -> int:
    """`settings.yaml` and every person's files; exit 1 on errors."""
    s, problems = _settings(a.data)
    errors = [f"settings: {code}" for code in problems]
    counts = {"appointments": 0, "meds": 0, "labs": 0}
    for pid in s.people if s is not None else ():
        try:
            pd = records.load_person(a.data, pid)
        except ValueError as e:
            errors.append(f"{pid}: {e}")
            continue
        errors += [f"{pid}: {code}" for code in records.person_errors(pd)]
        counts["appointments"] += len(pd.appointments)
        counts["meds"] += len(pd.meds)
        counts["labs"] += len(pd.labs)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("settings ok")
    print(
        f"people ok: {len(s.people)} people, {counts['appointments']} appointments, "
        f"{counts['meds']} medicines and supplements, {counts['labs']} lab values"
    )
    return 0


def cmd_export(a) -> int:
    """The chef export as the app would write it now, on stdout; writes nothing."""
    s, problems = _settings(a.data)
    if s is None or problems:
        print("settings: " + ", ".join(problems or ["bad_settings"]), file=sys.stderr)
        return 1
    try:
        people = records.load_people(a.data, list(s.people))
    except ValueError as e:
        print(f"people: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(export.render(people, datetime.now(UTC), s.timezone))
    return 0


def cmd_say(a) -> int:
    """`--intro`: the introduction (the text of /presentacion) to the group. It does not poll,
    so it is safe beside the running bot."""
    if not a.intro:
        print("say: nothing to send (use --intro)", file=sys.stderr)
        return 2
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is empty", file=sys.stderr)
        return 2
    from supermedicaldoctor.bot import Bot
    from supermedicaldoctor.telegram import Telegram, TelegramError, safe

    s, problems = _settings(a.data)
    if problems:
        print("settings: " + ", ".join(problems), file=sys.stderr)
        return 1
    bot = Bot(a.data, Telegram(token), settings=s)
    try:
        bot.send(s.chat, bot.t("intro"))
    except TelegramError as e:
        print(f"say failed: {safe(e)}", file=sys.stderr)
        return 1
    finally:
        bot.close()
    print("intro sent")
    return 0


def cmd_run(a) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    log = logging.getLogger("smd")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is empty")
        return 2
    s, problems = _settings(a.data)
    if problems:
        log.error("settings: %s", ", ".join(problems))
        return 2
    from supermedicaldoctor.bot import Bot, Interrupted
    from supermedicaldoctor.telegram import Telegram, TelegramError, safe

    bot = Bot(
        a.data,
        Telegram(token),
        token=os.environ.get("SMD_GIT_TOKEN", "").strip(),
        settings=s,
        alive=ALIVE,
    )
    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()
        if bot.polling:  # a long poll would hold the exit for up to 20 s
            raise Interrupted

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log.info("smd %s", __version__)
    try:
        bot.run(stop)
    except TelegramError as e:
        log.error("stopped: %s", safe(e))
        return 1
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
