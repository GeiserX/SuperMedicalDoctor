"""`smd check`, `smd export` and the guards of `smd run` and `smd say`."""

import yaml
from conftest import write_seed

from supermedicaldoctor import cli


def test_check_passes_on_the_seed_and_counts(tmp_path, capsys):
    write_seed(tmp_path)
    assert cli.main(["check", "--data", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out == (
        "settings ok\npeople ok: 2 people, 1 appointments, 4 medicines and supplements, "
        "1 lab values\n"
    )


def test_check_names_each_problem_and_exits_1(tmp_path, capsys):
    write_seed(tmp_path)
    settings = (tmp_path / "settings.yaml").read_text().replace("admins: [pablo]", "admins: [x]")
    (tmp_path / "settings.yaml").write_text(settings)
    (tmp_path / "people/pablo/meds.yaml").write_text("- {id: A, name: Ácido, kind: pill}\n")
    (tmp_path / "people/lucia/labs.jsonl").write_text("not json\n")
    assert cli.main(["check", "--data", str(tmp_path)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err == [
        "settings: bad_admins",
        "lucia: bad_json: labs.jsonl line 1",
        "pablo: meds.yaml item 1: bad_id",
        "pablo: meds.yaml item 1: bad_kind",
    ]
    assert "Ácido" not in "\n".join(err)


def test_check_without_settings(tmp_path, capsys):
    assert cli.main(["check", "--data", str(tmp_path)]) == 1
    assert capsys.readouterr().err == "settings: missing_settings\n"


def test_export_prints_the_chef_export(tmp_path, capsys):
    write_seed(tmp_path)
    assert cli.main(["export", "--data", str(tmp_path)]) == 0
    doc = yaml.safe_load(capsys.readouterr().out)
    assert set(doc["people"]) == {"lucia", "pablo"}


def test_run_and_say_need_a_token(tmp_path, capsys, monkeypatch):
    write_seed(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert cli.main(["run", "--data", str(tmp_path)]) == 2
    assert cli.main(["say", "--intro", "--data", str(tmp_path)]) == 2
    assert cli.main(["say", "--data", str(tmp_path)]) == 2
