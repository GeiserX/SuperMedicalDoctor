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
        "¿Lo apunto?\n\nToca Sí o No. Si quieres otra cosa, responde a este mensaje."
    )
    yes = buttons(answer)[0][1]
    tap(bot, yes, user=PABLO)
    assert toasts(bot)[-1] == "Esto solo lo puede responder Lucía."
    tap(bot, yes)
    assert contents(plugin(bot).CALLS[-1])[-1] == "Lucía: Sí"


def test_choices_carry_no_line_under_them(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["¿Cuál?\n\n```options\n- Mañana\n- Tarde\n```"]
    say(bot, "x", mention=True)
    # the buttons say what they do, and untapped they expire: no line under the question
    assert last_sent(bot)["text"] == "¿Cuál?"


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


# --- replies to the bot, in every shape Telegram sends them --------------------------------


def test_a_reply_to_a_bot_answer_with_media_queues_a_turn_with_its_caption(make_bot):
    bot = make_bot()
    msg = message("¿y el dermatólogo?", user=PABLO)
    msg["reply_to_message"] = {
        "message_id": 18,
        "from": {"id": 999, "is_bot": True},
        "document": {"file_id": "d1", "file_name": "especialidades.pdf"},
        "caption": "Especialidades que cubre el seguro",
    }
    bot.handle({"message": msg})
    bot.join_idle()
    assert contents(plugin(bot).CALLS[-1])[-1] == (
        "Pablo: [replying to the bot: Especialidades que cubre el seguro] ¿y el dermatólogo?"
    )


def test_a_reply_from_another_topic_or_chat_reaches_the_bot(make_bot):
    """Telegram sends a reply to a message of another topic as `external_reply`, with no
    `reply_to_message`."""
    bot = make_bot()
    msg = message("¿y esto?", user=PABLO)
    msg["external_reply"] = {
        "origin": {"type": "user", "sender_user": {"id": 999, "is_bot": True}, "date": 1},
        "chat": {"id": GROUP, "type": "supergroup"},
        "message_id": 18,
    }
    msg["quote"] = {"text": "cubre dermatología", "position": 0}
    bot.handle({"message": msg})
    bot.join_idle()
    assert contents(plugin(bot).CALLS[-1])[-1] == (
        "Pablo: [replying to the bot: cubre dermatología] ¿y esto?"
    )


def test_a_reply_to_a_remembered_message_without_its_sender_reaches_the_bot(make_bot):
    bot = make_bot()
    say(bot, "hola", mention=True)
    mine = bot.kv.get_json(f"own:{GROUP}")[-1]
    msg = message("vale", user=PABLO)
    msg["external_reply"] = {"origin": {"type": "hidden_user"}, "message_id": mine}
    bot.handle({"message": msg})
    bot.join_idle()
    assert contents(plugin(bot).CALLS[-1])[-1] == "Pablo: [replying to the bot: ] vale"


# --- replies to a person's earlier message ---------------------------------------------------

EARLIER = 1792000000  # 2026-10-14T17:46:40+00:00
SAID = "of 2026-10-14T17:46:40+00:00)"


def _earlier(user=PABLO, **kind):
    return {"message_id": 40, "from": {"id": user}, "date": EARLIER, **kind}


def test_a_reply_to_someones_earlier_photo_says_whose_what_and_when(make_bot):
    """The bot never saw that photo: the line must name it, by its date, so the assistant can
    find it in the chat's history ("¿has visto mi foto?")."""
    bot = make_bot()
    msg = message("¿la has visto?", mention=True)
    msg["reply_to_message"] = _earlier(photo=[{"file_id": "p0", "width": 90}])
    bot.handle({"message": msg})
    bot.join_idle()
    assert contents(plugin(bot).CALLS[-1])[-1] == (
        f"Lucía (replying to Pablo's photo {SAID}: ¿la has visto?"
    )


def test_the_kind_of_the_replied_message_is_named(make_bot):
    bot = make_bot()
    for kind, said in (
        ({"document": {"file_id": "d0"}}, "file"),
        ({"video_note": {"file_id": "n0"}}, "video"),
        ({"voice": {"file_id": "v0"}}, "voice"),
        ({"text": "mañana voy"}, "text"),
    ):
        msg = message("¿y esto?", mention=True)
        msg["reply_to_message"] = _earlier(user=LUCIA, **kind)
        bot.handle({"message": msg})
        bot.join_idle()
        assert contents(plugin(bot).CALLS[-1])[-1] == (
            f"Lucía (replying to Lucía's {said} {SAID}: ¿y esto?"
        )


def test_a_reply_in_a_photo_a_voice_note_and_unaddressed_talk_names_it_too(make_bot):
    bot = make_bot()
    say(bot, "hola", mention=True)
    msg = message("", user=PABLO)
    del msg["text"]
    msg["photo"] = [{"file_id": "big", "file_size": 3, "width": 1280}]
    msg["reply_to_message"] = _earlier(user=LUCIA, text="¿qué te dijo?")
    bot.tg.files["big"] = b"BIG"
    bot.handle({"message": msg})
    bot.join_idle()
    assert contents(plugin(bot).CALLS[-1])[-1].startswith(
        f"Pablo (replying to Lucía's text {SAID}: [foto: inbox/"
    )
    plugin(bot).VOICES[:] = ["sí, esa"]
    bot.tg.files["v1"] = b"OggS"
    msg = message("", user=PABLO)
    del msg["text"]
    msg["voice"] = {"file_id": "v1"}
    msg["reply_to_message"] = _earlier(user=LUCIA, text="¿qué te dijo?")
    bot.handle({"message": msg})
    bot.join_idle()
    assert contents(plugin(bot).CALLS[-1])[-1] == (
        f"Pablo (replying to Lucía's text {SAID}: [voice note] sí, esa"
    )
    msg = message("ya te dije")
    msg["reply_to_message"] = _earlier(photo=[{"file_id": "p0"}])
    bot.handle({"message": msg})
    say(bot, "¿y tú?", mention=True)
    assert f"[not to you] Lucía (replying to Pablo's photo {SAID}: ya te dije" in contents(
        plugin(bot).CALLS[-1]
    )


def test_a_reply_to_the_bot_or_to_the_start_of_a_topic_names_no_earlier_message(make_bot):
    bot = make_bot()
    msg = message("vale", user=PABLO)
    msg["reply_to_message"] = {**_earlier(user=999), "from": {"id": 999, "is_bot": True}}
    msg["reply_to_message"]["text"] = "¿Algo más?"
    bot.handle({"message": msg})
    bot.join_idle()
    assert contents(plugin(bot).CALLS[-1])[-1] == "Pablo: [replying to the bot: ¿Algo más?] vale"
    msg = message("¿y hoy?", mention=True, thread=7)
    msg["reply_to_message"] = {
        **_earlier(),
        "message_id": 7,
        "forum_topic_created": {"name": "Citas"},
    }
    bot.handle({"message": msg})
    bot.join_idle()
    assert contents(plugin(bot).CALLS[-1])[-1] == "Lucía: ¿y hoy?"


def test_every_message_the_bot_sends_is_remembered(make_bot):
    """Answers (split or not), «Anotado» lines, offers and asks: a reply to any of them is
    for the bot."""
    from helpers import record

    bot = make_bot()
    block = {
        "person": "lucia",
        "appointments": [{"op": "add", "date": "2027-03-16", "time": "9:00", "what": "Eco"}],
    }

    def asks(turn):
        turn["ask"]({"id": "q", "summary": "Algo", "confirm_by": "admins"})
        return record("x" * 5000 + "\n\n" + "y" * 10, block) + (
            "\n\n```reminders\n- 2027-03-11 08:00 | Hierro\n```"
        )

    plugin(bot).REPLIES[:] = [asks]
    say(bot, "hola", mention=True)
    sent_ids = list(range(1001, 1001 + len(sent(bot))))
    assert len(sent_ids) >= 5  # two parts, the ask, «Anotado» with its offer, the reminder offer
    assert bot.kv.get_json(f"own:{GROUP}") == sent_ids


def test_a_group_text_not_for_the_bot_says_why_in_the_log(make_bot, caplog):
    import logging

    caplog.set_level(logging.INFO)
    bot = make_bot()
    other = message("¿vienes?", user=PABLO)
    other["reply_to_message"] = {"message_id": 3, "from": {"id": LUCIA}, "text": "voy"}
    bot.handle({"message": other})
    bot.handle({"message": message("hola a todos", user=PABLO)})
    anon = message("hola", user=1087968824)
    anon["sender_chat"] = {"id": GROUP, "type": "supergroup"}
    bot.handle({"message": anon})
    bot.join_idle()
    assert "group text not for the bot: reply to someone else" in caplog.text
    assert "group text not for the bot: not a reply and no mention" in caplog.text
    assert "group text from an anonymous admin or a channel: not a person" in caplog.text
    assert "¿vienes?" not in caplog.text and "hola a todos" not in caplog.text
