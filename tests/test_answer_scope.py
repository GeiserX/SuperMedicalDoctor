"""`answer_scope: all`: in the group, a person's text without a mention or a reply starts a turn
as an addressed one does, marked «[no mention]», and the assistant may answer it with a reaction
or with nothing to say («[[no reply]]»). A person's message is never left with nothing to show for
it: an answer that posts nothing marks the turn's messages with 👀 (read)."""

from conftest import GROUP, LUCIA, PABLO, SETTINGS, STRANGER
from helpers import contents, message, plugin, say, sent

from supermedicaldoctor import assistant

# answer_scope: all without history_scope: all: the setting implies the context it needs
ALL = SETTINGS.replace("history_scope: all", "history_scope: asked\n  answer_scope: all")


def _rules(bot):
    return plugin(bot).CALLS[-1]["turn"]["rules"]


def _reactions(bot):
    """(message_id, emoji) of every reaction asked of Telegram, refused ones included."""
    return [(r["message_id"], r["reaction"][0]["emoji"]) for r in bot.tg.of("setMessageReaction")]


def _burst(bot, first, second):
    """`first` starts a turn and `second` arrives within its debounce: one turn covers both."""
    bot.debounce_s = 2.5
    now = [0.0]
    bot.clock = lambda: now[0]
    fired = []

    def sleep(seconds):
        if now[0] >= 1.0 and not fired:
            fired.append(1)
            bot.handle({"message": second})
            bot.eyes.join_idle()  # both arrival 👀 are done before the turn takes the lines
        now[0] += seconds

    bot.sleep = sleep
    bot.handle({"message": first})
    bot.join_idle()


def test_a_line_without_a_mention_starts_a_turn_marked_no_mention(make_bot):
    bot = make_bot(settings=ALL)
    say(bot, "quedan 10 raciones de lasaña")
    [call] = plugin(bot).CALLS
    assert contents(call) == ["[no mention] Lucía: quedan 10 raciones de lasaña"]
    assert call["turn"]["asker"] == "lucia"
    assert bot.tg.texts() == ["Vale."]
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_a_reply_to_another_person_starts_a_turn_too_and_says_whom_it_answers(make_bot):
    bot = make_bot(settings=ALL)
    msg = message("ya lo saqué", user=PABLO)
    msg["reply_to_message"] = {"message_id": 7, "from": {"id": LUCIA}, "date": 1, "text": "x"}
    bot.handle({"message": msg})
    bot.join_idle()
    [line] = contents(plugin(bot).CALLS[0])
    assert line.startswith("[no mention] Pablo (replying to Lucía's text of ")


def test_a_mention_keeps_its_plain_line(make_bot):
    bot = make_bot(settings=ALL)
    say(bot, "¿qué tomo?", mention=True)
    assert contents(plugin(bot).CALLS[0]) == ["Lucía: ¿qué tomo?"]


def test_the_rules_say_how_to_answer_a_line_without_a_mention(make_bot):
    bot = make_bot(settings=ALL)
    say(bot, "hola")
    rules = " ".join(_rules(bot).split())
    assert '"[no mention]"' in rules
    assert "answer with only a reaction" in rules and "react: 👍" in rules
    assert "say briefly that you took note" in rules
    assert "your whole answer is [[no reply]]" in rules
    assert "their messages keep the 👀 (read) the bot put on them when it started reading" in rules
    assert "a 👍 means noted" in rules
    assert "sends nothing" not in rules
    assert "a mention of you or a reply to you always gets an answer in words" in rules.lower()
    assert "what people say to each other: context only" not in rules


def test_a_burst_of_lines_without_a_mention_is_one_turn(make_bot):
    bot = make_bot(settings=ALL)
    bot.debounce_s = 2.5
    now = [0.0]
    bot.clock = lambda: now[0]
    second = message("y 100 g de merluza fuera", user=PABLO)
    fired = []

    def sleep(seconds):
        if now[0] >= 1.0 and not fired:
            fired.append(1)
            bot.handle({"message": second})
        now[0] += seconds

    bot.sleep = sleep
    bot.handle({"message": message("quedan 10 raciones de lasaña")})
    bot.join_idle()
    [call] = plugin(bot).CALLS
    assert contents(call) == [
        "[no mention] Lucía: quedan 10 raciones de lasaña",
        "[no mention] Pablo: y 100 g de merluza fuera",
    ]


def test_a_line_without_a_mention_joins_the_running_turn(make_bot):
    bot = make_bot(settings=ALL)
    late = message("y otra cosa")

    def handover(turn):
        bot.handle({"message": late})
        got = turn["incoming"]()
        assert [g["content"] for g in got] == ["[no mention] Lucía: y otra cosa"]
        return {"text": "Las dos.", "conversation": "c", "kept": True, "covers": [got[0]["id"]]}

    plugin(bot).REPLIES[:] = [handover]
    say(bot, "una cosa", mention=True)
    assert len(plugin(bot).CALLS) == 1
    assert bot.tg.texts() == ["Las dos."]
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_a_bot_a_channel_or_a_stranger_never_starts_a_turn(make_bot):
    bot = make_bot(settings=ALL)
    as_bot = message("hola", user=LUCIA)
    as_bot["from"]["is_bot"] = True  # a bot that somehow holds a person's id
    bot.handle({"message": as_bot})
    as_chat = message("hola", user=LUCIA)
    as_chat["sender_chat"] = {"id": GROUP, "type": "supergroup"}
    bot.handle({"message": as_chat})
    other_bot = message("menú del día", user=4444)
    other_bot["from"]["is_bot"] = True
    bot.handle({"message": other_bot})
    say(bot, "hola", user=STRANGER)
    assert plugin(bot).CALLS == []
    assert sent(bot) == []
    say(bot, "¿qué tomo?", mention=True)  # what they said is context, still not to the bot
    lines = contents(plugin(bot).CALLS[0])
    assert lines[-1] == "Lucía: ¿qué tomo?"
    assert all(x.startswith("[not to you] ") for x in lines[:-1]) and len(lines) == 5


def test_a_reaction_only_answer_reacts_to_every_line_of_the_burst_and_sends_no_text(make_bot):
    bot = make_bot(settings=ALL)
    first, second = message("quedan 10 raciones de lasaña"), message("y 100 g de merluza fuera")
    plugin(bot).REPLIES[:] = ["```telegram\nreact: 👌\n```"]
    _burst(bot, first, second)
    assert len(plugin(bot).CALLS) == 1
    a, b = first["message_id"], second["message_id"]
    assert _reactions(bot) == [(a, "👀"), (b, "👀"), (a, "👌"), (b, "👌")]  # replaces the 👀
    assert {r["chat_id"] for r in bot.tg.of("setMessageReaction")} == {GROUP}
    assert sent(bot) == []


def test_a_reaction_only_answer_reacts_to_a_line_that_joined_the_running_turn(make_bot):
    bot = make_bot(settings=ALL)
    late = message("y otra cosa")

    def handover(turn):
        bot.handle({"message": late})
        got = turn["incoming"]()
        return {"text": "```telegram\nreact: 👍\n```", "kept": True, "covers": [got[0]["id"]]}

    plugin(bot).REPLIES[:] = [handover]
    msg = say(bot, "una cosa")
    a, b = msg["message_id"], late["message_id"]
    assert _reactions(bot) == [(a, "👀"), (b, "👀"), (a, "👍"), (b, "👍")]
    assert sent(bot) == []


def test_a_deliberate_no_reply_marks_every_line_read_and_the_turn_counts(make_bot, caplog):
    bot = make_bot(settings=ALL)
    first, second = message("¿vienes a cenar?", user=PABLO), message("sí, a las 9")
    plugin(bot).REPLIES[:] = [{"text": "[[no reply]]", "conversation": "c-1", "kept": True}]
    caplog.set_level("INFO")
    _burst(bot, first, second)
    assert len(plugin(bot).CALLS) == 1
    assert _reactions(bot) == [(first["message_id"], "👀"), (second["message_id"], "👀")]
    for method in ("sendMessage", "sendSticker", "sendPhoto", "sendDocument"):
        assert bot.tg.of(method) == [], method
    assert bot.kv.get_json(f"pending:{GROUP}") is None  # handled: not sent again
    assert bot.kv.get(f"conv:{GROUP}") == "c-1"  # the conversation keeps the turn
    assert "answer: nothing to say" in caplog.text
    assert "ai turn failed" not in caplog.text and "not kept" not in caplog.text
    say(bot, "¿qué tomo?", mention=True)
    assert contents(plugin(bot).CALLS[-1]) == ["Lucía: ¿qué tomo?"]  # the old lines are not re-sent
    assert plugin(bot).CALLS[-1]["turn"]["conversation"] == "c-1"


def test_an_empty_answer_marks_the_line_read(make_bot):
    # the plugin's own silence («(sin respuesta)» comes back as an empty text): the same 👀
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = [{"text": "", "conversation": "c-1", "kept": True}]
    msg = say(bot, "¿compras pan?", user=PABLO)
    assert _reactions(bot) == [(msg["message_id"], "👀")]
    assert sent(bot) == []


def test_a_block_that_changes_nothing_marks_the_line_read(make_bot):
    # a record block saying what the data already says posts no «Anotado»: still a 👀
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = ['```record\n{"person": "lucia"}\n```']
    msg = say(bot, "sigo con lo mismo")
    assert sent(bot) == []
    assert _reactions(bot) == [(msg["message_id"], "👀")]


def test_a_text_answer_leaves_the_start_mark_and_adds_no_reaction(make_bot):
    bot = make_bot(settings=ALL)
    first, second = message("quedan 10 raciones"), message("¿cuántas para mañana?")
    plugin(bot).REPLIES[:] = ["Para mañana, 4."]
    _burst(bot, first, second)
    assert bot.tg.texts() == ["Para mañana, 4."]
    assert _reactions(bot) == [(first["message_id"], "👀"), (second["message_id"], "👀")]


def test_a_refused_read_mark_falls_back_to_a_thumb_once_and_never_to_text(make_bot, caplog):
    bot = make_bot(settings=ALL)
    caplog.set_level("INFO")
    first, second = message("uno"), message("dos")
    a, b = first["message_id"], second["message_id"]
    real = bot.tg.call

    def call(method, **params):  # this chat refuses 👀 on a, every time
        refused = method == "setMessageReaction" and params["message_id"] == a
        if refused and params["reaction"][0]["emoji"] == "👀":
            bot.tg.fail_next(method, 400, "Bad Request: REACTION_INVALID")
        return real(method, **params)

    bot.tg.call = call
    plugin(bot).REPLIES[:] = ["[[no reply]]"]
    _burst(bot, first, second)
    # b keeps its 👀; a, refused on arrival, is tried again when the turn takes it and at the
    # end, then gets 👍 once
    assert _reactions(bot) == [(a, "👀"), (b, "👀"), (a, "👀"), (a, "👀"), (a, "👍")]
    assert sent(bot) == []
    assert caplog.text.count("reaction failed") == 3


def test_a_refused_reaction_falls_back_to_a_thumb_and_never_to_text(make_bot):
    bot = make_bot(settings=ALL)

    def thanks(turn):
        bot.eyes.join_idle()
        bot.tg.fail_next("setMessageReaction", 400, "Bad Request: REACTION_INVALID")
        return "```telegram\nreact: 🙏\n```"

    plugin(bot).REPLIES[:] = [thanks]
    msg = say(bot, "gracias")
    m = msg["message_id"]
    assert _reactions(bot) == [(m, "👀"), (m, "🙏"), (m, "👍")]
    assert sent(bot) == []


def test_a_mention_answered_with_nothing_gets_words_never_only_a_read_mark(make_bot):
    bot = make_bot(settings=ALL)
    loose, asked = message("quedan 10 raciones"), message("¿qué tomo?", mention=True)
    plugin(bot).REPLIES[:] = ["[[no reply]]"]
    _burst(bot, loose, asked)
    [words] = sent(bot)
    assert words["text"] == bot.t("no_answer")
    assert words["reply_parameters"]["message_id"] == asked["message_id"]
    # both keep the 👀 of the start; the mention gets words on top, never only the 👀
    assert _reactions(bot) == [(loose["message_id"], "👀"), (asked["message_id"], "👀")]


def test_a_reply_to_the_bot_answered_with_nothing_gets_words(make_bot):
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = [{"text": "", "kept": True}]
    msg = say(bot, "sí", reply=True)
    assert [m["text"] for m in sent(bot)] == [bot.t("no_answer")]
    assert _reactions(bot) == [(msg["message_id"], "👀")]
    assert msg["message_id"] == sent(bot)[0]["reply_parameters"]["message_id"]


def test_a_reminders_only_answer_shows_its_offer_and_no_read_mark(make_bot):
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = ["```reminders\n- 2027-03-11 08:00 | Toma el omega | pablo\n```"]
    msg = say(bot, "a Pablo le toca el omega mañana a las 8", mention=True)
    [offer] = sent(bot)
    assert "¿quieres que te lo recuerde?" in offer["text"]
    assert _reactions(bot) == [(msg["message_id"], "👀")]  # the start's, nothing more


def test_an_edit_that_adds_a_mention_to_a_queued_line_asks_for_words(make_bot):
    bot = make_bot(settings=ALL)
    bot.worker.stop()
    msg = message("quedan 8 raciones")
    bot.handle({"message": msg})
    edited = message("quedan 8 raciones, ¿cuántas para mañana?", mention=True)
    edited["message_id"] = msg["message_id"]
    bot.handle({"edited_message": edited})
    [line] = bot.kv.get_json(f"pending:{GROUP}")
    assert line.get("words") is True
    loose = message("quedan 8 raciones")
    loose["message_id"] = msg["message_id"]
    bot.handle({"edited_message": loose})  # and the mention taken out again
    [line] = bot.kv.get_json(f"pending:{GROUP}")
    assert line["content"].startswith("[no mention] ") and line.get("words") is False


def test_a_voice_note_answered_with_nothing_gets_words(make_bot):
    bot = make_bot(settings=ALL)
    plugin(bot).VOICES[:] = ["me duele la cabeza"]
    plugin(bot).REPLIES[:] = ["[[no reply]]"]
    bot.tg.files["v1"] = b"OggS"
    msg = message("")
    del msg["text"]
    msg["voice"] = {"file_id": "v1"}
    bot.handle({"message": msg})
    bot.join_idle()
    assert bot.tg.texts()[-1] == bot.t("no_answer")
    assert _reactions(bot) == [(msg["message_id"], "👀")]  # on the note, not on the echo


def test_a_failed_turn_keeps_its_marks_and_its_lines_go_with_the_next_turn(make_bot):
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = [
        {"text": "[[no reply]]", "kept": False},  # the provider lost the turn
        RuntimeError("timeout"),
        "[[no reply]]",
    ]
    first = say(bot, "uno")
    assert _reactions(bot) == [(first["message_id"], "👀")]
    second = say(bot, "dos")
    assert _reactions(bot) == [(first["message_id"], "👀"), (second["message_id"], "👀")]
    assert [x["body"] for x in bot.kv.get_json(f"pending:{GROUP}")] == ["uno", "dos"]
    third = say(bot, "tres")
    assert contents(plugin(bot).CALLS[-1]) == [
        "[no mention] Lucía: uno",
        "[no mention] Lucía: dos",
        "[no mention] Lucía: tres",
    ]
    assert _reactions(bot) == [(m["message_id"], "👀") for m in (first, second, third)]
    assert bot.kv.get_json(f"pending:{GROUP}") is None


def test_asked_mode_marks_a_line_only_when_its_turn_starts(make_bot):
    bot = make_bot()  # answer_scope: asked
    plugin(bot).REPLIES[:] = ["[[no reply]]", {"text": "", "kept": True}]
    first = say(bot, "¿qué tomo?", mention=True)
    second = say(bot, "¿y ahora?", mention=True)
    assert _reactions(bot) == [(first["message_id"], "👀"), (second["message_id"], "👀")]
    assert sent(bot) == []


def test_the_no_reply_marker_never_reaches_the_chat(make_bot):
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = [
        "Anotado. [[No Reply]]",
        "[[ no_reply ]]\n```telegram\nreact: 👌\n```",
    ]
    say(bot, "mañana no ceno")
    say(bot, "vale")
    assert bot.tg.texts() == ["Anotado."]
    assert [r["reaction"][0]["emoji"] for r in bot.tg.of("setMessageReaction")] == [
        "👀",
        "👀",
        "👌",
    ]


def test_strip_no_reply():
    assert assistant.strip_no_reply("[[no reply]]") == ("", True)
    assert assistant.strip_no_reply("  [[NO-REPLY]]\n") == ("", True)
    assert assistant.strip_no_reply("Hola.\n\n[[no reply]]") == ("Hola.", True)
    assert assistant.strip_no_reply(" Hola ") == (" Hola ", False)  # untouched without it


def test_an_edit_of_a_queued_line_without_a_mention_keeps_its_mark(make_bot):
    bot = make_bot(settings=ALL)
    bot.worker.stop()  # the line stays queued
    msg = message("quedan 8 raciones")
    bot.handle({"message": msg})
    bot.handle({"edited_message": {**msg, "text": "quedan 10 raciones"}})
    [line] = bot.kv.get_json(f"pending:{GROUP}")
    assert line["content"] == "[no mention] Lucía: quedan 10 raciones"


def test_an_edit_of_an_answered_line_without_a_mention_asks_to_check_the_answer(make_bot):
    bot = make_bot(settings=ALL)
    msg = say(bot, "quedan 8 raciones")
    bot.handle({"edited_message": {**msg, "text": "quedan 10 raciones"}})
    bot.join_idle()
    last = contents(plugin(bot).CALLS[-1])[-1]
    assert last.startswith("[Lucía edited a message you already answered.")
    assert "Before: quedan 8 raciones" in last and "Now: quedan 10 raciones" in last


def test_asked_mode_keeps_unmentioned_lines_as_context_only(make_bot):
    bot = make_bot()  # the fixture's settings: no answer_scope, so "asked"
    assert bot.settings.answer_scope == "asked"
    say(bot, "quedan 10 raciones de lasaña")
    assert plugin(bot).CALLS == []
    msg = say(bot, "quedan 8")
    bot.handle({"edited_message": {**msg, "text": "quedan 9"}})
    bot.join_idle()
    assert plugin(bot).CALLS == []  # an edit of one is still not a message
    say(bot, "¿qué hay?", mention=True)
    [call] = plugin(bot).CALLS
    assert contents(call) == [
        "[not to you] Lucía: quedan 10 raciones de lasaña",
        "[not to you] Lucía: quedan 9",
        "Lucía: ¿qué hay?",
    ]
    rules = " ".join(call["turn"]["rules"].split())
    assert "[no mention]" not in rules and "[[no reply]]" not in rules
    assert '"[not to you]" are what people say to each other: context only' in rules


def test_more_than_ten_lines_without_a_mention_all_wait_for_the_next_turn(make_bot):
    bot = make_bot(settings=ALL)
    bot.worker.stop()  # a long turn or a rest for capacity: the lines pile up
    for i in range(12):
        bot.handle({"message": message(f"linea {i}")})
    pending = bot.kv.get_json(f"pending:{GROUP}")
    assert [x["body"] for x in pending] == [f"linea {i}" for i in range(12)]


def test_the_rules_say_principle_1_covers_every_message_of_a_person(make_bot):
    bot = make_bot(settings=ALL)
    say(bot, "hola")
    rules = " ".join(_rules(bot).split())
    assert "what is addressed to you (principle 1) is every message from a person" in rules


def test_an_edit_of_a_line_answered_with_a_reaction_asks_to_check_that_answer(make_bot):
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = ["```telegram\nreact: 👍\n```", "Corregido."]
    msg = say(bot, "he tomado 1 ibuprofeno")
    bot.handle({"edited_message": {**msg, "text": "he tomado 1 ibuprofeno 600"}})
    bot.join_idle()
    [last] = contents(plugin(bot).CALLS[-1])
    assert last.startswith("[Lucía edited a message you already answered.")
    assert "Your answer was: (reaction 👍)" in last


def test_an_edit_of_a_line_answered_with_silence_asks_to_check_that_answer(make_bot):
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = ["[[no reply]]", "Vale."]
    msg = say(bot, "¿vienes?", user=PABLO)
    bot.handle({"edited_message": {**msg, "text": "¿vienes a cenar?"}})
    bot.join_idle()
    [last] = contents(plugin(bot).CALLS[-1])
    assert last.startswith("[Pablo edited a message you already answered.")
    assert "Your answer was: (no reply)" in last


def test_an_edit_that_adds_a_mention_to_a_queued_line_drops_its_mark(make_bot):
    bot = make_bot(settings=ALL)
    bot.worker.stop()
    msg = message("quedan 8 raciones")
    bot.handle({"message": msg})
    edited = message("quedan 8 raciones, ¿cuántas para mañana?", mention=True)
    edited["message_id"] = msg["message_id"]
    bot.handle({"edited_message": edited})
    [line] = bot.kv.get_json(f"pending:{GROUP}")
    assert line["content"] == "Lucía: quedan 8 raciones, ¿cuántas para mañana?"


def test_an_edit_that_only_adds_a_mention_drops_the_mark_too(make_bot):
    bot = make_bot(settings=ALL)
    bot.worker.stop()
    msg = message("quedan 8 raciones")
    bot.handle({"message": msg})
    edited = message("quedan 8 raciones", mention=True)
    edited["message_id"] = msg["message_id"]
    bot.handle({"edited_message": edited})
    [line] = bot.kv.get_json(f"pending:{GROUP}")
    assert line["content"] == "Lucía: quedan 8 raciones"


def test_a_marker_wrapped_in_formatting_sends_no_text(make_bot):
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = ["`[[no reply]]`", "**[[no reply]]**", "[[no reply]]."]
    msgs = [say(bot, word, user=PABLO) for word in ("uno", "dos", "tres")]
    assert len(plugin(bot).CALLS) == 3
    assert sent(bot) == []
    assert _reactions(bot) == [(m["message_id"], "👀") for m in msgs]


def test_lines_without_a_mention_show_no_typing(make_bot):
    bot = make_bot(settings=ALL)
    say(bot, "¿vienes a cenar?", user=PABLO)
    assert bot.tg.of("sendChatAction") == []
    say(bot, "¿qué tomo?", mention=True)
    assert len(bot.tg.of("sendChatAction")) == 1


def _photo(caption="", mention=False, reply=False):
    msg = message(caption, mention=mention, reply=reply)
    msg["caption"] = msg.pop("text")
    if "entities" in msg:
        msg["caption_entities"] = msg.pop("entities")
    msg["photo"] = [{"file_id": "ph", "file_size": 7, "width": 1000}]
    return msg


def test_a_photo_that_mentions_the_bot_answered_with_nothing_gets_words(make_bot):
    bot = make_bot(settings=ALL)
    bot.tg.files["ph"] = b"\xff\xd8 jpeg"
    plugin(bot).REPLIES[:] = ["[[no reply]]"]
    msg = _photo("¿qué es esto?", mention=True)
    bot.handle({"message": msg})
    bot.join_idle()
    assert bot.tg.texts() == [bot.t("no_answer")]
    assert _reactions(bot) == [(msg["message_id"], "👀")]


def test_a_photo_that_replies_to_the_bot_answered_with_nothing_gets_words(make_bot):
    bot = make_bot(settings=ALL)
    bot.tg.files["ph"] = b"\xff\xd8 jpeg"
    plugin(bot).REPLIES[:] = [{"text": "", "kept": True}]
    msg = _photo("esta", reply=True)
    bot.handle({"message": msg})
    bot.join_idle()
    assert bot.tg.texts() == [bot.t("no_answer")]
    assert _reactions(bot) == [(msg["message_id"], "👀")]


def test_a_loose_photo_answered_with_nothing_is_marked_read(make_bot):
    bot = make_bot(settings=ALL)
    bot.tg.files["ph"] = b"\xff\xd8 jpeg"
    plugin(bot).REPLIES[:] = ["[[no reply]]"]
    msg = _photo("la cena")
    bot.handle({"message": msg})
    bot.join_idle()
    assert sent(bot) == []
    assert _reactions(bot) == [(msg["message_id"], "👀")]


def test_a_mention_answered_with_a_reaction_only_gets_that_reaction(make_bot):
    # a reaction is something to show: the app forces words only when nothing showed
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = ["```telegram\nreact: 👍\n```"]
    msg = say(bot, "gracias", mention=True)
    assert sent(bot) == []
    assert _reactions(bot) == [(msg["message_id"], "👀"), (msg["message_id"], "👍")]


def test_a_block_that_changes_nothing_with_a_sticker_that_cannot_go_marks_the_line_read(make_bot):
    # no sticker set: the sticker never reaches the chat, and the block posted nothing either
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = ['```record\n{"person": "lucia"}\n```\n```telegram\nsticker: 😀\n```']
    msg = say(bot, "sigo con lo mismo")
    assert sent(bot) == [] and bot.tg.of("sendSticker") == []
    assert _reactions(bot) == [(msg["message_id"], "👀")]


def test_a_mention_whose_words_cannot_go_is_marked_read(make_bot):
    bot = make_bot(settings=ALL)
    bot.tg.fail_next("sendMessage", 400, "Bad Request: message thread not found")
    bot.tg.fail_next("sendMessage", 400, "Bad Request: message thread not found")
    plugin(bot).REPLIES[:] = ["[[no reply]]"]
    msg = say(bot, "¿qué tomo?", mention=True)
    assert _reactions(bot) == [(msg["message_id"], "👀")]
