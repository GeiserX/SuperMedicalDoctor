"""How an answer looks and acts in Telegram: the renderer, the gestures block (including hostile
model output) and the bot sending them after the text."""

import re

from conftest import GROUP
from helpers import last_sent, plugin, say, sent

from supermedicaldoctor import gestures, records, render

TAGS = r"b|i|u|s|tg-spoiler|code|pre|blockquote|a"


def test_render_folds_quotes_and_code():
    out = render.to_html(
        "**Resumen**\n> cita\n```fold\n# Detalle\n- a ||b||\n```\n```python\nx < 1\n```"
    )
    assert out == (
        "<b>Resumen</b>\n<blockquote>cita</blockquote>\n<blockquote expandable><b>Detalle</b>\n"
        '• a <tg-spoiler>b</tg-spoiler></blockquote>\n<pre><code class="language-python">'
        "x &lt; 1</code></pre>"
    )


def test_render_hostile_markup_never_opens_a_tag():
    for md in (
        "<blockquote expandable>x",
        "||<b>x</b>||",
        "```fold\n</blockquote><b>\n```",
        '```py" onclick="x\ny\n```',
        '[x](https://a.org/"><script>)',
    ):
        out = render.to_html(md)
        assert sorted(re.findall(rf"<({TAGS})[ >]", out)) == sorted(
            re.findall(rf"</({TAGS})>", out)
        ), (md, out)
        assert "<script" not in out and "onclick" not in out


def test_a_cut_fold_reopens_as_a_fold():
    chunks = render.split("```fold\n" + "detalle\n" * 900 + "```")
    assert len(chunks) > 1
    assert all(render.to_html(c).startswith("<blockquote expandable>") for c in chunks)


def test_gestures_hostile_output_is_dropped():
    text, g = gestures.extract(
        "x\n```telegram\nreact: <b>\nsticker: abc\nphoto: http://a.org/a.jpg\n"
        "photo: https://u:p@evil.org/a.jpg\nsend_message: -100 hola\n```\n"
        "```document run.sh\necho\n```\n"
    )
    assert text == "x"
    assert g == {"react": None, "sticker": None, "photos": [], "documents": []}
    assert gestures.extract("```telegram\nreact: 🥦\n```")[1]["react"] is None


def test_answer_goes_as_html_and_a_refused_markup_as_plain_text(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["**Toma** el hierro con zumo."]
    say(bot, "hierro", mention=True)
    assert last_sent(bot)["text"] == "<b>Toma</b> el hierro con zumo."
    assert last_sent(bot)["parse_mode"] == "HTML"
    bot.tg.fail_next("sendMessage", 400, "Bad Request: can't parse entities")
    plugin(bot).REPLIES[:] = ["**Otra** vez."]
    say(bot, "¿y?", mention=True)
    assert last_sent(bot)["text"] == "**Otra** vez."
    assert "parse_mode" not in last_sent(bot)


def test_a_reaction_alone_says_nothing(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["```telegram\nreact: 👍\n```"]
    msg = say(bot, "gracias", mention=True)
    [r] = bot.tg.of("setMessageReaction")
    assert (r["chat_id"], r["message_id"]) == (GROUP, msg["message_id"])
    assert r["reaction"] == [{"type": "emoji", "emoji": "👍"}]
    assert not any("👍" in p["text"] or "react" in p["text"] for p in sent(bot))


def test_a_failed_reaction_with_no_text_says_the_emoji(make_bot):
    bot = make_bot()
    bot.tg.fail_next("setMessageReaction", 400, "Bad Request: REACTION_INVALID")
    plugin(bot).REPLIES[:] = ["```telegram\nreact: 🙏\n```"]
    say(bot, "gracias", mention=True)
    assert last_sent(bot)["text"] == "🙏"


def test_a_report_goes_as_a_file_and_a_refused_file_never_loses_the_text(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [
        "Te paso el informe.\n````document informe.md | Octubre\n# Informe\n````"
    ]
    say(bot, "informe", mention=True)
    assert last_sent(bot)["text"] == "Te paso el informe."
    [doc] = bot.tg.of("sendDocument")
    assert (doc["document"], doc["data"]) == ("informe.md", b"# Informe\n")
    assert doc["caption"] == "Octubre"
    bot.tg.fail_next("sendDocument", 400, "Bad Request: file is empty")
    plugin(bot).REPLIES[:] = ["Otra vez.\n```document b.txt\nx\n```"]
    say(bot, "otra", mention=True)
    assert last_sent(bot)["text"] == "Otra vez."


def test_stickers_and_photos_need_the_settings(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["Hecho.\n```telegram\nsticker: 🎉\nphoto: https://a.org/p.jpg\n```"]
    say(bot, "hola", mention=True)
    assert bot.tg.of("sendSticker") == [] and bot.tg.of("sendPhoto") == []


def test_the_formatting_guide_and_the_voice_reach_the_rules(make_bot):
    bot = make_bot()
    say(bot, "hola", mention=True)
    rules = plugin(bot).CALLS[-1]["turn"]["rules"]
    assert "```fold" in rules and "react: 👍" in rules
    assert "sticker:" not in rules


def test_the_voice_and_switches_come_from_settings():
    s = records.Settings.from_dict(
        {"telegram": {"style": "Calmado y claro.", "sticker_set": "x", "photos": "yes"}}
    )
    assert (s.style, s.sticker_set, s.photos) == ("Calmado y claro.", "x", False)
    guide = gestures.guide("en", s.style, bool(s.sticker_set), s.photos)
    assert guide.rstrip().endswith("Calmado y claro.") and "sticker: 🎉" in guide
