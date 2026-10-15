"""The ```edit block: file edits the assistant asks for, inside the allowed paths only, checked on
a copy, committed with the exports, confirmed with their diff and undone by /deshacer."""

import json

import pytest
import yaml
from conftest import LUCIA, PABLO, SETTINGS, git
from helpers import buttons, last_sent, plugin, say, tap, toasts

from supermedicaldoctor import edits
from supermedicaldoctor.records import BlockError, Settings


def edit_block(edits_, asked=True):
    return (
        "Vale.\n\n```edit\n"
        + json.dumps({"asked": asked, "edits": edits_}, ensure_ascii=False)
        + "\n```"
    )


def answer(bot, *replies):
    plugin(bot).REPLIES[:] = list(replies)


NOTE = {
    "path": "people/lucia/notes.md",
    "content": "Prefiere citas por la mañana.\nVive cerca del centro.",
}


def test_an_asked_edit_is_committed_at_once_with_its_diff_and_undone(make_bot, data_repo):
    bot = make_bot()
    answer(bot, edit_block([NOTE]))
    say(bot, "apúntalo en sus notas", user=PABLO, mention=True)
    assert data_repo.remote_file("people/lucia/notes.md") == (
        "Prefiere citas por la mañana.\nVive cerca del centro.\n"
    )
    assert data_repo.remote_log()[0] == "chat(pablo): edit people/lucia/notes.md"
    assert bot.tg.texts()[-1] == (
        "Actualizado:\n• people/lucia/notes.md\n+ Prefiere citas por la mañana.\n"
        "+ Vive cerca del centro."
    )
    say(bot, "/deshacer", user=PABLO)
    assert bot.tg.texts()[-1] == "Deshecho: cambios en people/lucia/notes.md."
    files = git(data_repo.remote, "ls-tree", "-r", "--name-only", "main").split()
    assert "people/lucia/notes.md" not in files


def test_a_replacement_edits_one_place_and_regenerates_the_export(make_bot, data_repo):
    bot = make_bot()
    answer(
        bot,
        edit_block(
            [
                {
                    "path": "people/lucia/profile.yaml",
                    "old": "intolerances: [lactosa]",
                    "new": "intolerances: [lactosa, gluten]",
                }
            ]
        ),
    )
    say(bot, "Lucía tampoco tolera el gluten, apúntalo", user=PABLO, mention=True)
    chef = yaml.safe_load(data_repo.remote_file("export/chef.yaml"))
    assert chef["people"]["lucia"]["intolerances"] == ["lactosa", "gluten"]
    changed = git(data_repo.remote, "show", "--name-only", "--format=", "main").split()
    assert sorted(changed) == ["export/chef.yaml", "people/lucia/profile.yaml"]
    assert "− intolerances: [lactosa]\n+ intolerances: [lactosa, gluten]" in bot.tg.texts()[-1]


@pytest.mark.parametrize(
    "path",
    [
        "plugins/fake.py",
        "plugins/notes.md",
        "export/chef.yaml",
        "deploy/stack.yaml",
        "tests/x.md",
        "docs/DESIGN.md",
        "docs/CONTRACT.md",
        ".gitignore",
        "people/lucia/labs.jsonl",
        "people/lucia/documents/x.md",
        "../outside.md",
        "/etc/passwd",
        "people/marta/notes.md",
        "README.md",
        "people/lucia/script.sh",
    ],
)
def test_a_path_outside_the_allow_list_is_refused_and_nothing_is_written(make_bot, data_repo, path):
    bot = make_bot()
    answer(bot, edit_block([{"path": path, "content": "x"}]))
    before = data_repo.remote_log()
    say(bot, "cambia eso", user=PABLO, mention=True)  # even an admin
    assert bot.tg.texts()[-1].startswith("No he podido hacer ese cambio en los archivos")
    assert data_repo.remote_log() == before


def test_a_private_chat_cannot_touch_the_other_persons_folder(make_bot, data_repo):
    bot = make_bot()
    answer(bot, edit_block([{"path": "people/pablo/notes.md", "content": "x"}]))
    before = data_repo.remote_log()
    say(bot, "apunta lo de Pablo", user=LUCIA, private=True)
    assert bot.tg.texts()[-1].startswith("No he podido hacer ese cambio en los archivos")
    assert data_repo.remote_log() == before


def test_in_the_group_either_folder_and_an_admin_anywhere(make_bot, data_repo):
    bot = make_bot()
    answer(
        bot,
        edit_block([{"path": "people/pablo/notes.md", "content": "Corre los martes."}]),
        edit_block([{"path": "people/lucia/notes.md", "content": "Nota."}]),
    )
    say(bot, "apunta lo de Pablo", user=LUCIA, mention=True)
    assert data_repo.remote_file("people/pablo/notes.md") == "Corre los martes.\n"
    say(bot, "apunta lo de Lucía", user=PABLO, private=True)  # Pablo is an admin
    assert data_repo.remote_file("people/lucia/notes.md") == "Nota.\n"


def test_settings_are_for_admins_and_editable_docs_are_listed(make_bot, data_repo):
    bot = make_bot(settings=SETTINGS + "editable: [docs/estado.md]\n")
    answer(
        bot,
        edit_block(
            [{"path": "settings.yaml", "old": "history_days: 14", "new": "history_days: 20"}]
        ),
        edit_block([{"path": "docs/estado.md", "content": "Todo en orden."}]),
        edit_block([{"path": "docs/HEALTH.md", "content": "Resumen."}]),
    )
    before = data_repo.remote_log()
    say(bot, "cambia el historial", user=LUCIA, mention=True)
    assert data_repo.remote_log() == before
    say(bot, "apunta el estado", user=LUCIA, mention=True)
    assert data_repo.remote_file("docs/estado.md") == "Todo en orden.\n"
    say(bot, "y el resumen", user=LUCIA, mention=True)
    assert data_repo.remote_file("docs/HEALTH.md") == "Resumen.\n"


def test_a_proposed_edit_waits_for_si_and_no_leaves_it(make_bot, data_repo):
    bot = make_bot()
    answer(bot, edit_block([NOTE], asked=False), edit_block([NOTE], asked=False))
    before = data_repo.remote_log()
    say(bot, "hoy he ido al centro", user=LUCIA, mention=True)
    ask = last_sent(bot)
    assert ask["text"].startswith("@lucia_tg, ¿hago este cambio?\n• people/lucia/notes.md\n+ ")
    assert ask["text"].endswith("Si no respondes, no hago nada.")
    assert data_repo.remote_log() == before
    no = buttons(ask)[1][1]
    tap(bot, no, user=PABLO)
    assert toasts(bot)[-1] == "Esto solo lo puede responder Lucía."
    tap(bot, no, user=LUCIA)
    assert bot.tg.texts()[-1] == "Vale, lo dejo como estaba." and data_repo.remote_log() == before
    say(bot, "otra vez", user=LUCIA, mention=True)
    tap(bot, buttons(last_sent(bot))[0][1], user=LUCIA)
    assert data_repo.remote_log()[0] == "chat(lucia): edit people/lucia/notes.md"


def test_deleting_a_file_or_most_of_one_asks_even_when_asked(make_bot, data_repo):
    bot = make_bot()
    meds_before = data_repo.remote_file("people/lucia/appointments.yaml")
    answer(
        bot,
        edit_block([{"path": "people/lucia/appointments.yaml", "delete": True}]),
        edit_block([{"path": "people/pablo/profile.yaml", "content": "name: Pablo\n"}]),
    )
    before = data_repo.remote_log()
    say(bot, "borra sus citas", user=PABLO, mention=True)
    ask = last_sent(bot)
    assert "• people/lucia/appointments.yaml: borrado" in ask["text"]
    assert "¿hago este cambio?" in ask["text"] and data_repo.remote_log() == before
    say(bot, "deja solo mi nombre", user=PABLO, mention=True)
    assert "¿hago este cambio?" in last_sent(bot)["text"] and data_repo.remote_log() == before
    tap(bot, buttons(ask)[0][1], user=PABLO)
    files = git(data_repo.remote, "ls-tree", "-r", "--name-only", "main").split()
    assert "people/lucia/appointments.yaml" not in files
    say(bot, "/deshacer", user=PABLO)
    assert data_repo.remote_file("people/lucia/appointments.yaml") == meds_before


def test_an_edit_that_breaks_the_data_is_refused_and_nothing_changes(make_bot, data_repo):
    bot = make_bot()
    meds = data_repo.data / "people" / "lucia" / "meds.yaml"
    before_text = meds.read_text()
    answer(
        bot,
        edit_block(
            [{"path": "people/lucia/meds.yaml", "old": "kind: medicine", "new": "kind: pill"}]
        ),
    )
    before = data_repo.remote_log()
    say(bot, "cambia el tipo", user=PABLO, mention=True)
    assert (
        bot.tg.texts()[-1] == "Ese cambio dejaba los datos con errores, así que no he tocado nada."
    )
    assert data_repo.remote_log() == before and meds.read_text() == before_text
    assert git(data_repo.data, "status", "--porcelain", "--untracked-files=no") == ""


def test_an_edit_of_the_instructions_that_drops_lines_is_asked(make_bot, data_repo):
    bot = make_bot()
    answer(
        bot,
        edit_block(
            [{"path": "settings.yaml", "old": "Secret household instruction.", "new": "Otra cosa."}]
        ),
    )
    before = data_repo.remote_log()
    say(bot, "cambia tus reglas", user=PABLO, mention=True)
    assert "¿hago este cambio?" in last_sent(bot)["text"]
    assert data_repo.remote_log() == before


def test_an_asked_settings_edit_by_an_admin_still_waits_for_an_admins_si(make_bot, data_repo):
    """Text quoted into an admin's turn must not grant admin rights or move a source unseen."""
    bot = make_bot()
    answer(
        bot,
        edit_block(
            [{"path": "settings.yaml", "old": "admins: [pablo]", "new": "admins: [pablo, lucia]"}]
        ),
    )
    before = data_repo.remote_log()
    say(bot, "mira este documento", user=PABLO, mention=True)
    ask = last_sent(bot)
    assert "¿hago este cambio?" in ask["text"]
    assert data_repo.remote_log() == before
    tap(bot, buttons(ask)[0][1], user=LUCIA)
    assert toasts(bot)[-1] == "Esto solo lo puede responder Pablo."
    assert data_repo.remote_log() == before
    tap(bot, buttons(ask)[0][1], user=PABLO)
    assert data_repo.remote_log()[0] == "chat(pablo): edit settings.yaml"
    assert "admins: [pablo, lucia]" in data_repo.remote_file("settings.yaml")


def test_a_replacement_whose_text_is_not_there_once_is_refused(make_bot, data_repo):
    bot = make_bot()
    answer(bot, edit_block([{"path": "people/lucia/profile.yaml", "old": "no existe", "new": "x"}]))
    before = data_repo.remote_log()
    say(bot, "x", user=PABLO, mention=True)
    assert bot.tg.texts()[-1].startswith("No he podido hacer ese cambio")
    assert data_repo.remote_log() == before


def test_a_si_after_the_file_moved_changes_nothing(make_bot, data_repo):
    bot = make_bot()
    answer(bot, edit_block([NOTE], asked=False))
    say(bot, "x", user=LUCIA, mention=True)
    ask = last_sent(bot)
    (data_repo.human / "people/lucia/notes.md").write_text("Escrito a mano.\n", encoding="utf-8")
    git(data_repo.human, "add", ".")
    git(data_repo.human, "commit", "--quiet", "-m", "a hand note")
    git(data_repo.human, "push", "--quiet")
    bot.repo._tried_at = None
    tap(bot, buttons(ask)[0][1], user=LUCIA)
    assert bot.tg.texts()[-1].startswith("Esos archivos han cambiado desde que lo pregunté")
    assert data_repo.remote_file("people/lucia/notes.md") == "Escrito a mano.\n"


def test_the_caps():
    many = [{"path": f"people/lucia/n{n}.md", "content": "x"} for n in range(11)]
    with pytest.raises(BlockError, match="too_many_edits"):
        edits.parse([{"edits": many}])
    with pytest.raises(BlockError, match="edit_too_large"):
        edits.parse([{"edits": [{"path": "people/lucia/n.md", "content": "x" * 20_001}]}])
    big = [{"path": f"people/lucia/n{n}.md", "content": "x" * 19_000} for n in range(4)]
    with pytest.raises(BlockError, match="edits_too_large"):
        edits.parse([{"edits": big}])
    with pytest.raises(BlockError, match="bad_edit"):
        edits.parse([{"edits": [{"path": "people/lucia/n.md", "content": "x", "delete": True}]}])


def test_editable_paths_in_settings_are_checked(tmp_path):
    from conftest import write_seed

    from supermedicaldoctor import records

    write_seed(tmp_path)
    for bad in ("plugins/x.md", "people/lucia/x.md", "docs/DESIGN.md", "../x.md", "docs/x.sh"):
        s = Settings.from_dict({**yaml.safe_load(SETTINGS), "editable": [bad]})
        assert "bad_editable" in records.settings_errors(s, tmp_path), bad
    s = Settings.from_dict({**yaml.safe_load(SETTINGS), "editable": ["docs/estado.md"]})
    assert records.settings_errors(s, tmp_path) == []


def test_the_deny_list_wins_even_over_an_editable_entry():
    """Defense in depth: `smd check` already refuses such an `editable` entry; the deny list
    refuses the path again on its own."""
    s = Settings.from_dict(
        {**yaml.safe_load(SETTINGS), "editable": ["plugins/x.md", "docs/DESIGN.md"]}
    )
    for path in ("plugins/x.md", "docs/DESIGN.md"):
        with pytest.raises(BlockError, match="path_never"):
            edits.allowed(
                path, s, scope=["lucia", "pablo"], asker="pablo", private=False, admin=True
            )


def test_a_private_chat_is_its_askers_folder_whatever_the_scope_says():
    """Defense in depth: a private turn's scope is already the asker alone; the private rule
    holds even if a wider scope ever reached it."""
    s = Settings.from_dict(yaml.safe_load(SETTINGS))
    with pytest.raises(BlockError, match="not_your_folder"):
        edits.allowed(
            "people/pablo/notes.md",
            s,
            scope=["lucia", "pablo"],
            asker="lucia",
            private=True,
            admin=False,
        )
    edits.allowed(
        "people/lucia/notes.md",
        s,
        scope=["lucia", "pablo"],
        asker="lucia",
        private=True,
        admin=False,
    )
