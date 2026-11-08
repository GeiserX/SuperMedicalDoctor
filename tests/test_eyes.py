"""👀 when a turn starts: in every chat, the bot puts 👀 on each person's message the moment a turn
takes it, and on a message that joins a running turn the moment it arrives, so people see it is
being worked on. Never on a bot's message, a tap or a turn with no person's message, never twice,
and never in the way of the turn."""

import threading

from conftest import GROUP, LUCIA, SETTINGS
from helpers import buttons, last_sent, message, plugin, say, sent, tap

ALL = SETTINGS.replace("history_scope: all", "history_scope: asked\n  answer_scope: all")


def _reactions(bot):
    return [(r["message_id"], r["reaction"][0]["emoji"]) for r in bot.tg.of("setMessageReaction")]


def _voice(file_id="v1", **kw):
    msg = message("", **kw)
    del msg["text"]
    msg["voice"] = {"file_id": file_id}
    return msg


def test_the_mark_lands_when_the_turn_starts_before_the_assistant_answers(make_bot):
    bot = make_bot(settings=ALL)
    seen = []

    def answer(turn):
        # queued before the assistant was called, and in the chat while it still thinks
        seen.append(list(bot.kv.get_json(f"eyes:{GROUP}") or []))
        bot.eyes.join_idle()
        seen.append(_reactions(bot))
        assert sent(bot) == []
        return "Vale, lo apunto."

    plugin(bot).REPLIES[:] = [answer]
    msg = say(bot, "¿qué tomo?", mention=True)
    m = msg["message_id"]
    assert seen == [[m], [(m, "👀")]]
    assert bot.tg.texts() == ["Vale, lo apunto."]
    assert _reactions(bot) == [(m, "👀")]  # a text answer leaves it there


def test_a_line_that_joins_a_running_turn_is_marked_at_once(make_bot):
    bot = make_bot()  # asked mode: a mention joins
    late = message("y otra cosa", mention=True)
    seen = []

    def handover(turn):
        bot.handle({"message": late})
        bot.eyes.join_idle()
        seen.append(_reactions(bot))  # before the assistant takes it, before any answer
        got = turn["incoming"]()
        return {"text": "Las dos.", "kept": True, "covers": [got[0]["id"]]}

    plugin(bot).REPLIES[:] = [handover]
    msg = say(bot, "una cosa", mention=True)
    a, b = msg["message_id"], late["message_id"]
    assert seen == [[(a, "👀"), (b, "👀")]]
    assert _reactions(bot) == [(a, "👀"), (b, "👀")]


def test_a_file_that_waits_for_the_next_turn_is_marked_once_on_arrival(make_bot):
    bot = make_bot()
    bot.tg.files["pdf1"] = b"%PDF"
    doc = message("el análisis")
    doc["caption"] = doc.pop("text")
    doc["document"] = {"file_id": "pdf1", "file_name": "a.pdf", "mime_type": "application/pdf"}
    calls = threading.Event()

    seen = []

    def handover(turn):
        bot.handle({"message": doc})
        bot.eyes.join_idle()
        seen.append(_reactions(bot))  # at once, though only the next turn reads it
        return "Vale."

    def second(turn):
        calls.set()
        return "Leído."

    plugin(bot).REPLIES[:] = [handover, second]
    msg = say(bot, "hola", mention=True)
    assert calls.wait(5)
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 2
    marks = [(msg["message_id"], "👀"), (doc["message_id"], "👀")]
    assert seen == [marks]
    assert _reactions(bot) == marks


def test_a_private_chat_gets_the_mark(make_bot):
    bot = make_bot()
    msg = say(bot, "¿qué tomo hoy?", private=True)
    assert bot.tg.texts() == ["Vale."]
    [r] = bot.tg.of("setMessageReaction")
    assert (r["chat_id"], r["message_id"]) == (LUCIA, msg["message_id"])
    assert r["reaction"] == [{"type": "emoji", "emoji": "👀"}]


def test_asked_mode_marks_a_mention_and_a_reply_and_not_plain_talk(make_bot):
    bot = make_bot()
    plain = say(bot, "hola a todos")
    asked = say(bot, "¿qué tomo?", mention=True)
    replied = say(bot, "sí", reply=True)
    assert _reactions(bot) == [(asked["message_id"], "👀"), (replied["message_id"], "👀")]
    assert plain["message_id"] not in {m for m, _ in _reactions(bot)}


def test_a_voice_note_is_marked_on_the_persons_note_not_on_the_echo(make_bot):
    bot = make_bot()
    plugin(bot).VOICES[:] = ["me duele la cabeza"]
    bot.tg.files["v1"] = b"OggS"
    msg = _voice()
    bot.handle({"message": msg})
    bot.join_idle()
    echo, _answer = sent(bot)
    assert echo["text"].startswith("🎤")
    assert _reactions(bot) == [(msg["message_id"], "👀")]


def test_an_edit_that_starts_a_turn_is_marked(make_bot):
    bot = make_bot()
    msg = message("hola a todos")
    bot.handle({"message": msg})
    bot.join_idle()
    assert bot.tg.of("setMessageReaction") == []
    edited = message("¿qué tomo?", mention=True, mid=msg["message_id"])
    bot.handle({"edited_message": edited})
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 1
    assert _reactions(bot) == [(msg["message_id"], "👀")]


def test_no_mark_on_a_bot_a_channel_or_a_stranger(make_bot):
    bot = make_bot(settings=ALL)
    as_bot = message("hola")
    as_bot["from"]["is_bot"] = True
    bot.handle({"message": as_bot})
    as_chat = message("hola")
    as_chat["sender_chat"] = {"id": GROUP, "type": "supergroup"}
    bot.handle({"message": as_chat})
    say(bot, "hola", user=9999)
    bot.join_idle()
    assert bot.tg.of("setMessageReaction") == []


def test_a_tap_starts_a_turn_without_a_mark_on_the_bots_message(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["¿Cuál?\n```options\n- Mañana\n- Tarde\n```", "Hecho."]
    asked = say(bot, "¿cuándo?", mention=True)
    offer = last_sent(bot)
    tap(bot, buttons(offer)[0][1], message_id=offer.get("message_id", 1))
    assert len(plugin(bot).CALLS) == 2
    assert _reactions(bot) == [(asked["message_id"], "👀")]  # the tap's turn adds none


def test_a_turn_with_no_persons_message_puts_no_mark(make_bot):
    bot = make_bot()
    bot.queue_line(GROUP, {"id": "auto-1", "ts": int(bot.now().timestamp()), "content": "x"})
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 1
    assert bot.tg.of("setMessageReaction") == []


def test_a_message_is_never_marked_twice(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [{"text": "x", "kept": False}, "Vale.", "Sigue valiendo."]
    msg = say(bot, "¿qué tomo?", mention=True)  # a failed turn: the line goes again
    say(bot, "¿y luego?", mention=True)
    # an edit of the answered message starts a turn of its own: it already has its 👀
    edited = message("¿qué tomo ahora?", mention=True, mid=msg["message_id"])
    bot.handle({"edited_message": edited})
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 3
    marks = [m for m, _ in _reactions(bot)]
    assert len(marks) == len(set(marks)) == 2


def test_an_edit_during_the_turn_adds_no_second_mark(make_bot):
    bot = make_bot()
    msg = message("¿qué tomo?", mention=True)

    def edit(turn):
        bot.handle(
            {"edited_message": message("¿qué tomo hoy?", mention=True, mid=msg["message_id"])}
        )
        return "Vale."

    plugin(bot).REPLIES[:] = [edit, "Corregido."]
    bot.handle({"message": msg})
    bot.join_idle()
    assert len(plugin(bot).CALLS) == 2
    assert _reactions(bot) == [(msg["message_id"], "👀")]


def test_a_refused_mark_never_holds_the_turn_or_posts_text(make_bot, caplog):
    bot = make_bot()
    caplog.set_level("INFO")
    bot.tg.fail_next("setMessageReaction", 400, "Bad Request: REACTION_INVALID")
    plugin(bot).REPLIES[:] = ["[[no reply]]"]
    msg = say(bot, "¿qué tomo?", mention=True)
    assert len(plugin(bot).CALLS) == 1
    assert sent(bot) == []  # silence stays silence: no text instead
    assert _reactions(bot) == [(msg["message_id"], "👀")]  # no 👍 at the start
    assert "start reaction failed" in caplog.text


def test_a_stuck_mark_never_holds_the_answer(make_bot):
    bot = make_bot()
    gate = threading.Event()
    real = bot.tg.call

    def call(method, **params):
        if method == "setMessageReaction":
            gate.wait(10)
        return real(method, **params)

    bot.tg.call = call
    answered = threading.Event()

    def answer(turn):
        return "Vale."

    plugin(bot).REPLIES[:] = [answer]
    bot.handle({"message": message("¿qué tomo?", mention=True)})
    for _ in range(200):
        if bot.tg.texts():
            answered.set()
            break
        threading.Event().wait(0.05)
    assert answered.is_set()  # the answer went out while the 👀 was still stuck
    assert bot.tg.of("setMessageReaction") == []
    gate.set()
    bot.join_idle()
    assert len(bot.tg.of("setMessageReaction")) == 1


def test_a_reaction_only_answer_replaces_the_mark_and_a_later_turn_marks_it_again(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["```telegram\nreact: 👍\n```", "Ya está."]
    msg = say(bot, "gracias", mention=True)
    m = msg["message_id"]
    assert _reactions(bot) == [(m, "👀"), (m, "👍")]
    edited = message("gracias, ¿y mañana?", mention=True, mid=m)
    bot.handle({"edited_message": edited})  # its turn takes it again: 👀 again
    bot.join_idle()
    assert _reactions(bot) == [(m, "👀"), (m, "👍"), (m, "👀")]


def test_a_failed_turn_leaves_the_mark(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [RuntimeError("timeout")]
    msg = say(bot, "¿qué tomo?", mention=True)
    assert bot.tg.texts() == [bot.t("ai_down")]
    assert _reactions(bot) == [(msg["message_id"], "👀")]
