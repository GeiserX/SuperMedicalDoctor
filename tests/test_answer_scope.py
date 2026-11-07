"""`answer_scope: all`: in the group, a person's text without a mention or a reply starts a turn
as an addressed one does, marked «[no mention]», and the assistant may answer it with a reaction
or with nothing at all («[[no reply]]»)."""

from conftest import GROUP, LUCIA, PABLO, SETTINGS, STRANGER
from helpers import contents, message, plugin, say, sent

from supermedicaldoctor import assistant

# answer_scope: all without history_scope: all: the setting implies the context it needs
ALL = SETTINGS.replace("history_scope: all", "history_scope: asked\n  answer_scope: all")


def _rules(bot):
    return plugin(bot).CALLS[-1]["turn"]["rules"]


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


def test_a_reaction_only_answer_reacts_to_the_last_line_and_sends_no_text(make_bot):
    bot = make_bot(settings=ALL)
    bot.debounce_s = 2.5
    now = [0.0]
    bot.clock = lambda: now[0]
    second = message("y 100 g de merluza fuera")
    fired = []

    def sleep(seconds):
        if now[0] >= 1.0 and not fired:
            fired.append(1)
            bot.handle({"message": second})
        now[0] += seconds

    bot.sleep = sleep
    plugin(bot).REPLIES[:] = ["```telegram\nreact: 👍\n```"]
    bot.handle({"message": message("quedan 10 raciones de lasaña")})
    bot.join_idle()
    [r] = bot.tg.of("setMessageReaction")
    assert (r["chat_id"], r["message_id"]) == (GROUP, second["message_id"])
    assert sent(bot) == []


def test_a_deliberate_no_reply_sends_nothing_and_the_turn_counts(make_bot, caplog):
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = [{"text": "[[no reply]]", "conversation": "c-1", "kept": True}]
    caplog.set_level("INFO")
    say(bot, "¿vienes a cenar?", user=PABLO)
    assert len(plugin(bot).CALLS) == 1
    for method in ("sendMessage", "setMessageReaction", "sendSticker", "sendPhoto", "sendDocument"):
        assert bot.tg.of(method) == [], method
    assert bot.kv.get_json(f"pending:{GROUP}") is None  # handled: not sent again
    assert bot.kv.get(f"conv:{GROUP}") == "c-1"  # the conversation keeps the turn
    assert "answer: nothing to say" in caplog.text
    assert "ai turn failed" not in caplog.text and "not kept" not in caplog.text
    say(bot, "¿qué tomo?", mention=True)
    assert contents(plugin(bot).CALLS[-1]) == ["Lucía: ¿qué tomo?"]  # the old line is not re-sent
    assert plugin(bot).CALLS[-1]["turn"]["conversation"] == "c-1"


def test_the_no_reply_marker_never_reaches_the_chat(make_bot):
    bot = make_bot(settings=ALL)
    plugin(bot).REPLIES[:] = [
        "Anotado. [[No Reply]]",
        "[[ no_reply ]]\n```telegram\nreact: 👌\n```",
    ]
    say(bot, "mañana no ceno")
    say(bot, "vale")
    assert bot.tg.texts() == ["Anotado."]
    assert [r["reaction"][0]["emoji"] for r in bot.tg.of("setMessageReaction")] == ["👌"]


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
