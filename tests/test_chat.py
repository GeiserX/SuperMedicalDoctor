"""The shared chat machinery: edits, choices as buttons, voice notes that cannot be heard, `later`,
offers that expire, and the chat log as context."""

import time

from conftest import GROUP, LUCIA, PABLO
from helpers import buttons, contents, last_sent, message, plugin, say, sent, tap, toasts

# --- edits ---------------------------------------------------------------------------------


def test_an_edit_of_an_answered_message_asks_to_check_the_answer(make_bot):
    bot = make_bot()
    msg = say(bot, "¿puedo tomar ibuprofeno?", mention=True)
    bot.handle({"edited_message": {**msg, "text": "@example_doctor_bot ¿puedo tomar paracetamol?"}})
    bot.join_idle()
    last = contents(plugin(bot).CALLS[-1])[-1]
    assert last.startswith("[Lucía edited a message you already answered.")
    assert "Before: ¿puedo tomar ibuprofeno?" in last
    assert "Now: ¿puedo tomar paracetamol?" in last
    assert "Your answer was: Vale." in last


def test_an_edit_that_changes_no_word_or_is_a_command_does_nothing(make_bot):
    bot = make_bot()
    msg = say(bot, "hola", mention=True)
    bot.handle({"edited_message": {**msg, "text": "@example_doctor_bot  hola "}})
    bot.handle({"edited_message": {**msg, "text": "/citas"}})
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 1


def test_an_edit_that_adds_the_mention_is_a_new_message(make_bot):
    bot = make_bot()
    msg = say(bot, "tengo fiebre")
    assert plugin(bot).CALLS == []
    edited = {
        **msg,
        "text": "@example_doctor_bot tengo fiebre",
        "entities": [{"type": "mention", "offset": 0, "length": 19}],
    }
    bot.handle({"edited_message": edited})
    bot.join_idle()
    assert contents(plugin(bot).CALLS[0]) == ["Lucía: tengo fiebre"]
    assert len(bot.kv.chat_rows(GROUP, 0)) == 2  # the line and the answer: one row each


# --- choices -------------------------------------------------------------------------------


def test_choices_come_as_buttons_and_a_tap_is_that_persons_next_message(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["¿Lo apunto?\n\n```options\n- Sí\n- No\n```"]
    say(bot, "me han puesto hierro", mention=True)
    answer = last_sent(bot)
    assert answer["text"] == (
        "¿Lo apunto?\n\nToca Sí o No. Si quieres otra cosa, responde a este mensaje. Si no "
        "respondes, no hago nada."
    )
    yes = buttons(answer)[0][1]
    tap(bot, yes, user=PABLO)
    assert toasts(bot)[-1] == "Esto solo lo puede responder Lucía."
    tap(bot, yes)
    assert contents(plugin(bot).CALLS[-1])[-1] == "Lucía: Sí"


def test_choices_nobody_taps_expire_into_nothing(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["¿Cuál?\n\n```options\n- Mañana\n- Tarde\n```"]
    say(bot, "x", mention=True)
    bot.now.advance(hours=13)
    bot.tick(bot.now())
    last = bot.tg.of("editMessageReplyMarkup")[-1]
    assert last["reply_markup"]["inline_keyboard"][0][0]["text"] == "Sin respuesta: nada"


# --- voice ---------------------------------------------------------------------------------


def _voice(bot, file_id="v1", size=None, user=LUCIA):
    msg = message("", user=user)
    del msg["text"]
    msg["voice"] = {"file_id": file_id, "duration": 3}
    if size is not None:
        msg["voice"]["file_size"] = size
    bot.handle({"message": msg})
    bot.join_idle()
    return msg


def test_a_voice_note_the_plugin_cannot_hear_says_so_under_it(make_bot):
    bot = make_bot()
    bot.tg.files["v1"] = b"OggS"
    msg = _voice(bot)  # VOICES empty: the plugin cannot transcribe
    reply = last_sent(bot)
    assert reply["text"] == "Ahora mismo no puedo escuchar notas de voz. ¿Me lo escribes?"
    assert reply["reply_parameters"]["message_id"] == msg["message_id"]
    assert plugin(bot).CALLS == []


def test_a_voice_note_over_twenty_megabytes_is_refused_on_arrival(make_bot):
    bot = make_bot()
    _voice(bot, size=21 * 1024 * 1024)
    assert last_sent(bot)["text"].startswith("Esa nota de voz es muy larga")


def test_an_empty_transcript_is_a_failure(make_bot):
    bot = make_bot()
    plugin(bot).VOICES[:] = ["   "]
    bot.tg.files["v1"] = b"OggS"
    _voice(bot)
    assert last_sent(bot)["text"] == "No he entendido la nota de voz. ¿Me lo escribes?"


def test_a_voice_note_from_pablo_in_his_private_chat_is_heard(make_bot):
    bot = make_bot()
    plugin(bot).VOICES[:] = ["¿cuándo es mi cita?"]
    bot.tg.files["v1"] = b"OggS"
    msg = message("", user=PABLO, private=True)
    del msg["text"]
    msg["voice"] = {"file_id": "v1"}
    bot.handle({"message": msg})
    bot.join_idle()
    turn = plugin(bot).CALLS[0]["turn"]
    assert turn["scope"] == ["pablo"]
    assert contents(plugin(bot).CALLS[0]) == ["Pablo: [voice note] ¿cuándo es mi cita?"]


def test_a_forget_that_exits_does_not_stop_nuevo(make_bot):
    bot = make_bot()
    say(bot, "hola", mention=True)

    def exits(conversation, settings):
        raise SystemExit(1)

    plugin(bot).forget = exits
    say(bot, "/nuevo", user=PABLO)
    assert last_sent(bot)["text"] == "Empiezo una conversación nueva. Lo anotado sigue igual."


# --- later ---------------------------------------------------------------------------------


def test_later_posts_after_the_answer_and_a_failure_says_so(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [
        {"text": "Lo miro.", "kept": True, "later": lambda: "Ya está."},
        {"text": "Otra.", "kept": True, "later": lambda: 1 / 0},
    ]
    say(bot, "uno", mention=True)
    deadline = time.monotonic() + 5
    while "Ya está." not in bot.tg.texts() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert bot.tg.texts()[:2] == ["Lo miro.", "Ya está."]
    say(bot, "dos", mention=True)
    deadline = time.monotonic() + 5
    while len(bot.tg.texts()) < 4 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (
        bot.tg.texts()[-1]
        == "No he podido terminar lo que estaba haciendo. Pídemelo otra vez más tarde."
    )


# --- the chat log --------------------------------------------------------------------------


def test_the_recap_is_the_chat_log_without_the_new_lines(make_bot):
    bot = make_bot()
    say(bot, "primera", mention=True)
    say(bot, "segunda", mention=True)
    history = plugin(bot).CALLS[-1]["turn"]["history"]
    assert [h["content"] for h in history] == ["Lucía: primera", "Vale."]


def test_an_answer_ending_in_a_question_opens_the_reply_box(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["¿Desde cuándo?"]
    say(bot, "me duele", mention=True)
    assert last_sent(bot)["reply_markup"] == {"force_reply": True, "selective": True}


def test_the_bot_remembers_the_ids_of_its_own_messages(make_bot):
    bot = make_bot()
    say(bot, "hola", mention=True)
    mine = bot.kv.get_json(f"own:{GROUP}")
    assert len(mine) == len(sent(bot)) == 1
    msg = message("y otra cosa", user=PABLO)
    msg["reply_to_message"] = {"message_id": mine[-1], "text": "Vale."}  # no `from`: by id
    bot.handle({"message": msg})
    bot.join_idle()
    assert contents(plugin(bot).CALLS[-1])[-1] == "Pablo: [replying to the bot: Vale.] y otra cosa"
