"""The data model: settings checks, settings edited by line, and the record block applied all or
nothing (CONTRACT 2 and 4)."""

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from conftest import LUCIA_PROFILE, SETTINGS, write_seed

from supermedicaldoctor import records
from supermedicaldoctor.records import Attachment, BlockError, Settings

TODAY = date(2027, 3, 10)


@pytest.fixture
def data(tmp_path) -> Path:
    write_seed(tmp_path)
    return tmp_path


def people(data):
    return records.load_people(data, ["lucia", "pablo"])


def apply(data, block, scope=("lucia", "pablo"), asker="lucia", private=False, atts=None):
    return records.apply_record(
        block,
        people=people(data),
        scope=list(scope),
        asker=asker,
        private=private,
        attachments=atts or {},
        today=TODAY,
        read=lambda p: (data / p).read_bytes() if (data / p).is_file() else None,
    )


def reload(applied, path):
    text = applied.files[path]
    return yaml.safe_load(text) if path.endswith(".yaml") else text


# --- settings ------------------------------------------------------------------------------


def test_the_seed_settings_are_valid(data):
    s = records.load_settings(data)
    assert records.settings_errors(s, data) == []
    assert s.chat == -1001234567 and s.thread is None
    assert list(s.people) == ["lucia", "pablo"]
    assert s.people["lucia"].username == "lucia_tg" and s.people["pablo"].username is None
    assert s.is_admin("pablo") and not s.is_admin("lucia")
    assert s.by_user(111).id == "lucia"


@pytest.mark.parametrize(
    "old,new,code",
    [
        ("chat_id: -1001234567", 'chat_id: "x"', "bad_chat_id"),
        ("thread_id: null", "thread_id: abc", "bad_thread_id"),
        ("admins: [pablo]", "admins: [marta]", "bad_admins"),
        ("user_id: 111,", 'user_id: "111",', "bad_user_id: lucia"),
        ("user_id: 222,", "user_id: 111,", "duplicate_user_id: pablo"),
        ('day_before_at: "20:00"', 'day_before_at: "25:00"', "bad_day_before_at"),
        ('quiet: ["23:00", "07:00"]', 'quiet: ["23:00"]', "bad_quiet"),
        ("history_scope: all", "history_scope: some", "bad_history_scope"),
        ("followup: true", "followup: yes please", "bad_followup"),
        ("timezone: Europe/Madrid", "timezone: Mars/Base", "bad_timezone"),
        ("plugin: plugins/fake.py", "plugin: ../outside.py", "plugin_outside_data"),
        ("path: export/chef.yaml", "path: ../chef.yaml", "bad_export"),
    ],
)
def test_bad_settings_are_named_by_a_code(data, old, new, code):
    assert old in SETTINGS
    s = Settings.from_dict(yaml.safe_load(SETTINGS.replace(old, new)))
    assert code in records.settings_errors(s, data)


def test_an_unquoted_time_reads_as_the_time_it_says(data):
    s = Settings.from_dict(yaml.safe_load(SETTINGS.replace('"20:00"', "20:00")))
    assert s.day_before_at == "20:00"
    assert records.settings_errors(s, data) == []


def test_sources_are_checked(data):
    good = {
        "name": "genome",
        "kind": "gitea",
        "base_url": "http://gitea.example:3000",
        "repo": "home/health",
        "ref": "main",
        "token_env": "X",
        "files": [{"path": "lucia/STATE.md", "person": "lucia"}],
    }
    for change, code in (
        ({}, None),
        ({"kind": "svn"}, "bad_source_kind: genome"),
        ({"base_url": None}, "bad_source_base_url: genome"),
        ({"files": [{"path": "../x", "person": "lucia"}]}, "bad_source_path: genome"),
        ({"files": [{"path": "x.md", "person": "marta"}]}, "bad_source_person: genome"),
        ({"files": []}, "bad_source_files: genome"),
        ({"name": "Bad Name"}, "bad_source_name"),
    ):
        raw = yaml.safe_load(SETTINGS)
        raw["sources"] = [{**good, **change}]
        errors = records.settings_errors(Settings.from_dict(raw), data)
        assert (errors == []) if code is None else (code in errors), change


def test_set_lines_changes_one_value_and_keeps_every_other_byte():
    out = records.set_lines(SETTINGS, {("reminders", "day_before_at"): "21:15"})
    assert out == SETTINGS.replace('day_before_at: "20:00"', 'day_before_at: "21:15"')
    assert "# the evening before" in out


def test_set_lines_replaces_a_block_scalar_and_adds_missing_keys():
    text = "assistant:\n  instructions: |\n    line one\n    line two\n  model: null\n"
    out = records.set_lines(
        text,
        {("assistant", "instructions"): 'Di "hola"\ny adiós', ("reminders", "followup"): False},
    )
    loaded = yaml.safe_load(out)
    assert loaded["assistant"] == {"instructions": 'Di "hola"\ny adiós', "model": None}
    assert loaded["reminders"] == {"followup": False}
    out2 = records.set_lines("assistant:\n  model: null\n", {("assistant", "instructions"): "x"})
    assert yaml.safe_load(out2)["assistant"] == {"model": None, "instructions": "x"}


def test_set_lines_refuses_a_flow_section():
    with pytest.raises(ValueError, match="flow_section"):
        records.set_lines("reminders: {followup: true}\n", {("reminders", "followup"): False})


def test_validate_settings():
    ok = records.validate_settings({"reminders": {"day_before_at": "9:05"}}, admin=False)
    assert ok == {("reminders", "day_before_at"): "09:05"}
    assert records.validate_settings({"reminders.followup": False}, admin=False) == {
        ("reminders", "followup"): False
    }
    for block, admin, code in (
        ({"assistant": {"instructions": "x"}}, False, "admins_only"),
        ({"assistant": {"instructions": "x" * 4001}}, True, "bad_instructions"),
        ({"reminders": {"quiet": ["1:00", "2:00"]}}, True, "unknown_key"),
        ({"telegram": {"chat_id": 1}}, True, "unknown_key"),
        ({"reminders": {"day_before_at": "late"}}, True, "bad_day_before_at"),
        ({}, True, "bad_settings"),
    ):
        with pytest.raises(BlockError) as e:
            records.validate_settings(block, admin)
        assert e.value.code == code


# --- appointments --------------------------------------------------------------------------


def test_an_appointment_is_added_with_an_id_and_the_chat_it_was_said_in(data):
    applied = apply(
        data,
        {
            "person": "lucia",
            "appointments": [
                {
                    "op": "add",
                    "date": "2027-03-21",
                    "time": "9:30",
                    "what": "Análisis de sangre",
                    "doctor": "endo",
                    "place": "Centro Ejemplo",
                    "prep": [{"hours_before": 8, "text": "En ayunas"}],
                }
            ],
        },
        private=True,
    )
    appts = reload(applied, "people/lucia/appointments.yaml")
    new = appts[-1]
    assert new["id"] == "2027-03-21-0930-analisis"
    assert new["time"] == "09:30" and new["chat"] == "private" and new["status"] == "pending"
    assert new["date"] == date(2027, 3, 21)
    assert applied.added[0]["id"] == new["id"]
    assert applied.summary == ["appointment add 2027-03-21-0930-analisis"]
    assert list(applied.files) == ["people/lucia/appointments.yaml"]


def test_the_same_appointment_twice_changes_nothing(data):
    block = {
        "person": "lucia",
        "appointments": [
            {"op": "add", "date": "2027-03-12", "time": "09:30", "what": "Analítica de control"}
        ],
    }
    applied = apply(data, block)
    assert not applied and applied.files == {}


def test_change_cancel_and_done(data):
    aid = "2027-03-12-0930-analitica"
    applied = apply(
        data,
        {"person": "lucia", "appointments": [{"op": "change", "id": aid, "time": "10:00"}]},
    )
    assert reload(applied, "people/lucia/appointments.yaml")[0]["time"] == "10:00"
    assert reload(applied, "people/lucia/appointments.yaml")[0]["id"] == aid  # ids stay
    applied = apply(data, {"person": "lucia", "appointments": [{"op": "cancel", "id": aid}]})
    assert reload(applied, "people/lucia/appointments.yaml")[0]["status"] == "cancelled"
    applied = apply(
        data,
        {"person": "lucia", "appointments": [{"op": "done", "id": aid, "outcome": "Todo bien"}]},
    )
    done = reload(applied, "people/lucia/appointments.yaml")[0]
    assert done["status"] == "done" and done["outcome"] == "Todo bien"


@pytest.mark.parametrize(
    "block,code",
    [
        ({"person": "marta"}, "person_out_of_scope"),
        ({"person": "lucia", "appointments": [{"op": "cancel", "id": "nope"}]}, "unknown_id"),
        ({"person": "lucia", "appointments": [{"op": "add", "what": "x"}]}, "missing_date"),
        (
            {"person": "lucia", "appointments": [{"op": "add", "date": "2027-02-30", "what": "x"}]},
            "bad_date",
        ),
        (
            {
                "person": "lucia",
                "appointments": [{"op": "add", "date": "2027-03-20", "time": "25:00", "what": "x"}],
            },
            "bad_time",
        ),
        (
            {
                "person": "lucia",
                "appointments": [
                    {"op": "add", "date": "2027-03-20", "what": "x", "doctor": "dr-house"}
                ],
            },
            "unknown_doctor",
        ),
        ({"person": "lucia", "cycle": []}, "unknown_key"),
        ({"person": "lucia", "meds": [{"op": "add", "name": "X", "kind": "pill"}]}, "bad_kind"),
        (
            {
                "person": "lucia",
                "meds": [{"op": "add", "name": "X", "kind": "medicine", "slot": "noon"}],
            },
            "bad_slot",
        ),
        (
            {"person": "lucia", "labs": [{"date": "2027-03-11", "test": "X", "value": 1}]},
            "future_date",
        ),
        (
            {
                "person": "lucia",
                "documents": [{"file": "inbox/1-x.pdf", "date": "2027-03-01", "title": "x"}],
            },
            "not_an_attachment",
        ),
    ],
)
def test_a_bad_block_is_refused_with_a_code_that_quotes_no_value(data, block, code):
    with pytest.raises(BlockError) as e:
        apply(data, block)
    assert e.value.code == code
    for value in ("marta", "nope", "dr-house", "noon", "pill"):
        assert value not in str(e.value)


def test_a_private_chat_records_only_the_askers_own_data(data):
    with pytest.raises(BlockError, match="not_the_asker"):
        apply(data, {"person": "pablo"}, private=True, asker="lucia")
    with pytest.raises(BlockError, match="person_out_of_scope"):
        apply(data, {"person": "pablo"}, scope=("lucia",), private=True)


def test_a_hand_edited_file_with_a_bare_item_refuses_the_block_with_a_code(data):
    (data / "people/lucia/appointments.yaml").write_text("- just a string\n", encoding="utf-8")
    block = {"person": "lucia", "appointments": [{"op": "add", "date": "2027-03-21", "what": "X"}]}
    with pytest.raises(BlockError, match="bad_file"):
        apply(data, block)


def test_a_block_is_all_or_nothing(data):
    block = {
        "person": "lucia",
        "appointments": [{"op": "add", "date": "2027-03-21", "what": "Dentista"}],
        "meds": [{"op": "stop", "id": "no-such-med"}],
    }
    with pytest.raises(BlockError):
        apply(data, block)


# --- medicines and supplements -------------------------------------------------------------


def test_a_med_is_added_changed_and_stopped_and_stopped_ones_stay(data):
    applied = apply(
        data,
        {
            "person": "lucia",
            "meds": [
                {
                    "op": "add",
                    "name": "Hierro bisglicinato",
                    "kind": "supplement",
                    "dose": "20 mg",
                    "slot": "waking",
                    "food": "Con vitamina C",
                }
            ],
        },
    )
    meds = reload(applied, "people/lucia/meds.yaml")
    new = meds[-1]
    assert new["id"] == "hierro-bisglicinato" and new["source"] == "chat"
    assert new["since"] == TODAY and new["until"] is None and new["days"] == "daily"
    assert len(meds) == 4  # the stopped one stays
    applied = apply(
        data, {"person": "lucia", "meds": [{"op": "change", "id": "vitamina-d", "dose": "4000 UI"}]}
    )
    assert reload(applied, "people/lucia/meds.yaml")[1]["dose"] == "4000 UI"
    applied = apply(data, {"person": "lucia", "meds": [{"op": "stop", "id": "vitamina-d"}]})
    assert reload(applied, "people/lucia/meds.yaml")[1]["until"] == TODAY


def test_adding_a_med_already_taken_is_a_change(data):
    applied = apply(
        data,
        {
            "person": "lucia",
            "meds": [{"op": "add", "name": "Vitamina D", "kind": "supplement", "dose": "1000 UI"}],
        },
    )
    meds = reload(applied, "people/lucia/meds.yaml")
    assert len(meds) == 3 and meds[1]["dose"] == "1000 UI"
    assert applied.items[0][0] == "med_change"


# --- labs and documents --------------------------------------------------------------------


def test_lab_values_and_their_document_are_filed(data):
    att = Attachment("inbox/812-analitica.pdf", "application/pdf", b"%PDF-1", "lucia")
    applied = apply(
        data,
        {
            "person": "lucia",
            "labs": [
                {
                    "date": "2027-03-09",
                    "test": "Ferritina",
                    "value": 23,
                    "unit": "ng/mL",
                    "low": 30,
                    "high": 400,
                    "doc": "inbox/812-analitica.pdf",
                },
                {
                    "date": "2027-03-09",
                    "test": "Antígeno",
                    "value": "negativo",
                    "doc": "inbox/812-analitica.pdf",
                },
            ],
            "documents": [
                {
                    "file": "inbox/812-analitica.pdf",
                    "date": "2027-03-09",
                    "title": "Analítica de control",
                }
            ],
        },
        atts={att.name: att},
    )
    doc = "people/lucia/documents/2027-03-09-analitica-de-control.pdf"
    assert applied.files[doc] == b"%PDF-1"
    lines = [json.loads(x) for x in applied.files["people/lucia/labs.jsonl"].splitlines()]
    assert [x["test"] for x in lines] == ["Ferritina", "Antígeno", "Ferritina"]
    assert lines[1] == {
        "date": "2027-03-09",
        "test": "Antígeno",
        "value": "negativo",
        "doc": doc,
        "source": "chat",
    }
    assert list(lines[2]) == ["date", "test", "value", "unit", "low", "high", "doc", "source"]
    text = applied.files["people/lucia/labs.jsonl"]
    assert "Antígeno" in text  # ensure_ascii=False


def test_a_lab_read_twice_changes_nothing_and_the_same_file_is_not_filed_twice(data):
    att = Attachment("inbox/9-a.pdf", "application/pdf", b"%PDF", "lucia")
    block = {
        "person": "lucia",
        "labs": [{"date": "2027-03-09", "test": "TSH", "value": 2.1, "doc": "inbox/9-a.pdf"}],
    }
    applied = apply(data, block, atts={att.name: att})
    for path, content in applied.files.items():
        target = data / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    again = apply(data, block, atts={att.name: att})
    assert not again and again.files == {}


def test_a_different_file_with_the_same_name_gets_its_own_path(data):
    folder = data / "people/lucia/documents"
    folder.mkdir()
    (folder / "2027-03-09-a.pdf").write_bytes(b"old")
    att = Attachment("inbox/9-a.pdf", "application/pdf", b"new", "lucia")
    applied = apply(
        data,
        {"person": "lucia", "documents": [{"file": att.name, "date": "2027-03-09", "title": "a"}]},
        atts={att.name: att},
    )
    assert list(applied.files) == ["people/lucia/documents/2027-03-09-a-2.pdf"]


# --- profile -------------------------------------------------------------------------------


def test_profile_lists_take_add_and_remove(data):
    applied = apply(
        data,
        {
            "person": "lucia",
            "profile": {
                "allergies": {"add": [{"what": "ibuprofeno", "reaction": "asma"}]},
                "diet": {"remove": ["Sin pomelo: interacciona con un medicamento que toma"]},
                "notes": {"add": ["Prefiere citas por la mañana"]},
                "conditions": {"add": [{"name": "Anemia", "status": "suspected"}]},
            },
        },
    )
    profile = reload(applied, "people/lucia/profile.yaml")
    assert [a["what"] for a in profile["allergies"]] == ["penicilina", "ibuprofeno"]
    assert profile["diet"] == []
    assert profile["notes"] == ["Prefiere citas por la mañana"]
    assert profile["conditions"][-1] == {"name": "Anemia", "status": "suspected"}
    assert profile["birth_date"] == date(1990, 1, 1)  # unknown and untouched keys are kept
    assert "allow_unicode" not in applied.files["people/lucia/profile.yaml"]
    assert "Endocrinología" in applied.files["people/lucia/profile.yaml"]


def test_notes_keep_the_newest_thirty(data):
    applied = apply(
        data,
        {"person": "lucia", "profile": {"notes": {"add": [f"nota {n}" for n in range(35)]}}},
    )
    notes = reload(applied, "people/lucia/profile.yaml")["notes"]
    assert len(notes) == 30 and notes[-1] == "nota 34"


def test_person_errors_reads_the_seed_clean_and_names_bad_items(data):
    assert records.person_errors(records.load_person(data, "lucia")) == []
    (data / "people/lucia/meds.yaml").write_text(
        "- {id: Bad Id, name: X, kind: pill, slot: noon, days: [funday]}\n", encoding="utf-8"
    )
    errors = records.person_errors(records.load_person(data, "lucia"))
    assert errors == [
        "meds.yaml item 1: bad_id",
        "meds.yaml item 1: bad_kind",
        "meds.yaml item 1: bad_slot",
        "meds.yaml item 1: bad_days",
    ]


def test_settings_for_the_turn_leave_out_the_instructions():
    text = records.settings_text_for_turn(SETTINGS)
    assert "Secret household instruction" not in text
    assert yaml.safe_load(text)["assistant"]["plugin"] == "plugins/fake.py"


def test_slug_and_fasting_words():
    assert records.slug("Hierro bisglicinato") == "hierro-bisglicinato"
    assert records.slug("Análisis de sangre", 20).split("-")[0] == "analisis"
    assert records.is_fasting("En ayunas") and records.is_fasting("Ayuno de 12 h")
    assert not records.is_fasting("Sin hierro")


def test_the_profile_seed_is_the_contract_example():
    assert yaml.safe_load(LUCIA_PROFILE)["doctors"][0]["id"] == "endo"
