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
./install.sh                 # Linux / Pi / macOS / WSL2
# or manually:
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # OpenRouter key + HERMES_API_KEY
python -m lity
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

## Going local (the endgame)

Point `provider.base_url` at Ollama (`http://localhost:11434/v1`) and set
`models.main` to a small local model. The kernel's context discipline is
what makes this swap realistic; Hermes keeps whatever models it is
configured with, independently.
