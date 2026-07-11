# Lity

A lightweight, single-user personal agent — Hermes-Agent-class capabilities
(memory, personality, scheduling, sub-agents, browser control, Python
execution) built around a deliberately **starved main thread** so it stays
cheap now and can run on a small local model later. Designed to install and
run on a Raspberry Pi.

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

## Quick start

```bash
./install.sh                 # Linux / Pi / macOS / WSL2
# or manually:
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env         # put your OpenRouter key in it
python -m lity
```

Open **http://localhost:8321** — you land in the Home thread. Sub-threads
appear automatically when the agent delegates work; you can open your own
with *+ new thread*.

## The one-paragraph mental model

The **kernel** (main thread) runs on a small cheap model with a ~1.6k-token
system prompt and a 20-message window; it can only converse, remember,
schedule, and `delegate`. Five **sub-agents** (coder, researcher, browser,
shell, writer) run in parallel on stronger models with full tool sets and
their own threads; only a compressed ≤300-token result returns to the kernel.
A **compactor** folds old messages into one rewritable summary, a **memory**
pipeline extracts facts in the background (FTS5 recall), and a **scheduler**
runs timers, crons, and a 30-minute heartbeat. Tools carry permission levels
0–4; anything above your `autonomy_level` pops an approval card in the UI.
Read-only tools can be routed **direct-to-user** (the model sets
`direct_to_user: true` and the raw tool output becomes the reply — no second
model pass), files/images flow both ways as chat attachments (`send_file` tool
+ 📎 upload button), and the **⚙ settings dashboard** lets you edit
config.yaml, the identity files, every sub-agent, and inspect the tool
registry from the browser.

## Configure

Everything lives in [config.yaml](config.yaml) (models, budgets, autonomy
level, heartbeat) and the `workspace/` markdown files:

| File | Role |
|---|---|
| `workspace/SOUL.md` | personality & behavioural defaults (system slot 1) |
| `workspace/USER.md` | who you are — the agent maintains it |
| `workspace/HEARTBEAT.md` | standing checks evaluated every heartbeat |
| `workspace/AGENTS.md` | rules every sub-agent must follow |
| `agents/*.yaml` | the sub-agent registry — drop in a YAML to add one |

## Going local (the endgame)

Point `provider.base_url` at Ollama (`http://localhost:11434/v1`) and set
`models.main` to a small local model. The kernel's context discipline is
what makes this swap realistic; sub-agents can stay on cloud models or move
local independently.
