# How it works

SuperMedicalDoctor is one process: a Telegram long poll, a tick between polls, one worker thread
for assistant turns, one hearing thread for voice notes, one thread for answers to approvals and
one for slow plugin work. Standard library plus PyYAML. The data lives in a private git repository
([data format](data-format.md)); the AI lives in a plugin file in that repository, so the app
never knows which provider answers.

## Who reaches the assistant

- **The group** (`telegram.chat_id`, a supergroup) and **private chats with a person** in
  `settings.people`. Everyone else is ignored; a stranger's tap gets a toast.
- In the group, a text reaches the assistant when it **mentions the bot** or **replies to one of
  its messages**: a reply Telegram marks as the bot's, a reply to any id the bot remembers sending
  in that chat, or a reply from another topic or chat (`external_reply`) whose origin is the bot.
  The quoted part goes with it. A group text that does not reach it leaves one log line saying
  why (a reply to someone else, no reply and no mention, an anonymous admin), never its words. Every **voice note**, **photo**, **video**, **GIF**, **round video**, **sticker** and **file** of
  any kind from a person reaches it too: a voice note cannot carry a mention, and a lab sheet dropped in the doctor's group
  is meant for the doctor. The caption is the message.
- With `history_scope: all`, the group's other messages are kept in the chat log and go to the next
  turn as context marked `[not to you]`. They are never answered.
- In a private chat everything reaches it.
- The bot must read every group message: make it an administrator of the group, or turn its
  privacy mode off in BotFather (then remove it from the group and add it again). Without one of
  the two, Telegram delivers it neither plain text nor every voice note.

## A turn

One answer per burst: a chat's turn starts 2.5 s after its newest line, and waits up to 30 s for a
voice note still being heard. Before it builds the turn the app pulls the data repository (a pull
in the last 30 s is skipped), fetches the listed source files with their ETag, and downloads the
photos and files of the turn's lines (20 MiB each, 25 MiB per turn; the rest wait for the next
turn). What arrives while the plugin works is handed over through `incoming()`, except lines with
files, which go with the next turn.

**Scope is structural.** A private chat's turn carries only that person's folder and sources; a
group turn carries everyone's. The plugin cannot widen it, and a `record` block for someone outside
the scope, or for anyone but the asker in a private chat, is refused.

### The plugin

One Python file in the data directory (`assistant.plugin`), loaded again when it changes.

```python
def converse(turn: dict, settings: dict) -> dict | str: ...  # required
def forget(conversation: str, settings: dict) -> None: ...  # optional, /nuevo
def transcribe(audio: bytes, mime: str, language: str, settings: dict) -> str: ...  # optional
def answer(request: dict, decision: dict, settings: dict) -> dict: ...  # optional, approvals
```

`turn`:

| key | what |
|---|---|
| `conversation` | what the plugin returned last time in this chat, or null |
| `rules` | the shared principles, then `rules.md` filled in, then the household's rules and instructions |
| `context` | `context.md`: today, ages, pending appointments (60 days) and the last done, medicines and supplements by slot, the latest value of each lab test, the chat's reminders, each source's freshness, the data commit and whether the pull worked |
| `new`, `history` | the lines not kept yet (with `[not to you]` context), and the chat log of the last `history_days` without them |
| `scope` | the person ids whose data the turn carries |
| `asker` | the person the turn answers; null when it covers several people's lines |
| `files` | `{"data/<path>": text}`: `settings.yaml` without `assistant.instructions`, and every file under `people/<id>/` for the scope, except `documents/` |
| `sources` | `{"sources/<name>/<path>": text}` for source files whose person is in the scope |
| `attachments` | `[{"name": "inbox/<message id>-<safe name>", "mime", "data": bytes, "from": person}]` |
| `calendars` | `{person: calendar string}` for the scope |
| `incoming` | a callable: lines that arrived while the turn runs |
| `ask` | a callable, when the plugin defines `answer`: post an action for approval |

`settings` is `assistant:` minus `plugin` and `instructions`, plus `people` (each person's name,
user id, username and calendar) and `chat_id`.

The return value is a string or `{"text", "conversation", "kept", "covers", "options",
"reminders", "later"}`. `kept: false` (or an exception, or the timeout) is a failed turn: nothing is
applied and the lines go with the next one. `later` is a callable the app runs on its own thread
after posting the text; its string is posted too.

### Blocks in the answer

The app takes fenced blocks out of `text`; nobody sees them.

- **`record`** (JSON), one per person: appointments (`add`, `change`, `cancel`, `done`), medicines
  and supplements (`add`, `change`, `stop`), lab values, documents and profile lists (`add`,
  `remove`). Each block is checked all or nothing, then written with the regenerated chef export in
  one commit `log(<person>): <short list>`, pushed. The «Anotado:» lines under the answer are built
  from what was saved, never from the assistant's prose; a block that changes nothing says nothing,
  so an answer applied twice is harmless. A `doc` or `file` must be an attachment of this turn: the
  app files it in `documents/<date>-<title>.<ext>`.
- **`settings`**: `reminders.day_before_at`, `reminders.followup`, and `assistant.instructions` or
  `assistant.instructions_add` for admins. Lines are appended; dropping one waits for «Sí».
- **`edit`** (JSON): `{"asked": bool, "edits": [{"path", "content" | "old"+"new" | "delete"}]}`,
  file edits inside the allowed paths ([data format](data-format.md)), checked on a copy, committed
  with the export as `chat(<person>): edit …`, shown line by line and undone by `/deshacer`. Only
  an edit the person asked for goes at once; a proposed one, a file deletion, more than half of
  one or any change to `settings.yaml` waits for «Sí» (from an admin, for `settings.yaml`).
- **`options`**: up to five choices as buttons, for the people the answer covered, for 12 hours. A
  tap is that person's next message. «Sí»/«No» get their own hint line.
- **`reminders`**: `- YYYY-MM-DD HH:MM | text [| person]`, offered under the answer with
  «Recordatorio aquí», «En el calendario» and «No», for that person only, for 30 minutes.

Every offer says «Si no respondes, no hago nada.» and, untapped, gives way to «Sin respuesta: nada».

## Approvals and calendars

A plugin that defines `answer` gets `turn["ask"]`: it posts an action with Aprobar and Rechazar.
Who may tap:

- with **`owner: <person>`** in the request (every write to that person's calendar): that person
  alone, mentioned in the ask. Lucía may ask for Pablo's appointment to go in his calendar; the
  button then waits for Pablo.
- else the person the turn answers, when the request says `confirm_by: asker` and the turn carries
  one person's lines; else the admins.

After a new appointment is recorded (with `calendar.appointments`), «Anotado:» carries «En el
calendario» and «No» for that appointment's person. The tap becomes that person's message asking
for one event «Cita: <what>» at the appointment's time, and carries a grant: an ask of that turn
that adds exactly that event (same time, or same all-day dates) for that same owner is approved by
the tap. Anything else is asked as usual.

## What the app does by itself

From each pending appointment, with no AI, each once:

| kind | when | says |
|---|---|---|
| the evening before | `reminders.day_before_at`, until midnight | «⏰ @lucia_tg: mañana a las 9:30, Analítica de control en Centro de salud Ejemplo.» with the preparation lines |
| preparation | `hours_before` the start; inside the quiet hours, at 22:00 the evening before | «⏰ @lucia_tg: desde ahora, en ayunas: Analítica de control, mañana a las 9:30.» |
| the ask after | 3 h after the start (20:00 without a time), for 48 h | «¿Qué tal la cita de …?» with «Sí, te cuento» (opens the reply box) and «No» (marks it done) |

`chat: group` posts in the group (in `telegram.thread_id` when set) mentioning the person;
`private` posts in their private chat. Nothing is posted inside `reminders.quiet`.

## Commands

| command | who | does |
|---|---|---|
| `/citas` (`/appointments`) | people | the asker's pending appointments |
| `/medicacion` (`/meds`) | people | the asker's medicines still taken, by slot |
| `/suplementos` (`/supplements`) | people | the asker's supplements still taken, by slot |
| `/recordatorios` (`/reminders`) | people | the chat's reminders to come, «Cancelar n» each |
| `/deshacer` (`/undo`) | people | reverts the last record commit made from this chat |
| `/ayuda` (`/help`) | people | the list; admins also see theirs |
| `/estado` (`/status`) | admins | data sync, each source's fetch time, the last answer |
| `/nuevo` (`/new`) | admins | drops the conversation and the chat log |
| `/presentacion` (`/about`) | admins | posts the introduction |

`/cmd@bot` works. The menu is set at every start, in the default scope and for Spanish.

## Edits, voice and the rest of the machinery

- **Edits.** Still queued: the new wording goes. In the running turn: its open asks are declined
  and a correction joins the turn. Already answered: a new line asks the assistant to check its
  answer. Now addressed to the bot: a new message.
- **Voice.** `getFile`, the download, the plugin's `transcribe`, then «🎤 text» under the note. A
  note the plugin cannot hear, over 20 MiB, or empty, says so under it.
- **Forum topics.** The app answers in the topic a message came from, and stores the topic with
  every pending line, offer, ask and reminder. Its own messages are edited by chat and message id,
  and it remembers the ids it sent, so a reply to any of them reaches it.
- **Without an assistant**, commands, reminders and the follow-up's «No» work; free text gets
  «Ahora mismo no puedo leer mensajes escritos; en cuanto pueda, lo leo.» and waits until a plugin
  is configured.

## Privacy

- The service log holds event kinds and counts: never a message, a value,
  a name or a file name. A test runs a whole flow at DEBUG and fails if any planted word, value or
  name appears; the same test with one careless log line must go red.
- The chat log, the pending lines and the reminders live in `state/smd.sqlite`, mode 600, git-
  ignored. `/nuevo` purges the conversation and the chat log.
- The public image and repository hold only fictional data.

## Operations

| variable | what |
|---|---|
| `SMD_DATA` | the data directory, default `/data` |
| `TELEGRAM_BOT_TOKEN` | the bot token |
| `SMD_GIT_TOKEN` | pulls and pushes the data repository; also any source whose `token_env` names it |
| any other `token_env` | read tokens for sources |
| `TZ` | the container clock; dates follow `settings.timezone` |
| anything the plugin needs | passed through; the app never reads it |

One container, no published ports, a read-only root filesystem with a tmpfs on `/tmp`, uid 1000.
The healthcheck reads the age of `/tmp/smd.alive`, touched on every tick. Only one process may
poll a bot token; `smd say --intro` does not poll.

**Release.** On every push to `main` the Docker workflow reads `__version__`; when `v<version>` is
not tagged yet it builds `drumsergio/supermedicaldoctor:<version>` for amd64 and arm64, pushes it,
then tags the commit. A change that keeps the version ships nothing.
