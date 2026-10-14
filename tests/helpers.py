"""Small helpers the test files share: messages, taps, and the scripted plugin."""

import itertools

from conftest import BOT_ID, GROUP, LUCIA

from supermedicaldoctor import assistant

_ids = itertools.count(5000)
QUESTION = "¿Algo más?"


def plugin(bot):
    return assistant.load_module(bot.data, "plugins/fake.py")


def message(
    text,
    user=LUCIA,
    chat=GROUP,
    private=False,
    reply=False,
    mention=False,
    thread=None,
    mid=None,
):
    """A Telegram message dict. `reply`: a reply to the bot; `mention`: «@example_doctor_bot»
    in front."""
    if mention:
        text = "@example_doctor_bot " + text
    msg = {
        "message_id": mid if mid is not None else next(_ids),
        "from": {"id": user, "first_name": "Someone"},
        "chat": {"id": user if private else chat, "type": "private" if private else "supergroup"},
        "text": text,
    }
    if mention:
        msg["entities"] = [{"type": "mention", "offset": 0, "length": 19}]
    if reply:
        msg["reply_to_message"] = {"message_id": 1, "from": {"id": BOT_ID}, "text": QUESTION}
    if thread is not None:
        msg["message_thread_id"] = thread
        msg["is_topic_message"] = True
    return msg


def say(bot, text, **kw):
    msg = message(text, **kw)
    bot.handle({"update_id": 1, "message": msg})
    bot.join_idle()
    return msg


def tap(bot, data, user=LUCIA, message_id=1, chat=GROUP):
    bot.handle(
        {
            "update_id": 2,
            "callback_query": {
                "id": f"cb-{data}-{user}",
                "from": {"id": user},
                "message": {"message_id": message_id, "chat": {"id": chat, "type": "supergroup"}},
                "data": data,
            },
        }
    )
    bot.join_idle()


def sent(bot):
    return bot.tg.of("sendMessage")


def last_sent(bot):
    return sent(bot)[-1]


def toasts(bot):
    return [p.get("text") for p in bot.tg.of("answerCallbackQuery")]


def buttons(params):
    rows = (params.get("reply_markup") or {}).get("inline_keyboard") or []
    return [(b["text"], b["callback_data"]) for row in rows for b in row]


def record(text, block):
    """An answer that ends with a record block."""
    import json

    return f"{text}\n\n```record\n{json.dumps(block, ensure_ascii=False)}\n```"


def contents(call):
    return [x["content"] for x in call["turn"]["new"]]
