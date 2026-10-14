"""The reminders the app sends by itself, with no AI, from each person's `appointments.yaml`.

Three kinds, each posted once per appointment (and again if the appointment moves):

- `day_before`: at `reminders.day_before_at` the evening before, until midnight.
- `prep-<n>`: `hours_before` before the start, for each preparation line of a timed appointment.
  One due inside the quiet hours goes at 22:00 the evening before instead (or an hour before the
  quiet hours start, when 22:00 is inside them too).
- `followup`: 3 h after the start (20:00 on the day, without a time), while the appointment is
  still pending, for 48 h; asks how it went.

Nothing is posted inside the quiet hours. Pure functions: `now` is passed in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as clock_time
from zoneinfo import ZoneInfo

from supermedicaldoctor.records import PersonData, appt_time, as_date, before, clock_text

FALLBACK = clock_time(22, 0)
FOLLOWUP_AFTER = timedelta(hours=3)
FOLLOWUP_UNTIMED = clock_time(20, 0)
FOLLOWUP_WINDOW = timedelta(hours=48)


@dataclass(frozen=True)
class Due:
    person: str
    appt: dict
    kind: str  # "day_before", "prep-<n>" or "followup"
    stamp: str  # the appointment's date and time it was due for: a move makes it due again
    at: datetime  # when it became due
    prep: dict | None = None
    early: bool = False  # a preparation moved to the evening before by the quiet hours

    @property
    def key(self) -> str:
        return f"sent:{self.person}:{self.appt.get('id')}:{self.kind}"


def in_quiet(t: clock_time, quiet: tuple[clock_time, clock_time]) -> bool:
    start, end = quiet
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def evening_before(at: datetime, quiet: tuple[clock_time, clock_time]) -> datetime:
    """The 22:00 at or before `at` (or an hour before the quiet hours, when 22:00 is quiet)."""
    hour = FALLBACK
    if in_quiet(hour, quiet):
        first = datetime.combine(at.date(), quiet[0], tzinfo=at.tzinfo) - timedelta(hours=1)
        hour = first.time()
    day = at.date() if at.time() >= hour else at.date() - timedelta(days=1)
    return datetime.combine(day, hour, tzinfo=at.tzinfo)


def due(
    people: Mapping[str, PersonData],
    now: datetime,
    tz: str,
    *,
    day_before_at: clock_time,
    followup: bool,
    quiet: tuple[clock_time, clock_time],
) -> list[Due]:
    """Every reminder due at `now`, sent or not (the caller keeps what it sent)."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    if in_quiet(local.time(), quiet):
        return []
    out: list[Due] = []
    for pid in sorted(people):
        for a in people[pid].appointments:
            if not isinstance(a, Mapping) or a.get("status", "pending") != "pending":
                continue
            day = as_date(a.get("date"))
            if day is None or not a.get("id"):
                continue
            t = appt_time(a)
            start = datetime.combine(day, t, tzinfo=zone) if t else None
            stamp = f"{day.isoformat()}T{clock_text(a.get('time')) or 'x'}"
            midnight = datetime.combine(day, clock_time(0, 0), tzinfo=zone)
            at = datetime.combine(day - timedelta(days=1), day_before_at, tzinfo=zone)
            if at <= local < midnight:
                out.append(Due(pid, dict(a), "day_before", stamp, at))
            if start is not None:
                for n, prep in enumerate(a.get("prep") or []):
                    hours = prep.get("hours_before") if isinstance(prep, Mapping) else None
                    if isinstance(hours, bool) or not isinstance(hours, int):
                        continue
                    at = before(start, hours)
                    early = in_quiet(at.time(), quiet)
                    if early:
                        at = evening_before(at, quiet)
                    if at <= local < start:
                        out.append(Due(pid, dict(a), f"prep-{n}", stamp, at, dict(prep), early))
            if followup:
                at = (
                    start + FOLLOWUP_AFTER
                    if start
                    else datetime.combine(day, FOLLOWUP_UNTIMED, tzinfo=zone)
                )
                if at <= local < at + FOLLOWUP_WINDOW:
                    out.append(Due(pid, dict(a), "followup", stamp, at))
    out.sort(key=lambda d: d.at)
    return out
