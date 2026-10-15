"""The turn at the seam with the plugin (CONTRACT 3): who reaches the assistant, the scope, the
files, the sources, the attachments and the calendars it gets, and one answer per burst."""

import time

import pytest
from conftest import GROUP, LUCIA, PABLO, STRANGER
from helpers import contents, message, plugin, say, sent

# --- who reaches the assistant -------------------------------------------------------------


def test_a_mention_or_a_reply_reaches_the_assistant_and_plain_group_text_does_not(make_bot):
    bot = make_bot()
    say(bot, "hola a todos")
    assert plugin(bot).CALLS == []
    say(bot, "¿me tomo el hierro?", mention=True)
    say(bot, "¿y el omega?", user=PABLO, reply=True)
    calls = plugin(bot).CALLS
    assert len(calls) == 2
    assert contents(calls[0])[-1] == "Lucía: ¿me tomo el hierro?"
    assert contents(calls[1])[-1].startswith("Pablo: [replying to the bot: ")


def test_unaddressed_group_talk_is_context_marked_not_to_you(make_bot):
    bot = make_bot()
    say(bot, "mañana tengo médico")
    say(bot, "¿qué llevo?", mention=True)
    assert contents(plugin(bot).CALLS[0]) == [
        "[not to you] Lucía: mañana tengo médico",
        "Lucía: ¿qué llevo?",
    ]


def test_strangers_are_never_answered_and_their_private_chats_ignored(make_bot):
    bot = make_bot()
    say(bot, "hola", user=STRANGER, mention=True)
    say(bot, "hola", user=STRANGER, private=True)
    assert plugin(bot).CALLS == []
    assert sent(bot) == []


def test_another_chat_is_ignored(make_bot):
    bot = make_bot()
    say(bot, "hola", chat=-1009999, mention=True)
    assert plugin(bot).CALLS == []


# --- the turn's shape ----------------------------------------------------------------------


def test_a_group_turn_carries_everyone_and_the_contract_keys(make_bot):
    bot = make_bot()
    say(bot, "¿qué tomo?", mention=True)
    turn = plugin(bot).CALLS[0]["turn"]
    settings = plugin(bot).CALLS[0]["settings"]
    assert turn["scope"] == ["lucia", "pablo"]
    assert turn["asker"] == "lucia"
    assert set(turn["files"]) == {
        "data/settings.yaml",
        "data/people/lucia/profile.yaml",
        "data/people/lucia/meds.yaml",
        "data/people/lucia/appointments.yaml",
        "data/people/lucia/labs.jsonl",
        "data/people/pablo/profile.yaml",
        "data/people/pablo/meds.yaml",
        "data/people/pablo/appointments.yaml",
    }
    assert "Secret household instruction" not in turn["files"]["data/settings.yaml"]
    assert "Secret household instruction" in turn["rules"]
    assert turn["calendars"] == {"lucia": "cal-lucia", "pablo": "cal-pablo"}
    assert turn["sources"] == {} and turn["attachments"] == []
    assert "plugin" not in settings and "instructions" not in settings
    assert settings["people"]["lucia"] == {
        "name": "Lucía",
        "user_id": LUCIA,
        "username": "lucia_tg",
        "calendar": "cal-lucia",
    }
    assert settings["chat_id"] == GROUP
    assert "Analítica de control" in turn["context"]


def test_a_private_turn_carries_only_the_asker(make_bot):
    bot = make_bot()
    say(bot, "¿qué toma Lucía?", user=PABLO, private=True)
    turn = plugin(bot).CALLS[0]["turn"]
    assert turn["scope"] == ["pablo"]
    assert turn["asker"] == "pablo"
    assert not any("lucia" in k for k in turn["files"])
    assert turn["calendars"] == {"pablo": "cal-pablo"}
    assert "Levotiroxina" not in turn["context"] and "Analítica" not in turn["context"]


def test_a_turn_with_two_peoples_messages_has_no_asker(make_bot):
    bot = make_bot()
    bot.debounce_s = 2.5
    now = [0.0]
    bot.clock = lambda: now[0]
    second = message("y yo", user=PABLO, mention=True)
    fired = []

    def sleep(seconds):
        if now[0] >= 1.0 and not fired:
            fired.append(1)
            bot.handle({"message": second})
        now[0] += seconds

    bot.sleep = sleep
    bot.handle({"message": message("tengo dudas", mention=True)})
    bot.join_idle()
    turn = plugin(bot).CALLS[0]["turn"]
    assert turn["asker"] is None
    assert len(plugin(bot).CALLS) == 1  # one answer for the burst


def test_the_turn_reads_the_latest_commit(make_bot, data_repo):
    from conftest import git

    bot = make_bot()
    (data_repo.human / "people/pablo/appointments.yaml").write_text(
        "- {id: 2027-03-20-x-dentista, date: 2027-03-20, time: null, what: Dentista, "
        "doctor: null, place: null, prep: [], chat: group, status: pending, outcome: ''}\n",
        encoding="utf-8",
    )
    git(data_repo.human, "commit", "--quiet", "-am", "a hand edit")
    git(data_repo.human, "push", "--quiet")
    bot.repo._tried_at = None  # the last pull is not fresh
    say(bot, "¿cuándo es el dentista?", user=PABLO, mention=True)
    assert (
        "Dentista" in plugin(bot).CALLS[0]["turn"]["files"]["data/people/pablo/appointments.yaml"]
    )


def test_a_failed_pull_answers_from_the_last_copy_and_says_so(make_bot, data_repo):
    import shutil

    bot = make_bot()
    shutil.rmtree(data_repo.remote)
    bot.repo._tried_at = None
    say(bot, "hola", mention=True)
    ctx = plugin(bot).CALLS[0]["turn"]["context"]
    assert "No se pudo actualizar: es la última copia buena." in ctx


# --- photos and files ----------------------------------------------------------------------


def _doc(user=LUCIA, file_id="pdf1", name="Analítica 2027.pdf", mime="application/pdf", cap=""):
    msg = message("", user=user)
    del msg["text"]
    msg["document"] = {"file_id": file_id, "file_name": name, "mime_type": mime, "file_size": 10}
    if cap:
        msg["caption"] = cap
    return msg


def test_a_pdf_in_the_group_goes_to_the_assistant_as_an_attachment(make_bot):
    bot = make_bot()
    bot.tg.files["pdf1"] = b"%PDF-1.7 fake"
    msg = _doc(cap="mis análisis")
    bot.handle({"message": msg})
    bot.join_idle()
    turn = plugin(bot).CALLS[0]["turn"]
    name = f"inbox/{msg['message_id']}-Analitica-2027.pdf"
    assert contents(plugin(bot).CALLS[0]) == [f"Lucía: [archivo: {name}] mis análisis"]
    assert turn["attachments"] == [
        {"name": name, "mime": "application/pdf", "data": b"%PDF-1.7 fake", "from": "lucia"}
    ]


def test_a_photo_takes_the_largest_size(make_bot):
    bot = make_bot()
    bot.tg.files.update({"small": b"s", "big": b"BIG"})
    msg = message("", user=PABLO)
    del msg["text"]
    msg["photo"] = [
        {"file_id": "small", "file_size": 1, "width": 90},
        {"file_id": "big", "file_size": 3, "width": 1280},
    ]
    bot.handle({"message": msg})
    bot.join_idle()
    att = plugin(bot).CALLS[0]["turn"]["attachments"]
    assert att[0]["name"] == f"inbox/{msg['message_id']}-foto.jpg" and att[0]["data"] == b"BIG"
    assert contents(plugin(bot).CALLS[0])[0].startswith("Pablo: [foto: inbox/")


@pytest.mark.parametrize(
    "name, mime, saved",
    [
        (
            "Informe alta.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "Informe-alta.docx",
        ),
        ("tensión.csv", "text/csv", "tension.csv"),
        ("Analitica", "application/pdf", "Analitica.pdf"),
        ("", "", "documento"),
    ],
)
def test_a_file_of_any_kind_goes_to_the_assistant_with_its_own_name(make_bot, name, mime, saved):
    bot = make_bot()
    bot.tg.files["f1"] = b"bytes"
    msg = _doc(file_id="f1", name=name, mime=mime)
    if not mime:
        del msg["document"]["mime_type"]
    bot.handle({"message": msg})
    bot.join_idle()
    path = f"inbox/{msg['message_id']}-{saved}"
    assert contents(plugin(bot).CALLS[0]) == [f"Lucía: [archivo: {path}]"]
    assert plugin(bot).CALLS[0]["turn"]["attachments"] == [
        {
            "name": path,
            "mime": mime or "application/octet-stream",
            "data": b"bytes",
            "from": "lucia",
        }
    ]


def test_a_file_too_big_is_refused_with_a_line(make_bot):
    bot = make_bot()
    big = _doc(file_id="big", name="x.zip", mime="application/zip")
    big["document"]["file_size"] = 21 * 1024 * 1024
    bot.handle({"message": big})
    bot.join_idle()
    assert plugin(bot).CALLS == []
    assert "demasiado grande" in bot.tg.texts()[0]


def test_files_past_the_turn_budget_go_in_a_turn_of_their_own(make_bot, monkeypatch):
    from supermedicaldoctor import bot as botmod

    monkeypatch.setattr(botmod, "TURN_FILES_MAX", 10)
    bot = make_bot()
    bot.tg.files.update({"a": b"12345678", "b": b"12345678"})
    first, second = _doc(file_id="a", name="a.pdf"), _doc(file_id="b", name="b.pdf")
    bot.kv.update_json(  # both queued before any turn runs, as a burst would leave them
        f"pending:{GROUP}",
        lambda cur: [
            {**bot._base_line(m, bot.settings.people["lucia"]), "file": f, "content": c, "body": ""}
            for m, f, c in (
                (
                    first,
                    {"file_id": "a", "name": "inbox/1-a.pdf", "mime": "application/pdf", "size": 8},
                    "Lucía: [archivo: inbox/1-a.pdf]",
                ),
                (
                    second,
                    {"file_id": "b", "name": "inbox/2-b.pdf", "mime": "application/pdf", "size": 8},
                    "Lucía: [archivo: inbox/2-b.pdf]",
                ),
            )
        ],
        [],
    )
    bot.worker.submit(GROUP)  # one submit only: the second turn must come from the first
    bot.join_idle()
    calls = plugin(bot).CALLS
    assert [[a["name"] for a in c["turn"]["attachments"]] for c in calls] == [
        ["inbox/1-a.pdf"],
        ["inbox/2-b.pdf"],
    ]


def test_a_file_that_cannot_be_fetched_says_so_and_the_line_goes_on(make_bot):
    bot = make_bot()
    bot.handle({"message": _doc(file_id="missing")})
    bot.join_idle()
    assert "No he podido descargar ese archivo" in bot.tg.texts()[0]
    assert plugin(bot).CALLS[0]["turn"]["attachments"] == []


# --- without the assistant -----------------------------------------------------------------


def test_without_an_assistant_text_waits_and_is_read_once_one_is_configured(make_bot, data_repo):
    from conftest import SETTINGS, git

    no_plugin = SETTINGS.replace("  plugin: plugins/fake.py\n", "")
    bot = make_bot(settings=no_plugin)
    say(bot, "¿qué tomo?", mention=True)
    assert bot.tg.texts() == [
        "Ahora mismo no puedo leer mensajes escritos; en cuanto pueda, lo leo."
    ]
    assert len(bot.kv.get_json(f"pending:{GROUP}")) == 1
    (data_repo.human / "settings.yaml").write_text(SETTINGS, encoding="utf-8")
    git(data_repo.human, "commit", "--quiet", "-am", "plugin back")
    git(data_repo.human, "push", "--quiet")
    bot.repo._tried_at = None
    bot.tick()
    bot.join_idle()
    assert contents(plugin(bot).CALLS[0]) == ["Lucía: ¿qué tomo?"]


def test_a_failed_turn_keeps_the_lines_for_the_next(make_bot):
    bot = make_bot()
    plugin(bot).DOWN = True
    say(bot, "uno", mention=True)
    assert bot.tg.texts()[-1].startswith("Ahora mismo no puedo leer mensajes escritos.")
    plugin(bot).DOWN = False
    say(bot, "dos", mention=True)
    assert contents(plugin(bot).CALLS[-1])[-2:] == ["Lucía: uno", "Lucía: dos"]


# --- forum topics --------------------------------------------------------------------------


def test_the_answer_goes_to_the_topic_the_message_came_from(make_bot):
    bot = make_bot()
    say(bot, "hola", mention=True, thread=77)
    answer = next(p for p in sent(bot) if p["text"] == "Vale.")
    assert answer["message_thread_id"] == 77
    assert bot.tg.of("sendChatAction")[-1]["message_thread_id"] == 77


def test_a_voice_note_is_heard_and_echoed_in_its_topic(make_bot):
    bot = make_bot()
    plugin(bot).VOICES[:] = ["me duele la cabeza"]
    bot.tg.files["v1"] = b"OggS"
    msg = message("", thread=5)
    del msg["text"]
    msg["voice"] = {"file_id": "v1", "duration": 2}
    bot.handle({"message": msg})
    bot.join_idle()
    echo = sent(bot)[0]
    assert echo["text"] == "🎤 me duele la cabeza" and echo["message_thread_id"] == 5
    assert contents(plugin(bot).CALLS[0]) == ["Lucía: [voice note] me duele la cabeza"]


def test_a_handed_over_message_joins_the_running_turn(make_bot):
    bot = make_bot()
    late = message("y otra cosa", mention=True)

    def handover(turn):
        bot.handle({"message": late})
        got = turn["incoming"]()
        assert [g["content"] for g in got] == ["Lucía: y otra cosa"]
        return {"text": "Las dos.", "conversation": "c", "kept": True, "covers": [got[0]["id"]]}

    plugin(bot).REPLIES[:] = [handover]
    say(bot, "una cosa", mention=True)
    assert bot.tg.texts() == ["Las dos."]
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_a_file_arriving_mid_turn_is_not_handed_over(make_bot):
    bot = make_bot()
    bot.tg.files["pdf1"] = b"%PDF"
    got = []

    def handover(turn):
        bot.handle({"message": _doc()})
        got.append(turn["incoming"]())
        return "Vale."

    plugin(bot).REPLIES[:] = [handover]
    say(bot, "hola", mention=True)
    deadline = time.monotonic() + 5
    while len(plugin(bot).CALLS) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    bot.join_idle()
    assert got == [[]]
    assert len(plugin(bot).CALLS[1]["turn"]["attachments"]) == 1


def test_loading_the_plugin_leaves_no_bytecode_in_the_data_checkout(
    make_bot, data_repo, monkeypatch
):
    monkeypatch.setattr("sys.dont_write_bytecode", False)
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    bot = make_bot()
    say(bot, "hola", mention=True)
    assert plugin(bot).CALLS
    assert not (data_repo.data / "plugins" / "__pycache__").exists()
    import sys

    assert sys.dont_write_bytecode is False  # put back as it was


# --- nothing waits for someone to write again ----------------------------------------------


def test_a_handed_over_message_the_answer_did_not_cover_gets_its_own_turn(make_bot):
    """Its own arrival queued the chat again, so it is answered without anyone writing."""
    bot = make_bot()
    late = message("y otra cosa", mention=True)

    def handover(turn):
        bot.handle({"message": late})
        assert len(turn["incoming"]()) == 1
        return {"text": "Primera.", "conversation": "c", "kept": True, "covers": []}

    plugin(bot).REPLIES[:] = [handover]
    say(bot, "una cosa", mention=True)
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 2  # nobody wrote again
    assert contents(plugin(bot).CALLS[1]) == ["Lucía: y otra cosa"]
    assert bot.tg.texts() == ["Primera.", "Vale."]


def _restart(bot, data_repo):
    """The old process is gone (its threads with it); a new one starts on the same data."""
    from conftest import FakeTelegram

    from supermedicaldoctor.bot import Bot

    bot.close()
    new = Bot(data_repo.data, FakeTelegram(), now=bot.now)
    new.debounce_s = 0
    new.start()
    new.resume()
    new.join_idle()
    return new


def test_a_line_queued_before_a_restart_is_answered_after_it(make_bot, data_repo):
    bot = make_bot()
    bot.worker.stop()  # the process dies before the turn starts
    bot.handle({"message": message("¿qué tomo?", mention=True)})
    assert len(bot.kv.get_json(f"pending:{GROUP}")) == 1
    new = _restart(bot, data_repo)
    try:
        assert contents(plugin(new).CALLS[-1]) == ["Lucía: ¿qué tomo?"]
        assert new.tg.texts() == ["Vale."]
        assert new.kv.get_json(f"pending:{GROUP}") is None
    finally:
        new.close()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_turn_killed_mid_flight_is_replayed_after_a_restart(make_bot, data_repo):
    from supermedicaldoctor.bot import Interrupted

    bot = make_bot()

    def dies(*a, **kw):
        raise Interrupted  # not an Exception: it ends the worker as a SIGKILL ends a process

    bot._converse = dies
    bot.handle({"message": message("¿qué tomo?", mention=True)})
    deadline = time.monotonic() + 5
    while bot.worker._thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not bot.worker._thread.is_alive() and bot.tg.texts() == []
    assert len(bot.kv.get_json(f"pending:{GROUP}")) == 1  # the input outlived the turn
    new = _restart(bot, data_repo)
    try:
        assert contents(plugin(new).CALLS[-1]) == ["Lucía: ¿qué tomo?"]
        assert new.tg.texts() == ["Vale."]
        assert new.kv.get_json(f"pending:{GROUP}") is None
    finally:
        new.close()
