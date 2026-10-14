# Data format

SuperMedicalDoctor runs from a data directory: a git checkout of the household's private data
repository, mounted at `/data` (`SMD_DATA`) and owned by uid 1000. The app reads it before every
turn and commits every change it makes. Everything below uses fictional people, `lucia` and
`pablo`.

```
settings.yaml                         the bot's settings; people edit it, the bot edits two keys
people/<person>/profile.yaml          allergies, intolerances, conditions, diet, doctors, notes
people/<person>/meds.yaml             medicines and supplements, stopped ones kept
people/<person>/appointments.yaml     appointments with their preparation
people/<person>/labs.jsonl            one lab value per line
people/<person>/documents/            the original PDFs and photos
export/chef.yaml                      generated for a meal planner; never edited by hand
plugins/<file>.py                     the assistant plugin
state/                                git-ignored: smd.sqlite and the source cache
```

Every YAML file is UTF-8 and unknown keys are ignored and kept. The app writes `profile.yaml`,
`meds.yaml` and `appointments.yaml` by loading, changing and dumping the whole file, so comments
in them are not kept; write comments in `settings.yaml`, which the app edits by line.
`smd check --data <dir>` validates all of it.

## `settings.yaml`

```yaml
timezone: Europe/Madrid
language: es
telegram:
  chat_id: -1001234567        # the group, a supergroup; private chats with people are always answered
  thread_id: null             # forum topic for the messages the bot starts itself; null is General
  admins: [pablo]             # person ids: /estado, /nuevo, /presentacion, instructions
  history_scope: all          # all | asked: all keeps the group's other messages as context
  history_days: 14            # 0..60: the chat log a fresh conversation is recapped from
people:
  lucia: {name: Lucía, user_id: 111, username: lucia_tg, calendar: cal-lucia}
  pablo: {name: Pablo, user_id: 222, username: null, calendar: cal-pablo}
rules: []                     # household rules, strings, shown to the assistant
reminders:
  day_before_at: "20:00"      # the appointment reminder, the evening before
  followup: true              # ask how an appointment went
  quiet: ["23:00", "07:00"]   # nothing is posted inside
calendar:
  appointments: true          # offer each new appointment for the person's calendar
  reminders: true             # «En el calendario» on reminder offers
assistant:
  plugin: plugins/assistant.py
  timeout_s: 900
  model: null
  instructions: ""            # private rules, appended after the app's rules
sources: []                   # files from other repositories, below
exports:
  - {kind: chef, path: export/chef.yaml}
editable: []                  # docs the assistant may edit besides docs/HEALTH.md and people/
```

- A person id is lowercase letters, digits and `-`. Each `user_id` is a Telegram user id, an
  integer, unique. `username` makes the bot's mentions open the person's reply box.
- `calendar` is an opaque string the app hands to the plugin (`turn["calendars"]`) and never reads.
  A person without one gets no calendar offers.
- Only `reminders.day_before_at`, `reminders.followup` and `assistant.instructions` (admins only, at
  most 4,000 characters) can be changed with a settings block. The bot rewrites that one line, keeps
  every other byte and commits it as `settings: <keys>`. New instruction lines are appended; a change
  that drops or rewrites an existing line waits for an admin's «Sí».
- `editable` lists text files outside `people/` the assistant may edit with an edit block (below).
  Never `plugins/`, `deploy/`, `tests/`, `export/`, `state/` or the design docs.

### `sources`

Files from other repositories, fetched before every turn with their `ETag`, so an unchanged file
costs one `304`. Only the listed files are fetched, never a directory. A file over 2 MB is refused;
a failed fetch keeps the cached copy and the assistant is told it is stale.

```yaml
sources:
  - name: genome              # the files appear to the assistant as sources/<name>/<path>
    kind: github              # github | gitea
    repo: example/health-notes
    ref: main
    token_env: GENOME_TOKEN   # the environment variable holding a read token
    files:
      - {path: lucia/CURRENT_STATE.md, person: lucia}
      - {path: shared/interactions.md, person: [lucia, pablo]}
  - name: cycle
    kind: gitea
    base_url: http://gitea.example:3000
    repo: home/lucia-health
    ref: main
    token_env: DATA_TOKEN
    files:
      - {path: health/summary.md, person: lucia}
```

A file reaches a turn only when its `person` is in that turn's scope: in Pablo's private chat,
Lucía's files never travel.

## `people/<person>/profile.yaml`

```yaml
name: Lucía
birth_date: 1990-01-01
sex: female                   # female | male | null
blood_type: null
allergies:
  - {what: penicilina, reaction: urticaria, since: 2010}
intolerances: [lactosa]
conditions:
  - {name: Hipotiroidismo, since: 2019, status: active}     # active | past | suspected
diet:                         # food rules that follow from their health; exported to the chef
  - "Sin pomelo: interacciona con un medicamento que toma"
doctors:
  - {id: endo, name: "Dra. Ejemplo", specialty: Endocrinología, center: "Hospital Ejemplo", phone: null}
notes: []                     # facts told in the chat; the newest 30, 300 characters each
```

## `people/<person>/meds.yaml`

A list. Stopped items stay with `until`, so «¿qué tomaba en marzo?» has an answer.

```yaml
- id: levotiroxina            # [a-z0-9-], unique in the file; the bot makes it from the name
  name: Levotiroxina
  kind: medicine              # medicine | supplement
  dose: "50 µg"
  slot: waking                # waking | breakfast | lunch | dinner | bedtime | as_needed
  days: daily                 # daily | a list of monday..sunday
  food: "En ayunas, 30 min antes del desayuno"
  why: Hipotiroidismo
  prescribed_by: endo         # a doctors id, or null
  since: 2024-03-01
  until: null                 # still taken while null or in the future
  source: seed                # seed | hand | chat
```

## `people/<person>/appointments.yaml`

```yaml
- id: 2027-03-12-0930-analitica     # date, time (or "x"), the first word of what; the bot makes it
  date: 2027-03-12
  time: "09:30"                     # or null
  what: Analítica de control
  doctor: endo                      # a doctors id, or null
  place: "Centro de salud Ejemplo"
  prep:
    - {hours_before: 48, text: "Sin hierro"}
    - {hours_before: 8, text: "En ayunas"}
  chat: group                       # group | private: where its reminders go
  status: pending                   # pending | done | cancelled
  outcome: ""
```

The bot reminds each pending appointment the evening before at `day_before_at`, each preparation
line `hours_before` the start (inside the quiet hours: at 22:00 the evening before), and asks how it
went 3 hours after the start (20:00 when there is no time). Each goes once; moving the appointment
makes them due again. A preparation whose text means not eating («En ayunas», «ayuno»,
«fasting») is a fasting window in the chef export.

## `people/<person>/labs.jsonl`

One value per line, sorted by date then test, keys in this order, empty optional keys left out:
`date, test, value, unit, low, high, lab, doc, source`. `value` is a number or a short string
(«negativo»); `doc` is the filed original; `source` is `chat`, `seed` or an importer's name.

```json
{"date": "2027-03-09", "test": "Ferritina", "value": 23, "unit": "ng/mL", "low": 30, "high": 400, "doc": "people/lucia/documents/2027-03-09-analitica.pdf", "source": "chat"}
```

A value for a test already recorded on the same date replaces it, so a result read twice is one
line.

## `export/chef.yaml`

Generated in the same commit as every change, and at start when it no longer says what the data
says. Only these fields, nothing else ever:

```yaml
# Generated by SuperMedicalDoctor. Do not edit: the next change overwrites it.
generated: '2027-03-10T10:00:00+01:00'
people:
  lucia:
    allergies: [penicilina]
    intolerances: [lactosa]
    diet: ["Sin pomelo: interacciona con un medicamento que toma"]
    meds:                                   # still taken: name, kind, dose, slot, days, food
      - {name: Levotiroxina, kind: medicine, dose: 50 µg, slot: waking, days: daily,
         food: "En ayunas, 30 min antes del desayuno"}
    fasting:                                # fasting preparation of pending appointments, 14 days
      - {from: '2027-03-12T01:30:00+01:00', until: '2027-03-12T09:30:00+01:00', text: En ayunas}
```

Never exported: conditions, lab values, documents, doctors, places, what an appointment is for,
why a medicine is taken, notes, birth date, anything from `sources`.

## Files the assistant edits

Besides records, the assistant keeps the files current with an ```` ```edit ```` block: a status,
a note, a new doctor's details. Each edit names a text file and gives its whole new content, one
exact replacement (`old`, `new`) or `delete: true`.

| path | who |
|---|---|
| `people/<id>/**` (not `labs.jsonl`, not `documents/`) | in the group, anyone; in a private chat, only that person; an admin, always |
| `settings.yaml` | admins; dropping an instruction line waits for «Sí» |
| `docs/HEALTH.md` and every path in `editable` | in the group, or an admin |

The edits are tried on a copy and must pass `smd check`; then they are committed with the
regenerated export, the change is shown line by line, and `/deshacer` reverts it. An edit the
assistant proposes on its own, the deletion of a file and the removal of more than half of one
wait for «Sí». At most 10 edits and 60,000 characters per answer, 20,000 per edit.

## The commits

| commit | made by |
|---|---|
| `log(<person>): <short list>` | a record from the chat, with the regenerated export |
| `undo(<person>): revert <commit>` | `/deshacer`, a `git revert` of the last record from that chat |
| `chat(<person>): edit <paths>` | an edit block, as the person who asked |
| `settings: <keys>` | a setting changed from the chat; never undone by `/deshacer` |
| `export: chef` | the export refreshed at start |

Commits are authored `SuperMedicalDoctor <supermedicaldoctor@localhost>`. The token goes in an
`Authorization` header through `GIT_CONFIG_*`, never in a URL or the checkout's config.
