# Lity

A lightweight, single-user personal agent built around a deliberately
**starved main thread** so it stays cheap now and can run on a small local
model later. Lity itself only converses, remembers and schedules — **all
real work is executed by an external [Hermes Agent](https://github.com/NousResearch)**
through its runs API. Designed to install and run on a Raspberry Pi next to
Hermes.

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

## Quick start

```bash
./install.sh                 # Linux / Pi / macOS / WSL2 — deps + voice + key wizard
./lityctl start              # add --voice for the voice assistant
```

Settings live in config.yaml + .env and are managed from the CLI:

```bash
./lityctl setup              # guided wizard: provider, models, keys, voice
./lityctl show               # current settings, keys masked
./lityctl set models.main gpt-5.4-mini
./lityctl set voice.enabled true
./lityctl key SPEECHMATICS_API_KEY   # prompts hidden, writes .env
```

Hermes side: run `hermes gateway` with `API_SERVER_ENABLED=true` (default
port 8642) and point `hermes.base_url` in config.yaml at it.

Open **http://localhost:8321** — you land in the Home thread. Task
sub-threads appear automatically when the agent delegates work; you can open
your own with *+ new thread*.

## The one-paragraph mental model

The **kernel** (main thread) runs on a small cheap model with a ~1.6k-token
system prompt and a 20-message window; it can only converse, remember,
schedule, and `delegate`. Every delegated task becomes a **run on the Hermes
Agent** (terminal, browser, files, coding, email/calendar — Hermes's full
toolbelt); progress streams into a task thread, Hermes permission requests
appear as normal Lity approval cards, and only a compressed ≤300-token
result returns to the kernel. A **compactor** folds old messages into one
rewritable summary, a **memory** pipeline extracts facts in the background
(FTS5 recall), and a **scheduler** runs timers, crons, and a 30-minute
heartbeat. Kernel tools carry permission levels 0–4; anything above your
`autonomy_level` pops an approval card in the UI. Read-only tools can be
routed **direct-to-user** (the model sets `direct_to_user: true` and the raw
tool output becomes the reply — no second model pass), files/images flow
both ways as chat attachments (`send_file` tool + 📎 upload button), and the
**⚙ settings dashboard** lets you edit config.yaml and the identity files
and inspect the tool registry from the browser.

## Configure

Everything lives in [config.yaml](config.yaml) (models, budgets, autonomy
level, heartbeat, the Hermes gateway) and the `workspace/` markdown files:

| File | Role |
|---|---|
| `workspace/SOUL.md` | personality & behavioural defaults (system slot 1) |
| `workspace/USER.md` | who you are — the agent maintains it |
| `workspace/HEARTBEAT.md` | standing checks evaluated every heartbeat |
| `workspace/AGENTS.md` | standing rules sent with every Hermes task |

## Voice assistant (in-process)

The former pipy_catty voice bot now lives inside Lity (`lity/voicebot/`) —
one process, no HTTP loopback, no separate install:

```
mic → openWakeWord gate → Speechmatics STT → kernel (direct) → Kokoro TTS → speaker
```

- **Wake word** ("hey jarvis" by default) gates the mic locally — zero STT
  cost while idle; saying it while the bot talks barges in.
- The pipeline's "LLM" is the **kernel itself** (no localhost API hop);
  replies are TTS-sanitized, and conversation memory stays server-side.
- **Proactive push**: finished-task results are spoken and new approvals
  beep (double-high tone) the moment they happen, via the internal event
  bus — ask "what's pending?" and approve by voice.
- Enable with `./lityctl start --voice`, or permanently via
  `voice.enabled: true` in config.yaml. Tune wake word, thresholds, and
  audio device indices in the `voice:` section (`./lityctl devices` lists
  them). Needs `SPEECHMATICS_API_KEY` in .env; Kokoro TTS is local (models
  auto-download on first run).
- The `/v1` OpenAI-compatible endpoints remain for **remote** voice
  satellites; local and remote share one spoken-message cursor, so nothing
  is ever spoken twice.

## Going local (the endgame)

Point `provider.base_url` at Ollama (`http://localhost:11434/v1`) and set
`models.main` to a small local model. The kernel's context discipline is
what makes this swap realistic; Hermes keeps whatever models it is
configured with, independently.
