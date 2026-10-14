"""The bot end to end against a real data repository: records from answers, commands, undo,
offers, the asks with an owner, the reminders it sends by itself and settings from the chat."""

import json
from datetime import timedelta

import yaml
from conftest import GROUP, LUCIA, PABLO, STRANGER, TODAY, at, git
from helpers import buttons, last_sent, message, plugin, record, say, sent, tap, toasts

ADD = {
    "person": "lucia",
    "appointments": [
        {
            "op": "add",
            "date": "2027-03-16",
            "time": "9:00",
            "what": "Ecografía",
            "place": "Hospital Ejemplo",
            "prep": [{"hours_before": 6, "text": "En ayunas"}],
        }
    ],
}


def remote_yaml(data_repo, path):
    return yaml.safe_load(data_repo.remote_file(path))


# --- records from the chat -----------------------------------------------------------------


def test_a_record_is_committed_with_the_export_and_confirmed_from_what_was_saved(
    make_bot, data_repo
):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [record("Apuntada la ecografía.", ADD)]
    say(bot, "tengo ecografía el martes a las 9", mention=True)
    texts = bot.tg.texts()
    assert texts[0] == "Apuntada la ecografía."
    assert texts[1].startswith(
        "Anotado para Lucía:\n• Cita: martes 16 de marzo a las 9:00, Ecografía"
    )
    assert "(Hospital Ejemplo)" in texts[1] and "• En ayunas (6 h antes)" in texts[1]
    assert data_repo.remote_log()[0] == "log(lucia): appointment add 2027-03-16-0900-ecografia"
    appts = remote_yaml(data_repo, "people/lucia/appointments.yaml")
    assert appts[-1]["chat"] == "group" and appts[-1]["time"] == "09:00"
    chef = remote_yaml(data_repo, "export/chef.yaml")
    assert [f["until"] for f in chef["people"]["lucia"]["fasting"]] == [
        "2027-03-12T09:30:00+01:00",
        "2027-03-16T09:00:00+01:00",
    ]
    changed = git(data_repo.remote, "show", "--name-only", "--format=", "main").split()
    assert sorted(changed) == ["export/chef.yaml", "people/lucia/appointments.yaml"]


def test_the_same_answer_applied_twice_changes_nothing_and_says_nothing(make_bot, data_repo):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [record("Vale.", ADD), record("Vale.", ADD)]
    say(bot, "uno", mention=True)
    say(bot, "otra vez", mention=True)
    assert sum(t.startswith("Anotado") for t in bot.tg.texts()) == 1
    assert (
        data_repo.remote_log().count("log(lucia): appointment add 2027-03-16-0900-ecografia") == 1
    )


def test_a_record_for_someone_outside_the_scope_is_refused(make_bot, data_repo):
    bot = make_bot()
    block = {"person": "lucia", "profile": {"notes": {"add": ["x"]}}}
    plugin(bot).REPLIES[:] = [record("Vale.", block)]
    before = data_repo.remote_log()
    say(bot, "apunta lo de Lucía", user=PABLO, private=True)
    assert bot.tg.texts()[-1].startswith("No he sabido anotarlo bien")
    assert data_repo.remote_log() == before


def test_a_block_that_does_not_parse_saves_nothing(make_bot, data_repo):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["Vale.\n\n```record\n{not json\n```"]
    before = data_repo.remote_log()
    say(bot, "x", mention=True)
    assert bot.tg.texts() == [
        "Vale.",
        "No he sabido anotarlo bien, así que no he guardado nada. ¿Me lo dices de otra forma?",
    ]
    assert data_repo.remote_log() == before


def test_two_people_two_blocks_two_commits(make_bot, data_repo):
    bot = make_bot()
    pablo = {"person": "pablo", "meds": [{"op": "stop", "id": "omega-3"}]}
    plugin(bot).REPLIES[:] = [record(record("Hecho.", ADD), pablo)]
    say(bot, "lo de los dos", mention=True)
    assert data_repo.remote_log()[:2] == [
        "log(pablo): med stop omega-3",
        "log(lucia): appointment add 2027-03-16-0900-ecografia",
    ]
    assert "Deja: Omega 3 1 g, cena (suplemento), desde el 10 de marzo" in bot.tg.texts()[-1]


def test_a_lab_photo_is_filed_with_its_values(make_bot, data_repo):
    bot = make_bot()
    bot.tg.files["ph"] = b"\xff\xd8 jpeg"
    msg = message("", user=LUCIA)
    del msg["text"]
    msg["photo"] = [{"file_id": "ph", "file_size": 7, "width": 1000}]
    name = f"inbox/{msg['message_id']}-foto.jpg"
    block = {
        "person": "lucia",
        "labs": [
            {"date": "2027-03-09", "test": "Ferritina", "value": 41, "unit": "ng/mL", "doc": name}
        ],
        "documents": [{"file": name, "date": "2027-03-09", "title": "Analítica"}],
    }
    plugin(bot).REPLIES[:] = [record("Tu ferritina subió de 18 a 41.", block)]
    bot.handle({"message": msg})
    bot.join_idle()
    path = "people/lucia/documents/2027-03-09-analitica.jpg"
    files = git(data_repo.remote, "show", "--name-only", "--format=", "main").split()
    assert path in files and "people/lucia/labs.jsonl" in files
    labs = data_repo.remote_file("people/lucia/labs.jsonl").splitlines()
    assert json.loads(labs[-1])["doc"] == path
    assert "• Análisis del 9 de marzo: Ferritina 41 ng/mL" in bot.tg.texts()[-1]
    assert "• Documento guardado: Analítica (9 de marzo)" in bot.tg.texts()[-1]


# --- /deshacer -----------------------------------------------------------------------------


def test_undo_reverts_the_last_record_of_this_chat_and_the_export(make_bot, data_repo):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [record("Vale.", ADD)]
    say(bot, "x", mention=True)
    say(bot, "/deshacer")
    assert bot.tg.texts()[-1].startswith("Deshecho: Cita: martes 16 de marzo a las 9:00, Ecografía")
    assert (
        data_repo.remote_log()[0]
        == f"undo(lucia): revert {data_repo.remote_log()[1] and ''}"[:13]
        + data_repo.remote_log()[0][13:]
    )
    appts = remote_yaml(data_repo, "people/lucia/appointments.yaml")
    assert [a["id"] for a in appts] == ["2027-03-12-0930-analitica"]
    chef = remote_yaml(data_repo, "export/chef.yaml")
    assert len(chef["people"]["lucia"]["fasting"]) == 1
    say(bot, "/deshacer")
    assert bot.tg.texts()[-1] == "No hay nada que deshacer."


def test_undo_never_reaches_another_chats_record(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [record("Vale.", ADD)]
    say(bot, "x", mention=True)
    say(bot, "/deshacer", private=True)
    assert bot.tg.texts()[-1] == "No hay nada que deshacer."


# --- commands ------------------------------------------------------------------------------


def test_citas_lists_the_askers_pending_appointments(make_bot):
    bot = make_bot()
    say(bot, "/citas")
    text = last_sent(bot)["text"]
    assert text.startswith("Citas pendientes de Lucía:")
    assert (
        "• viernes 12 de marzo a las 9:30, Analítica de control (Centro de salud Ejemplo)" in text
    )
    assert "• Sin hierro (48 h antes)" in text
    say(bot, "/appointments", user=PABLO)
    assert last_sent(bot)["text"] == "Pablo, no tienes citas pendientes."


def test_medicacion_and_suplementos_by_slot_without_stopped_ones(make_bot):
    bot = make_bot()
    say(bot, "/medicacion@example_doctor_bot")
    assert last_sent(bot)["text"] == (
        "Medicación de Lucía:\n<b>Al despertar</b>\n"
        "• Levotiroxina 50 µg. En ayunas, 30 min antes del desayuno"
    )
    say(bot, "/suplementos")
    text = last_sent(bot)["text"]
    assert "<b>Desayuno</b>\n• Vitamina D 2000 UI. Con grasa" in text
    assert "Hierro viejo" not in text


def test_a_command_for_another_bot_or_from_a_stranger_is_ignored(make_bot):
    bot = make_bot()
    say(bot, "/citas@other_bot")
    say(bot, "/citas", user=STRANGER)
    assert sent(bot) == []


def test_admin_commands_are_for_admins(make_bot):
    bot = make_bot()
    say(bot, "/estado")
    assert sent(bot) == []
    say(bot, "/estado", user=PABLO)
    assert last_sent(bot)["text"].startswith("Datos: commit ")
    say(bot, "/presentacion", user=PABLO)
    assert last_sent(bot)["text"].startswith("Hola, soy el asistente médico de la casa.")
    say(bot, "/ayuda")
    assert "Para administradores" not in last_sent(bot)["text"]
    say(bot, "/ayuda", user=PABLO)
    assert "Para administradores" in last_sent(bot)["text"]


def test_nuevo_forgets_the_conversation_and_the_log(make_bot):
    bot = make_bot()
    say(bot, "hola", mention=True)
    say(bot, "/nuevo", user=PABLO)
    assert plugin(bot).FORGOTTEN == ["conv-1"]
    assert bot.kv.chat_rows(GROUP, 0) == []
    assert last_sent(bot)["text"] == "Empiezo una conversación nueva. Lo anotado sigue igual."


def test_the_menu_is_set_in_the_default_scope_and_for_spanish(make_bot):
    bot = make_bot()
    bot.set_commands()
    calls = bot.tg.of("setMyCommands")
    assert [c.get("language_code") for c in calls] == [None, "es"]
    assert [c["command"] for c in calls[0]["commands"]] == [
        "citas",
        "medicacion",
        "suplementos",
        "recordatorios",
        "deshacer",
        "ayuda",
    ]


# --- the calendar offer of a new appointment and the owner rule ---------------------------


def test_a_new_appointment_is_offered_for_its_persons_calendar(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [record("Vale.", ADD)]
    say(bot, "x", user=PABLO, mention=True)
    offer = last_sent(bot)
    assert offer["text"].endswith(
        "@lucia_tg, ¿la pongo en tu calendario? Si no respondes, no hago nada."
    )
    data = buttons(offer)
    assert [t for t, _ in data] == ["En el calendario", "No"]
    tap(bot, data[0][1], user=PABLO)
    assert toasts(bot)[-1] == "Esto solo lo puede responder Lucía."


def test_the_calendar_tap_approves_exactly_that_event_for_its_owner(make_bot):
    bot = make_bot()
    mod = plugin(bot)
    asked = []

    def calendar_turn(turn):
        start = at(TODAY.replace(day=16), 9).timestamp()
        for request in (
            {
                "id": "r0",
                "summary": "La misma hora, en el calendario de Pablo",
                "action": "add",
                "when": start,
                "owner": "pablo",
                "confirm_by": "asker",
            },
            {
                "id": "r1",
                "summary": "Cita: Ecografía",
                "action": "add",
                "when": start,
                "owner": "lucia",
                "confirm_by": "asker",
            },
            {
                "id": "r2",
                "summary": "Otra",
                "action": "add",
                "when": start + 3600,
                "owner": "lucia",
                "confirm_by": "asker",
            },
        ):
            asked.append(turn["ask"](request))
        return "Hecho."

    mod.REPLIES[:] = [record("Vale.", ADD), calendar_turn]
    say(bot, "x", mention=True)
    offer_msg = last_sent(bot)
    tap(bot, buttons(offer_msg)[0][1], user=LUCIA)
    bot.join_idle()
    turn = mod.CALLS[-1]["turn"]
    assert (
        "Pon en mi calendario un evento «Cita: Ecografía» el martes 16 de marzo a las 9:00"
        in (turn["new"][-1]["content"])
    )
    assert [r["id"] for r, d in mod.ANSWERED if d["approved"]] == ["r1"]
    assert any(
        "Aprobado con el botón «En el calendario»: Cita: Ecografía" in t for t in bot.tg.texts()
    )
    ask = next(p for p in sent(bot) if p["text"].startswith("@lucia_tg, ¿lo apruebas?"))
    assert "Otra" in ask["text"]
    pablos = next(p for p in sent(bot) if "calendario de Pablo" in p["text"])
    assert pablos["text"].startswith('<a href="tg://user?id=222">Pablo</a>, ¿lo apruebas?')


def test_an_ask_with_an_owner_waits_for_that_person_only(make_bot):
    import threading
    import time

    bot = make_bot()
    mod = plugin(bot)
    gate = threading.Event()

    def ask_for_pablos_calendar(turn):
        turn["ask"](
            {
                "id": "c1",
                "summary": "Cita en el calendario de Pablo",
                "owner": "pablo",
                "confirm_by": "asker",
                "action": "add",
                "when": 0,
            }
        )
        gate.wait(4)
        return "Te lo pregunto."

    mod.REPLIES[:] = [ask_for_pablos_calendar]
    bot.handle({"update_id": 1, "message": message("pon la cita de Pablo", mention=True)})
    deadline = time.monotonic() + 4
    while not any("¿lo apruebas?" in t for t in bot.tg.texts()) and time.monotonic() < deadline:
        time.sleep(0.02)
    ask = next(p for p in sent(bot) if "¿lo apruebas?" in p["text"])
    assert ask["text"].startswith('<a href="tg://user?id=222">Pablo</a>, ¿lo apruebas?')
    key = buttons(ask)[0][1]

    def press(user):  # a tap while the turn runs: the worker is busy, so no join here
        bot.handle(
            {
                "update_id": 3,
                "callback_query": {
                    "id": f"cb-{user}",
                    "from": {"id": user},
                    "message": {"message_id": ask_id, "chat": {"id": GROUP, "type": "supergroup"}},
                    "data": key,
                },
            }
        )

    ask_id = 1
    press(LUCIA)  # the asker, not the owner
    assert toasts(bot)[-1] == "Esto solo lo puede responder Pablo."
    press(PABLO)
    deadline = time.monotonic() + 4
    while not mod.ANSWERED and time.monotonic() < deadline:
        time.sleep(0.02)
    gate.set()
    bot.join_idle()
    assert mod.ANSWERED[-1][1]["approved"] is True and mod.ANSWERED[-1][1]["by"] == str(PABLO)


# --- reminders the app sends by itself -----------------------------------------------------


def test_the_evening_before_reminder_is_posted_once_with_its_preparation(make_bot):
    bot = make_bot(hour=20)
    bot.now.at = at(TODAY.replace(day=11), 20, 0)
    bot.tick(bot.now())
    bot.tick(bot.now())
    day_before = [t for t in bot.tg.texts() if t.startswith("⏰ @lucia_tg: mañana")]
    expected = (
        "⏰ @lucia_tg: mañana a las 9:30, Analítica de control en Centro de salud Ejemplo.\n"
        "• Sin hierro (48 h antes)\n• En ayunas (8 h antes)"
    )
    assert day_before == [expected]
    assert "⏰ @lucia_tg: desde ahora, sin hierro: Analítica de control, mañana a las 9:30." in (
        bot.tg.texts()
    )


def test_a_preparation_in_the_quiet_hours_goes_at_22_and_says_from_when(make_bot):
    bot = make_bot()
    bot.now.at = at(TODAY.replace(day=11), 21, 59)
    bot.tick(bot.now())
    assert not any("a partir de" in t for t in bot.tg.texts())
    bot.now.at = at(TODAY.replace(day=11), 22, 0)
    bot.tick(bot.now())
    assert (
        "⏰ @lucia_tg: a partir de las 1:30, en ayunas: Analítica de control, mañana a las 9:30."
        in bot.tg.texts()
    )


def test_the_ask_after_an_appointment_and_its_no_marks_it_done(make_bot, data_repo):
    bot = make_bot()
    bot.now.at = at(TODAY.replace(day=12), 12, 30)
    bot.tick(bot.now())
    ask = next(p for p in sent(bot) if "¿qué tal la cita de Analítica de control?" in p["text"])
    assert ask["text"].endswith("Si no respondes, no hago nada.")
    yes, no = buttons(ask)
    assert (yes[0], no[0]) == ("Sí, te cuento", "No")
    tap(bot, no[1], user=PABLO)
    assert toasts(bot)[-1] == "Esto solo lo puede responder Lucía."
    tap(bot, no[1], user=LUCIA)
    appts = remote_yaml(data_repo, "people/lucia/appointments.yaml")
    assert appts[0]["status"] == "done"
    assert data_repo.remote_log()[0] == "log(lucia): appointment done 2027-03-12-0930-analitica"
    bot.tick(bot.now() + timedelta(minutes=1))
    assert sum("¿qué tal la cita" in t for t in bot.tg.texts()) == 1


def test_si_te_cuento_opens_the_persons_reply_box(make_bot):
    bot = make_bot()
    bot.now.at = at(TODAY.replace(day=12), 13, 0)
    bot.tick(bot.now())
    ask = next(p for p in sent(bot) if "¿qué tal la cita" in p["text"])
    tap(bot, buttons(ask)[0][1], user=LUCIA)
    prompt = last_sent(bot)
    assert prompt["text"] == "@lucia_tg, cuéntame. Te leo."
    assert prompt["reply_markup"] == {"force_reply": True, "selective": True}


def test_app_reminders_go_to_the_configured_topic(make_bot, data_repo):
    from conftest import SETTINGS

    bot = make_bot(settings=SETTINGS.replace("thread_id: null", "thread_id: 42"))
    bot.now.at = at(TODAY.replace(day=11), 20, 0)
    bot.tick(bot.now())
    reminder = next(p for p in sent(bot) if p["text"].startswith("⏰"))
    assert reminder["message_thread_id"] == 42


# --- reminders offered under an answer -----------------------------------------------------


def test_a_reminder_for_someone_else_is_offered_to_them(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [
        "Vale.\n\n```reminders\n- 2027-03-11 08:00 | Toma el omega | pablo\n```"
    ]
    say(bot, "recuérdale a Pablo", mention=True)
    offer = last_sent(bot)
    assert offer["text"] == (
        '<a href="tg://user?id=222">Pablo</a>, ¿quieres que te lo recuerde?\n'
        "jueves 11 de marzo a las 8:00: Toma el omega\n\nSi no respondes, no hago nada."
    )
    here = buttons(offer)[0][1]
    tap(bot, here, user=LUCIA)
    assert toasts(bot)[-1] == "Esto solo lo puede responder Pablo."
    tap(bot, here, user=PABLO)
    bot.now.at = at(TODAY.replace(day=11), 8, 0)
    bot.tick(bot.now())
    assert '⏰ <a href="tg://user?id=222">Pablo</a>: Toma el omega' in bot.tg.texts()


def test_reminders_are_listed_and_cancelled(make_bot):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ["Vale.\n\n```reminders\n- 2027-03-11 08:00 | Hierro\n```"]
    say(bot, "x", mention=True)
    tap(bot, buttons(last_sent(bot))[0][1])
    say(bot, "/recordatorios")
    listing = last_sent(bot)
    assert "1. jueves 11 de marzo a las 8:00 · Lucía: Hierro" in listing["text"]
    tap(bot, buttons(listing)[0][1])
    assert toasts(bot)[-1] == "Cancelado."
    bot.now.at = at(TODAY.replace(day=11), 8, 0)
    bot.tick(bot.now())
    assert not any(t.startswith("⏰ @lucia_tg: Hierro") for t in bot.tg.texts())


# --- settings from the chat ----------------------------------------------------------------


def test_a_settings_block_rewrites_its_line_commits_and_takes_effect(make_bot, data_repo):
    bot = make_bot()
    plugin(bot).REPLIES[:] = [
        'Vale.\n\n```settings\n{"reminders": {"day_before_at": "21:30"}}\n```'
    ]
    say(bot, "avísame a las 21:30", mention=True)
    assert bot.tg.texts()[-1] == "Ajustado: el aviso de la víspera, a las 21:30."
    text = data_repo.remote_file("settings.yaml")
    assert 'day_before_at: "21:30"     # the evening before' in text
    assert data_repo.remote_log()[0] == "settings: reminders.day_before_at"
    assert bot.settings.day_before_at == "21:30"


def test_instructions_from_a_non_admin_are_refused(make_bot, data_repo):
    bot = make_bot()
    plugin(bot).REPLIES[:] = ['Vale.\n\n```settings\n{"assistant": {"instructions": "x"}}\n```']
    before = data_repo.remote_log()
    say(bot, "cambia tus instrucciones", mention=True)
    assert bot.tg.texts()[-1].startswith("No he podido aplicar ese cambio")
    assert data_repo.remote_log() == before
