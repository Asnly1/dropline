# dropline

> A locally-run AI virtual character mail system — let AI characters stay in touch with you through **real email inboxes**, long-term.

🌏 中文: [README.cn.md](README.cn.md)

[![CI](https://github.com/Asnly1/dropline/actions/workflows/ci.yml/badge.svg)](https://github.com/Asnly1/dropline/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-%E2%89%A5%203.12-blue)
![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Windows-lightgrey)
![Storage](https://img.shields.io/badge/Storage-SQLite%20%2B%20JSON%2FMarkdown-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## ✨ Highlights

- 📬 **Real email communication** — IMAP/SMTP driven, not a chat window. All correspondence lives in your mailbox forever.
- 👥 **Profile-first structure** — each `character × relationship` pair has its own prompts, memory, and archive.
- 🧠 **Two-stage LLM pipeline** — Stage 1 understands the incoming letter and extracts candidate memories; Stage 2 writes the reply in character.
- 🖼️ **Optional image generation** — Stage 2 produces a `scene_card` rendered by a separate image provider. Failures halt the run cleanly.
- ⏰ **Cross-platform scheduling** — one-command install for macOS `launchd` and Windows Task Scheduler.
- 🔒 **Local, auditable storage** — run state in SQLite; long-term memory and episodes are plain JSON files you can read and hand-edit.

> ⚠️ **Privacy — "local" means storage, not processing.** dropline is **not** a local-model setup. Every run sends the incoming mail body, your memory files, and the character prompts to whichever external LLM / image provider you configure (OpenAI, Gemini, Anthropic, …). Only storage stays on your machine — pick a provider you trust with that content.

---

## 🏗️ How it works

One full pipeline run, end to end:

```mermaid
flowchart TD
    A[Scheduler or run-once] --> B[Load Active Profile<br/>character + relationship]
    B --> C[IMAP: fetch new mail]
    C --> D[Normalize inbound content]
    D --> E[Stage 1 LLM<br/>understand + candidate memories]
    E --> F[Code promotes entries into memory/]
    F --> G[Stage 2 LLM<br/>compose in-character reply]
    G -->|image.enabled=true| H[Image generation]
    G -->|false| I[Skip image]
    H --> J[SMTP: send]
    I --> J
    J --> K[Archive: archive/ + state.db]
```

Key design choices:

- **LLM proposes, code decides** — Stage 1 only emits _candidate_ memories. The decision of what gets written, and to which file, is made by local code following [docs/memory_promotion_rules.en.md](docs/memory_promotion_rules.en.md).
- **Image is a hard gate** — if image generation is enabled and fails, the run aborts and memory stays untouched (set `llm.image.on_failure=text_only` to fall back to a text-only email instead).
- **Everything is archived** — inputs, Stage 1/2 outputs, and the generated image for each run are saved under `archive/YYYY-MM-DD/run-*/`.

> 📬 **See a real run:** [**examples/conversation_keqing.en.md**](examples/conversation_keqing.en.md) walks through Keqing's `init-once` opening letter and the first round of back-and-forth — with the real generated images, and a collapsible look at the Stage 1 memory candidates behind each reply. ([中文版](examples/conversation_keqing.cn.md))

---

## 📂 Profile layout

Three-tier directory — character, relationship, session — each independent:

```mermaid
flowchart LR
    P[profiles/] --> A[active.json]
    P --> C[characters/]
    C --> CN["&lt;character&gt;/"]
    CN --> M[meta.json]
    CN --> PR["prompt/<br/>persona · world_state<br/>cast_relations · cast_pool<br/>translation_layer"]
    CN --> R["relationships/&lt;rel&gt;/<br/>prompt/relationship.md"]
    CN --> S["sessions/&lt;rel&gt;/"]
    S --> MEM["memory/<br/>user_profile · relationship_memory<br/>active_arc · conflict_notes"]
    S --> EP["episodes/<br/>user/ + character/"]
    S --> AR["archive/YYYY-MM-DD/run-*"]
```

- `profiles/active.json` points to the currently active character + relationship.
- A **session** is uniquely identified by `character × relationship` — switching relationship gives you a brand-new memory space.
- The CLI operates on human-readable names (character name + relationship description). Slugs are internal directory keys only.

---

## 🚀 Quickstart

### Requirements

- Python `>= 3.12`
- A mailbox (QQ / Gmail / 163 / etc.) with IMAP/SMTP app-password enabled — this becomes the **character's mailbox**
- An API key for at least one LLM provider

### Install

```bash
git clone <this-repo>
cd dropline
python3 -m pip install -e ".[dev]"
```

If you already use `uv`, this is also fine:

```bash
uv pip install -e ".[dev]"
```

After installation, you can run `dropline ...` directly or keep using `python3 -m dropline ...`.
For development checks:

```bash
python3 -m pytest
```

### Configure

The repo ships with a placeholder `config.example.yaml` at the root. Copy and edit that file directly.

```bash
cp config.example.yaml config.yaml   # optional private backup
# then edit config.yaml by hand
```

#### Core configuration

dropline talks to **two** mailboxes, so set both before the first run:

- **The character mailbox** — a real, dedicated email account the AI logs into to read your letters and send its replies. Give the character its own account; don't reuse your personal inbox.
- **Your inbox** (`primary_user_recipient`) — where the character's letters are delivered. You write _to_ the character mailbox; the character writes _back_ to you.

`allowlist_senders` is a hard gate: any inbound message from an address not on the list is ignored, so only you can reach the character.

```yaml
mail:
  allowlist_senders:
    - "you@example.com" # only mail from these senders is ever processed
  primary_user_recipient: "you@example.com" # replies land here (your own inbox)
  character_mailbox:
    address: "character@example.com"
    imap:
      host: "imap.example.com"
      port: 993
      ssl: true
      username: "character@example.com"
      password: "APP_PASSWORD" # a mail app-password, not your login password
      folder: "INBOX"
    smtp:
      host: "smtp.example.com"
      port: 465
      security: "ssl"
      username: "character@example.com"
      password: "APP_PASSWORD"

project:
  timezone: "America/Los_Angeles" # IANA name; anchors the schedule window
```

If you want to keep your real credentials only in your local dev copy, maintain `config.yaml` and pass `--config /absolute/path/config.yaml` when running commands.
If that private config file lives outside the repo root, explicitly set the real repo root under `project`:

```yaml
project:
  repo_root: "/absolute/path/to/dropline-repo"
```

When `project.repo_root` is omitted, dropline keeps the old behavior: the config file's parent directory is treated as the repo root.

SMTP transport security uses an explicit enum:

```yaml
mail:
  character_mailbox:
    smtp:
      security: "ssl" # ssl | starttls | none
```

`ssl` means direct `SMTP_SSL`, `starttls` means plain SMTP followed by STARTTLS, and `none` means no transport encryption.

To pin the language used by model outputs, add `language` under `llm`:

```yaml
llm:
  language: "English"
  stage1:
    provider: "openai"
  stage2:
    provider: "openai"
  image:
    enabled: true
    provider: "openai"
```

`llm.language` controls Stage 1, Stage 2, `generate-character`, `generate-relationship`, `scene_card`, and the final image prompt language. The accepted exact values are `简体中文` and `English`.

For text-only email, set `llm.image.enabled=false`:

```yaml
llm:
  image:
    enabled: false
```

When disabled, image generation is skipped entirely, Stage 2 no longer needs to emit a `scene_card`, and the email sends body text only without calling an image provider.

Email bodies are sent as plain text. Stage 2 emits the reply in the `body` field; Markdown is not rendered. The HTML alternative only escapes text and wraps line breaks / paragraphs.

### Create your first character + relationship

```bash
# Fictional character from a work: pass --work
python3 -m dropline generate-character --character "刻晴" --work "原神"
# ("Keqing" from "Genshin Impact" — Chinese names work as slugs)

# Real-life public figure: omit --work; it falls back to "public image only" mode
python3 -m dropline generate-character --character "Some Author"

# Generate the relationship prompt
python3 -m dropline generate-relationship \
    --character "刻晴" \
    --relationship "childhood friends, reunited after years, reserved but familiar"
```

### Run the pipeline once

```bash
# Select the active profile (auto-creates the session)
python3 -m dropline select --character "刻晴" --relationship "青梅竹马"

# Initialize the session's memory and state.db
python3 -m dropline init-state

# Kick off a full run (fetch → understand → reply → send → archive)
python3 -m dropline run-once
```

> ⚠️ First-run semantics: the first `run-once` does not process historical inbound mail; it only creates the initial proactive email. Messages received before that first proactive email may not enter the conversation.

> 💡 Don't want to memorize flags? Just run `python3 -m dropline` to drop into the interactive CLI — it prompts for any missing arguments.

---

## 🧠 Memory system

A session's memory is split into six kinds. Stage 1 only **proposes**; code **decides** whether to promote:

| File / Directory                  | Kind                     | Typical content                                                    |
| --------------------------------- | ------------------------ | ------------------------------------------------------------------ |
| `memory/user_profile.json`        | Stable facts             | User's long-term goals and core preferences (highest bar)          |
| `memory/relationship_memory.json` | Relationship conventions | Nicknames, shared in-jokes, the relationship definition            |
| `memory/active_arc.json`          | In-progress arc          | Recurring, unresolved themes within the last 7 days (max 1–2)      |
| `episodes/user/*.json`            | User short-term          | Daily mood, temporary worries (weight decays after 14–30 days)     |
| `episodes/character/*.json`       | Character short-term     | What the character handled this turn                               |
| `memory/conflict_notes.json`      | Conflicts                | New info contradicting old memory — parked here until re-confirmed |

Both the four memory files and the episodes are plain JSON arrays of entries — UTF-8 text you can open and hand-edit. The pipeline rewrites them after each run.

Promotion is strict: one-off emotions, unverified speculation, and details the character fabricates **never** get promoted. Full rules: [docs/memory_promotion_rules.en.md](docs/memory_promotion_rules.en.md).

---

## ⏰ Scheduling

Recommended pattern: **fire `scheduler-tick` on a cron-like interval**, let it decide whether it's actually time to run — no long-lived process.

### One-shot install

```bash
python3 -m dropline install-scheduler --config /absolute/path/config.yaml
python3 -m dropline uninstall-scheduler --config /absolute/path/config.yaml
```

Optional flags:

- `--task-name` — override the task name (default `dropline`)
- `--python` — override the Python interpreter (defaults to the current `sys.executable`)
- `--no-load` — generate install artifacts but don't register with the OS

Logs land in:

- `runtime_logs/dropline.out.log`
- `runtime_logs/dropline.err.log`

Trigger logic: the scheduler wakes every `schedule.check_every_minutes` minutes. If outside the window or before the daily target time, it exits fast. If on target, it runs a full pipeline.

All schedule keys:

```yaml
schedule:
  enabled: true # master switch for scheduled runs
  window_start: "20:00" # earliest send time of day
  window_end: "23:59" # latest send time (set earlier than start to cross midnight)
  daily_target_pick_time: "00:05" # after this time, dropline locks in the day's send slot
  check_every_minutes: 30 # how often the OS timer fires scheduler-tick
```

Each day the actual send time is **randomized within the window** (stable per date), so the character doesn't always write at the same minute.

Schedule windows may cross midnight. Example:

```yaml
schedule:
  window_start: "22:00"
  window_end: "01:00"
```

This means 22:00 on the scheduled date through 01:00 the next day. After 01:00, dropline will not keep catching up that window.

### macOS (launchd)

```bash
# Check status
launchctl print gui/$(id -u)/dropline

# Manually trigger now
launchctl kickstart -k gui/$(id -u)/dropline

# Uninstall by hand
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/dropline.plist
rm ~/Library/LaunchAgents/dropline.plist
```

### Windows (Task Scheduler)

```powershell
# Check status
schtasks /Query /TN "dropline" /V /FO LIST

# Manually trigger now
schtasks /Run /TN "dropline"

# Uninstall by hand
schtasks /Delete /TN "dropline" /F
```

---

## 🛠️ Failure handling & alerts

When nobody is watching, dropline contains the common failures by itself and emails you when a human is needed.

### How retries work

- **Back off, then retry** — after a failed run the scheduler waits at least `schedule.retry_after_minutes` before trying again (tick log event `skipped_backoff`); scheduler-triggered runs are capped at `schedule.max_runs_per_day` per day (`skipped_daily_limit`), while a manual `run-once` is never limited; the "catch up yesterday's missed send" path is attempted only once (`catch_up_succeeded` / `catch_up_failed`). One success resets the consecutive-failure counter.
- **Retries reuse archived output** — when the same batch (same `batch_id`) is retried and the failed run already archived `stage2.json`, Stage 1 and Stage 2 are skipped and the run goes straight to image generation and sending; with only `stage1.json`, just Stage 2 runs again and the user-side episode is not written twice. The log names the run whose artifacts were reused (`reused_run_id=...`).
- **Inbound quarantine** — when a reply-mode run fails in the stage1 / stage2 / validation / image stage, every inbound message in that batch gets `attempt_count` +1 and its `last_error` recorded; after `schedule.max_batch_attempts` attempts (only 2 for Stage 2 validation failures) the message is **quarantined** (`quarantined_at`). Quarantined mail leaves the batch, so the next run falls back to an initiative letter instead of letting one bad message block the character. `show-inbox` marks them as `QUARANTINED` with their `last_error`; once the cause is fixed, `requeue-inbound --uid N` puts a message back in the queue, and `delete-inbound --uid N` can delete it outright.
- **Image fallback** — `llm.image.on_failure` defaults to `abort` (image stays a hard gate); with `text_only`, an image generation error is logged as a WARNING, `image_failure.json` is written to the run archive, and the email goes out as plain text.
- **Zombie runs get closed** — at the start of every `scheduler-tick`, runs still `running` with a `started_at` older than 6 hours are marked `failed` (`error_stage=stale_running`); `mark-stale-runs-failed` remains available for older records.

### Inspect the state

```bash
python3 -m dropline status --config /absolute/path/config.yaml
```

Prints, for the active session: the most recent successful and failed run (run_id, stage, error message), the consecutive-failure count, unconsumed and quarantined inbound counts, pending outbound count, the IMAP cursors (`last_seen_uid` / `last_consumed_uid`, plus `uidvalidity` when the kv store has it), today's schedule (`scheduled_for_date` / `target_send_time` / `sent_time` / `runs_today`) and the time of the last alert.

### Alert emails

When the consecutive-failure count reaches `mail.alerts.after_failures`, or inbound mail gets quarantined, dropline sends a **plain-text system notification** from the character mailbox to `mail.alerts.recipient`: the sender display name is always `dropline` (never the character name), the subject starts with `[dropline] `, and the body states that it is a system notice rather than a letter, with the failed stage, the error message and the suggested next steps (`runtime_logs/`, `dropline status`, `requeue-inbound`). At most one alert is sent per day; a failure to send the alert itself is only logged and never affects the run.

Related keys (all optional, defaults shown):

```yaml
schedule:
  retry_after_minutes: 120 # minimum wait after a failed run before retrying
  max_runs_per_day: 3 # max scheduler-triggered runs per day (run-once does not count)
  max_batch_attempts: 3 # attempts per inbound batch before quarantine (2 for validation failures)

mail:
  alerts:
    enabled: true # master switch for alert emails
    recipient: "you@example.com" # defaults to primary_user_recipient
    after_failures: 2 # alert after this many consecutive failures

llm:
  image:
    on_failure: "abort" # abort | text_only
```

---

## 🔌 Supported LLM providers

| Provider  | Default text model             | Default image model          |
| --------- | ------------------------------ | ---------------------------- |
| OpenAI    | `gpt-5.5`                      | `gpt-image-2-2026-04-21`     |
| Gemini    | `gemini-3.1-pro-preview`       | `gemini-3-pro-image-preview` |
| Anthropic | `claude-opus-4-8`              | —                            |
| Grok      | `grok-4.20-0309-non-reasoning` | `grok-imagine-image-pro`     |
| Qwen      | `qwen3-max-2026-01-23`         | `qwen-image-2.0-pro`         |
| Doubao    | `doubao-seed-2-0-pro-260215`   | `doubao-seedream-5-0-260128` |
| DeepSeek  | `deepseek-v4-pro`              | —                            |
| GLM       | `GLM-5.2`                      | —                            |

> These are the models pre-filled in `config.yaml` — swap `<provider>.text_model` / `image_model` to use a different version any time.

Config rules:

- `llm.stage1.provider` / `llm.stage2.provider` choose the **text** provider
- `llm.image.provider` chooses the **image** provider
- You only need to configure the active `llm.stage1.provider`, `llm.stage2.provider`, and, when images are enabled, `llm.image.provider`
- The active text provider needs `api_key` and `text_model`; the active image provider also needs `image_model`
- Unused provider blocks may be deleted or left as placeholders. Missing `api_key` / model fields there will not block startup; fill them in when switching providers
- When `llm.image.enabled=false`, image generation is skipped entirely and Stage 2 no longer needs to emit a `scene_card`

---

## 📘 CLI reference

Enter the interactive CLI:

```bash
python3 -m dropline
```

<details>
<summary><b>Full command list</b></summary>

| Command                                     | Description                                                                                         |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `list`                                      | List all characters and relationships                                                               |
| `show-active`                               | Print the current active profile                                                                    |
| `show-inbox` / `show-outbox`                | Show current session's inbox / outbox                                                               |
| `generate-character`                        | LLM-generate character prompts (persona / world_state / …)                                          |
| `generate-relationship`                     | LLM-generate the relationship prompt                                                                |
| `select`                                    | Activate a character + relationship and create its session                                          |
| `delete-session`                            | Delete the target session's memory / episodes / archive; `--yes` skips confirmation                 |
| `delete-inbound`                            | Delete a single inbound (`--uid`) or clear the inbox (`--all`); `--yes` skips confirmation          |
| `delete-outbound`                           | Delete a single outbound (`--message-id`) or clear the outbox (`--all`); `--yes` skips confirmation |
| `requeue-inbound`                           | Clear the quarantine flag and failed-attempt count of one inbound message (`--uid`)                 |
| `init-state`                                | Initialize the active session's memory and `state.db`                                               |
| `run-once`                                  | Run one full pipeline immediately                                                                   |
| `scheduler-tick`                            | One scheduling check — runs the pipeline only if it's time                                          |
| `mark-stale-runs-failed`                    | Mark stale `running` runs as `failed` for the active session                                        |
| `status`                                    | Show recent runs, failure counters, inbox / outbox state and today's schedule for the session       |
| `install-scheduler` / `uninstall-scheduler` | Install / uninstall the OS-level timer                                                              |
| `reset-dev-state`                           | Wipe all sessions / logs / DB and rebuild from the active profile; `--yes` skips confirmation       |

</details>

## 📚 More docs

- [examples/conversation_keqing.en.md](examples/conversation_keqing.en.md) — a real first exchange (init-once + first round), with images and behind-the-scenes
- [docs/run_pipeline.en.md](docs/run_pipeline.en.md) — pipeline-stage I/O contract
- [docs/memory_promotion_rules.en.md](docs/memory_promotion_rules.en.md) — full memory promotion rules

---

## 📝 License

Released under the [MIT License](LICENSE) — free to use, modify, and redistribute with attribution.
