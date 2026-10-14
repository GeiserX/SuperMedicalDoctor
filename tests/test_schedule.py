"""When the app's own reminders fall due: the evening before, each preparation, the ask after."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from supermedicaldoctor import schedule
from supermedicaldoctor.records import PersonData

TZ = "Europe/Madrid"
QUIET = (time(23, 0), time(7, 0))


def at(day, hour, minute=0):
    return datetime(2027, 3, day, hour, minute, tzinfo=ZoneInfo(TZ))


def appt(**kw):
    base = {
        "id": "2027-03-12-0930-analitica",
        "date": "2027-03-12",
        "time": "09:30",
        "what": "Analítica",
        "prep": [
            {"hours_before": 48, "text": "Sin hierro"},
            {"hours_before": 8, "text": "En ayunas"},
        ],
        "chat": "group",
        "status": "pending",
    }
    return {**base, **kw}


def kinds(now, a=None, followup=True, day_before=time(20, 0)):
    people = {"lucia": PersonData("lucia", appointments=[a or appt()])}
    due = schedule.due(people, now, TZ, day_before_at=day_before, followup=followup, quiet=QUIET)
    return {d.kind: d for d in due}


def test_the_evening_before_from_its_hour_to_midnight():
    assert "day_before" not in kinds(at(11, 19, 59))
    assert "day_before" in kinds(at(11, 20, 0))
    assert "day_before" in kinds(at(11, 22, 59))
    assert "day_before" not in kinds(at(12, 7, 0))  # the day itself: too late to say «mañana»


def test_a_preparation_falls_due_its_hours_before():
    assert "prep-0" not in kinds(at(10, 9, 29))
    assert "prep-0" in kinds(at(10, 9, 30))
    assert "prep-0" in kinds(at(12, 9, 0))
    assert "prep-0" not in kinds(at(12, 9, 30))  # the appointment has started


def test_a_preparation_due_in_the_quiet_hours_goes_at_22_the_evening_before():
    due = kinds(at(11, 22, 0))["prep-1"]  # 01:30 is quiet
    assert due.early and due.at == at(11, 22, 0)
    assert "prep-1" not in kinds(at(11, 21, 59))


def test_nothing_is_due_inside_the_quiet_hours():
    assert kinds(at(12, 1, 30)) == {}


def test_the_ask_after_three_hours_and_for_two_days():
    assert "followup" not in kinds(at(12, 12, 29))
    assert "followup" in kinds(at(12, 12, 30))
    assert "followup" in kinds(at(14, 12, 29))
    assert "followup" not in kinds(at(14, 12, 30))
    assert "followup" not in kinds(at(12, 13), followup=False)


def test_an_appointment_without_a_time_is_asked_about_at_20():
    untimed = appt(time=None, prep=[])
    assert "followup" not in kinds(at(12, 19, 59), untimed)
    assert "followup" in kinds(at(12, 20, 0), untimed)
    assert "day_before" in kinds(at(11, 20, 0), untimed)


def test_done_and_cancelled_appointments_are_quiet():
    for status in ("done", "cancelled"):
        assert kinds(at(11, 20, 0), appt(status=status)) == {}


def test_a_moved_appointment_has_a_new_stamp():
    one = kinds(at(11, 20, 0))["day_before"]
    two = kinds(at(11, 20, 0), appt(time="10:00"))["day_before"]
    assert one.key == two.key and one.stamp != two.stamp


def test_the_evening_before_moves_out_of_quiet_hours_that_start_early():
    early = (time(21, 0), time(7, 0))
    moved = schedule.evening_before(at(12, 1, 30), early)
    assert moved == at(11, 20, 0)
