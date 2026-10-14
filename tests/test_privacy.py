"""The service log holds event kinds and counts, never words, values, names or file names. A
scripted flow runs with the log at DEBUG; the same flow with one careless log line must go red."""

import logging
import time

import pytest
from conftest import LUCIA, PABLO
from helpers import message, plugin, record, say

from supermedicaldoctor import bot as bot_module
from supermedicaldoctor.bot import Bot

SECRETS = (
    "Ecografía",
    "Hospital Ejemplo",
    "ferritina",
    "Levotiroxina",
    "planted-word",
    "lucia",
    "Lucía",
    "Pablo",
    "Analitica-2027",
    "dolor de rodilla",
    "2027-03-16",
    "cal-lucia",
)


def scripted_flow(bot):
    mod = plugin(bot)
    good = {
        "person": "lucia",
        "appointments": [
            {
                "op": "add",
                "date": "2027-03-16",
                "time": "9:00",
                "what": "Ecografía",
                "place": "Hospital Ejemplo",
            }
        ],
    }
    bad = {"person": "lucia", "meds": [{"op": "add", "name": "planted-word", "kind": "pill"}]}
    mod.REPLIES[:] = [
        record("Apuntado.", good),
        record("Vale.", bad),
        {
            "text": "Lo miro.",
            "kept": True,
            "later": lambda: (_ for _ in ()).throw(RuntimeError("Lucía planted-word ferritina")),
        },
        "Vale.",
    ]
    mod.VOICES[:] = ["dolor de rodilla desde ayer"]
    say(bot, "ecografía el martes a las 9 en Hospital Ejemplo", mention=True)
    say(bot, "y empiezo planted-word", mention=True)
    say(bot, "¿y la ferritina?", mention=True)
    bot.tg.files["v1"] = b"OggS"
    note = message("", user=PABLO)
    del note["text"]
    note["voice"] = {"file_id": "v1"}
    bot.handle({"message": note})
    bot.join_idle()
    bot.tg.files["pdf"] = b"%PDF"
    doc = message("", user=LUCIA)
    del doc["text"]
    doc["document"] = {
        "file_id": "pdf",
        "file_name": "Analitica-2027.pdf",
        "mime_type": "application/pdf",
    }
    doc["caption"] = "mis análisis de ferritina"
    bot.handle({"message": doc})
    bot.join_idle()
    say(bot, "/deshacer")
    say(bot, "/citas")
    deadline = time.monotonic() + 5  # the failing `later` runs on its own thread
    while not any(t.startswith("No he podido terminar") for t in bot.tg.texts()):
        assert time.monotonic() < deadline
        time.sleep(0.02)


@pytest.mark.parametrize("planted", [False, True])
def test_the_service_log_holds_no_words_values_or_names(make_bot, caplog, monkeypatch, planted):
    if planted:  # the positive control: one careless line must turn the test red
        original = Bot.queue_text

        def leaky(self, chat, msg, content, p, *rest):
            bot_module.log.info("queued %s", content)
            return original(self, chat, msg, content, p, *rest)

        monkeypatch.setattr(Bot, "queue_text", leaky)
    caplog.set_level(logging.DEBUG)
    bot = make_bot()
    scripted_flow(bot)

    texts = bot.tg.texts()
    assert any(t.startswith("Anotado para Lucía:") for t in texts)  # the flow really ran
    assert any(t.startswith("No he sabido anotarlo bien") for t in texts)
    assert any(t.startswith("🎤") for t in texts)
    assert any(t.startswith("Deshecho:") for t in texts)
    assert "block refused: bad_kind" in caplog.text
    assert "git push ok" in caplog.text
    assert "git revert ok" in caplog.text
    assert "voice heard" in caplog.text
    assert "file queued" in caplog.text
    assert "later failed: RuntimeError" in caplog.text

    found = [s for s in SECRETS if s.lower() in caplog.text.lower()]
    if planted:
        assert found, "the planted log line was not caught"
    else:
        assert found == [], found
