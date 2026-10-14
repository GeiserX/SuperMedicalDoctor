<p align="center">
  <img src="https://raw.githubusercontent.com/GeiserX/SuperMedicalDoctor/main/docs/images/banner.svg" alt="SuperMedicalDoctor" width="100%">
</p>

<h1 align="center"><img src="https://raw.githubusercontent.com/GeiserX/SuperMedicalDoctor/main/docs/images/logo.svg" alt="" width="40" height="40"> SuperMedicalDoctor</h1>

<p align="center">
  <a href="https://github.com/GeiserX/SuperMedicalDoctor/releases"><img src="https://img.shields.io/github/v/release/GeiserX/SuperMedicalDoctor?style=flat-square" alt="Release"></a>
  <a href="https://github.com/GeiserX/SuperMedicalDoctor/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/GeiserX/SuperMedicalDoctor/ci.yml?style=flat-square&label=CI" alt="CI"></a>
  <a href="https://github.com/GeiserX/SuperMedicalDoctor/blob/main/LICENSE"><img src="https://img.shields.io/github/license/GeiserX/SuperMedicalDoctor?style=flat-square" alt="License"></a>
  <a href="https://hub.docker.com/r/drumsergio/supermedicaldoctor"><img src="https://img.shields.io/docker/pulls/drumsergio/supermedicaldoctor?style=flat-square" alt="Docker pulls"></a>
</p>

SuperMedicalDoctor is a household doctor assistant that lives in a Telegram group. It keeps each person's appointments, medicines and supplements, reads the lab results someone sends, reminds the appointments and what to prepare for them, and answers health questions from medical sources with the evidence behind them. Every record stays in a private git repository you own, and the AI sits behind a plugin file, so you choose the provider.

## Features

- Records appointments, medicines, supplements, allergies and diet rules from a message, a voice note or a photo of the slip, and confirms what it saved in its own words, never the assistant's.
- Reads a lab PDF or photo, files the original in the repository and stores each value as one line, so every new result is compared with the person's earlier ones.
- Reminds each appointment the evening before, each preparation in time (fasting, no iron 48 h before) and asks once afterwards how it went, all without the AI.
- Answers from the latest commit: it pulls the data repository before every turn and fetches named files from other health repositories with an ETag.
- Keeps each person's data theirs: a private chat carries only that person's files, and the group never hears one person's data unless they raised it there.
- Offers instead of acting: a new appointment for the person's calendar, a reminder here or in the calendar. Anything that leaves the chat waits for that person's Aprobar.
- Writes a diet-relevant export for a meal planner: allergies, intolerances, diet rules, the medicines taken and fasting windows, nothing else.
- One answer per burst of messages, edits taken into account, every voice note heard, Sí/No and option buttons, forum topics followed.
- `/citas`, `/medicacion`, `/suplementos`, `/recordatorios` and `/deshacer` work without any AI.
- English and Spanish ship; the assistant reads the shared assistant principles first.

## Quick start

Create a bot with BotFather, make it an administrator of the group (or turn its privacy mode off), and prepare a data directory as described in [the data format](https://github.com/GeiserX/SuperMedicalDoctor/blob/main/docs/data-format.md), owned by uid 1000:

```sh
docker run --rm -v "$PWD/data:/data" drumsergio/supermedicaldoctor:0.1.0 smd check
docker run -d --name supermedicaldoctor -e TELEGRAM_BOT_TOKEN -e SMD_GIT_TOKEN -e TZ=Europe/Madrid \
  -v "$PWD/data:/data" drumsergio/supermedicaldoctor:0.1.0
```

`smd check` validates the settings and every person's files. Only one process may poll a bot token; `smd say --intro` posts the introduction from the host without polling.

## Documentation

- [How it works](https://github.com/GeiserX/SuperMedicalDoctor/blob/main/docs/how-it-works.md): who reaches the assistant, the turn and the plugin contract, approvals, reminders, privacy, operations
- [Data format](https://github.com/GeiserX/SuperMedicalDoctor/blob/main/docs/data-format.md): `settings.yaml`, each person's files, sources and the chef export
- [The scripted example plugin](https://github.com/GeiserX/SuperMedicalDoctor/blob/main/tests/fixtures/fake_plugin.py)

## Related projects

[SuperCycleTracker](https://github.com/GeiserX/SuperCycleTracker) and [SuperMealPlanner](https://github.com/GeiserX/SuperMealPlanner) live in the same household chats and share the same chat machinery.

## License

[AGPL-3.0-or-later](https://github.com/GeiserX/SuperMedicalDoctor/blob/main/LICENSE)
