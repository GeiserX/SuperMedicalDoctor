"""git, for real: a bare remote, the bot's checkout and a person's clone."""

import os
import stat

import pytest
from conftest import git

from supermedicaldoctor.repo import KV, GitError, Repo


def hook(repo_dir, name, body):
    base = repo_dir / ".git" if (repo_dir / ".git").exists() else repo_dir  # a bare remote
    path = base / "hooks" / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_a_pull_inside_its_freshness_is_skipped(data_repo):
    clock = [0.0]
    repo = Repo(data_repo.data, clock=lambda: clock[0])
    assert repo.pull()
    (data_repo.human / "people/pablo/profile.yaml").write_text("name: Pablo\n", encoding="utf-8")
    git(data_repo.human, "commit", "--quiet", "-am", "edit")
    git(data_repo.human, "push", "--quiet")
    clock[0] = 10.0
    repo.pull()
    assert "birth_date" in (data_repo.data / "people/pablo/profile.yaml").read_text()
    clock[0] = 31.0
    repo.pull()
    assert (data_repo.data / "people/pablo/profile.yaml").read_text() == "name: Pablo\n"


def test_a_pull_that_changes_the_settings_says_so(data_repo):
    repo = Repo(data_repo.data)
    (data_repo.human / "settings.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    git(data_repo.human, "commit", "--quiet", "-am", "settings")
    git(data_repo.human, "push", "--quiet")
    repo.pull(0)
    assert repo.settings_pulled


def test_a_commit_is_pushed_at_once(data_repo):
    repo = Repo(data_repo.data)
    sha = repo.commit({"people/pablo/notes.md": "x\n"}, "log(pablo): note")
    assert sha and data_repo.remote_log()[0] == "log(pablo): note"
    assert repo.unpushed() == 0
    assert repo.commit({"people/pablo/notes.md": "x\n"}, "log(pablo): same") is None


def test_a_commit_rebases_on_a_persons_push_and_both_land(data_repo):
    clock = [0.0]
    repo = Repo(data_repo.data, clock=lambda: clock[0])
    repo.pull()
    (data_repo.human / "people/pablo/profile.yaml").write_text("name: Pablo\n", encoding="utf-8")
    git(data_repo.human, "commit", "--quiet", "-am", "a person's edit")
    git(data_repo.human, "push", "--quiet")
    clock[0] = 5.0  # the pull is fresh: the push itself has to rebase
    repo.commit({"people/lucia/notes.md": "y\n"}, "log(lucia): note")
    assert data_repo.remote_log()[:2] == ["log(lucia): note", "a person's edit"]


def test_a_refused_push_keeps_the_commit_and_the_tick_pushes_it_later(data_repo):
    clock = [0.0]
    repo = Repo(data_repo.data, clock=lambda: clock[0])
    refuse = hook(data_repo.remote, "pre-receive", "exit 1")
    repo.commit({"people/lucia/notes.md": "z\n"}, "log(lucia): note")
    assert repo.unpushed() == 1 and repo.last_error == "push_failed"
    assert repo.flush() == "backoff"
    refuse.unlink()  # the positive control: the same flush goes through once the remote takes it
    clock[0] = 61.0
    assert repo.flush() == "pushed" and repo.unpushed() == 0


def test_a_failed_commit_puts_every_file_back(data_repo):
    repo = Repo(data_repo.data)
    before = (data_repo.data / "people/lucia/labs.jsonl").read_bytes()
    gate = hook(data_repo.data, "pre-commit", "exit 1")
    with pytest.raises(GitError):
        repo.commit(
            {"people/lucia/labs.jsonl": "new\n", "people/lucia/new.txt": "n\n"}, "log(lucia): x"
        )
    assert (data_repo.data / "people/lucia/labs.jsonl").read_bytes() == before
    assert not (data_repo.data / "people/lucia/new.txt").exists()
    assert git(data_repo.data, "status", "--porcelain") == ""
    gate.unlink()
    assert repo.commit({"people/lucia/labs.jsonl": "new\n"}, "log(lucia): x")


def test_revert_undoes_a_commit_and_regenerates_the_exports(data_repo):
    repo = Repo(data_repo.data)
    sha = repo.commit(
        {"people/lucia/notes.md": "a\n", "export/chef.yaml": "one\n"}, "log(lucia): a"
    )
    repo.commit({"export/chef.yaml": "two\n"}, "export: chef")  # a later change to the export

    def regen():
        exists = (data_repo.data / "people/lucia/notes.md").exists()
        return {"export/chef.yaml": "with note\n" if exists else "without note\n"}

    new = repo.revert(sha, regen, "undo(lucia): revert")
    assert new
    assert not (data_repo.data / "people/lucia/notes.md").exists()
    assert data_repo.remote_file("export/chef.yaml") == "without note\n"
    assert data_repo.remote_log()[0] == "undo(lucia): revert"
    assert git(data_repo.data, "status", "--porcelain") == ""


def test_revert_refuses_when_a_later_commit_changed_the_same_data(data_repo):
    repo = Repo(data_repo.data)
    sha = repo.commit({"people/lucia/notes.md": "a\n"}, "log(lucia): a")
    repo.commit({"people/lucia/notes.md": "b\n"}, "log(lucia): b")
    with pytest.raises(GitError, match="undo_conflict"):
        repo.revert(sha, dict, "undo(lucia): revert")
    assert git(data_repo.data, "status", "--porcelain") == ""
    assert (data_repo.data / "people/lucia/notes.md").read_text() == "b\n"


def test_write_settings_commits_the_settings_alone(data_repo):
    repo = Repo(data_repo.data)
    (data_repo.data / "people/lucia/scratch.txt").write_text("not mine\n", encoding="utf-8")
    repo.write_settings(lambda old: old.replace("21:00", "21:30") + "# x\n", "reminders.x")
    assert data_repo.remote_log()[0] == "settings: reminders.x"
    assert "scratch.txt" not in git(data_repo.remote, "show", "--name-only", "main")


def test_the_token_never_reaches_git_config_or_a_url(data_repo):
    repo = Repo(data_repo.data, token="secret-token-value")
    repo.commit({"people/lucia/notes.md": "q\n"}, "log(lucia): q")
    config = (data_repo.data / ".git" / "config").read_text()
    assert "secret-token-value" not in config


def test_kv_refuses_unknown_keys(tmp_path):
    kv = KV(tmp_path / "s.sqlite")
    with pytest.raises(ValueError, match="unknown_kv_key"):
        kv.set("profile", "x")
    kv.set("sent:lucia:a:day_before", "2027-03-12T09:30")
    assert kv.get("sent:lucia:a:day_before") == "2027-03-12T09:30"
    assert oct(os.stat(tmp_path / "s.sqlite").st_mode & 0o777) == "0o600"
    kv.close()
