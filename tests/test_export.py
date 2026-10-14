"""The chef export holds exactly the fields CONTRACT 2.7 names, and nothing else ever."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml
from conftest import write_seed

from supermedicaldoctor import export, records

NOW = datetime(2027, 3, 10, 10, 0, tzinfo=ZoneInfo("Europe/Madrid"))
ALLOWED_PERSON = {"allergies", "intolerances", "diet", "meds", "fasting"}
ALLOWED_MED = {"name", "kind", "dose", "slot", "days", "food"}
ALLOWED_FASTING = {"from", "until", "text"}
NEVER = ("Hipotiroidismo", "Endocrinología", "Centro de salud", "Analítica de control", "Ferritina")


def assert_clean(text: str) -> None:
    """The export check: only the allowed keys at every level, and none of the words that
    belong to conditions, doctors, places, appointment purposes or lab values."""
    doc = yaml.safe_load(text)
    assert set(doc) == {"generated", "people"}
    for person in doc["people"].values():
        assert set(person) <= ALLOWED_PERSON, set(person) - ALLOWED_PERSON
        for med in person.get("meds") or []:
            assert set(med) <= ALLOWED_MED, set(med) - ALLOWED_MED
        for f in person.get("fasting") or []:
            assert set(f) <= ALLOWED_FASTING
    for word in NEVER:
        assert word not in text, word


@pytest.fixture
def people(tmp_path):
    write_seed(tmp_path)
    return records.load_people(tmp_path, ["lucia", "pablo"])


def test_the_export_holds_only_the_contract_fields(people):
    text = export.render(people, NOW, "Europe/Madrid")
    assert text.startswith(export.HEADER)
    assert_clean(text)
    doc = yaml.safe_load(text)
    lucia = doc["people"]["lucia"]
    assert lucia["allergies"] == ["penicilina"]
    assert lucia["intolerances"] == ["lactosa"]
    assert lucia["diet"] == ["Sin pomelo: interacciona con un medicamento que toma"]
    assert [m["name"] for m in lucia["meds"]] == ["Levotiroxina", "Vitamina D"]  # stopped out
    assert lucia["fasting"] == [
        {
            "from": "2027-03-12T01:30:00+01:00",
            "until": "2027-03-12T09:30:00+01:00",
            "text": "En ayunas",
        }
    ]  # «Sin hierro» is preparation, not fasting
    assert doc["generated"] == "2027-03-10T10:00:00+01:00"
    assert doc["people"]["pablo"]["meds"][0]["name"] == "Omega 3"


def test_the_check_goes_red_when_conditions_reach_the_export(people, monkeypatch):
    """Positive control: an export that also carried `conditions` must fail the check."""
    real = export.chef

    def leaky(people_, now, tz):
        out = real(people_, now, tz)
        out["lucia"]["conditions"] = people_["lucia"].profile["conditions"]
        return out

    monkeypatch.setattr(export, "chef", leaky)
    with pytest.raises(AssertionError):
        assert_clean(export.render(people, NOW, "Europe/Madrid"))


def test_fasting_windows_reach_fourteen_days_ahead_only(people):
    later = datetime(2027, 2, 20, 10, 0, tzinfo=ZoneInfo("Europe/Madrid"))
    assert export.chef(people, later, "Europe/Madrid")["lucia"]["fasting"] == []


def test_a_fasting_window_across_the_clock_change_is_real_hours(people):
    """On 2027-03-28 Madrid moves from 02:00 to 03:00: eight hours before 09:30 is 00:30."""
    a = people["lucia"].appointments[0]
    a.update(date="2027-03-28", time="09:30", prep=[{"hours_before": 8, "text": "En ayunas"}])
    now = datetime(2027, 3, 27, 10, 0, tzinfo=ZoneInfo("Europe/Madrid"))
    fasting = export.chef(people, now, "Europe/Madrid")["lucia"]["fasting"]
    assert fasting[0]["from"] == "2027-03-28T00:30:00+01:00"
    assert fasting[0]["until"] == "2027-03-28T09:30:00+02:00"


def test_same_ignores_the_generated_time(people):
    text = export.render(people, NOW, "Europe/Madrid")
    assert export.same(text, people, NOW.replace(hour=11), "Europe/Madrid")
    people["lucia"].profile["intolerances"] = []
    assert not export.same(text, people, NOW, "Europe/Madrid")
