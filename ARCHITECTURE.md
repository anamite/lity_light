# Lity — Lightweight Personal Agent Architecture

A Hermes-Agent-class personal AI agent, rebuilt around one governing principle:

> **The main thread is sacred.** It stays tiny, cheap, and clean at all times — small
> enough that it can eventually run on a small local model on a Raspberry Pi.
> Everything heavy is delegated to purpose-built sub-agents that burn their own
> tokens in their own context and return only a compressed result.

Reference capabilities we match (from Nous Research's Hermes Agent): persistent
memory, SOUL.md personality, natural-language scheduling (crons + heartbeat),
sub-agents with isolated contexts, web search, full browser automation, Python
execution, and a gateway/API front door. What we deliberately do differently:
Hermes optimizes for capability breadth per conversation; Lity optimizes for a
starved, permanently-clean kernel context.

---

## 1. The two-tier model

```
┌─────────────────────────────────────────────────────────────────┐
│  KERNEL (main thread)                    ~1.6k token system     │
│  cheap/small model, later local          ~6–8k token window     │
│                                                                 │
│  System prompt slots (fixed budget):                            │
│    1. SOUL.md            ≤ 300 tok   personality, tone          │
│    2. USER.md            ≤ 300 tok   who the user is            │
│    3. Tool one-liners    ≤ 300 tok   ~9 kernel tools            │
│    4. Delegation policy  ≤ 200 tok   when to hand off + limits  │
│    5. Task board         ≤ 200 tok   open tasks: id·agent·state │
│    6. Injected memories  ≤ 200 tok   top-5 FTS hits, per turn   │
│    7. Thread summary     ≤ 300 tok   rolling, updatable         │
│                                                                 │
│  Window: last 20 messages verbatim; older → summary;            │
│  resolved tool call/result pairs collapsed to one line.         │
└───────────────┬─────────────────────────────────────────────────┘
                │ delegate(agent, task)          compressed result
                ▼                                (≤ 300 tokens) ▲
┌─────────────────────────────────────────────────────────────────┐
│  SUB-AGENTS (parallel asyncio tasks, own thread, own context)   │
│  coder · researcher · browser · shell · writer                  │
│  strong model per agent, full tool set, turn & token budgets    │
└─────────────────────────────────────────────────────────────────┘
```

The kernel NEVER: writes long code, reads whole web pages, drives the browser,
or holds a sub-agent's transcript. It converses, remembers, schedules, and
delegates. A sub-agent's work lives in its own DB thread (visible in the UI as
a sub-thread); only its final compressed result enters the kernel's context.

## 2. Directory layout

```
Lity_light/
├── config.yaml            # ALL runtime configuration (YAML, single file)
├── .env                   # OPENROUTER_API_KEY (never in config/git)
├── requirements.txt
├── install.sh             # plug-and-play setup (venv, deps, playwright, systemd)
├── workspace/             # the agent's "home" — human-editable markdown
│   ├── SOUL.md            # identity, voice, behavioral defaults (system slot 1)
│   ├── USER.md            # user profile, kept current by the agent (slot 2)
│   ├── MEMORY.md          # human-readable export of memory DB (regenerated)
│   ├── HEARTBEAT.md       # conditions the heartbeat evaluates each tick
│   └── AGENTS.md          # standing rules given to every sub-agent
├── agents/                # sub-agent registry — drop a YAML in, it exists
│   ├── coder.yaml         ├── researcher.yaml   ├── browser.yaml
│   ├── shell.yaml         ├── writer.yaml
│   └── prompts/<name>.md  # full system prompt per agent (only they pay for it)
├── lity/                  # the Python package
│   ├── __main__.py        # python -m lity → starts everything
│   ├── config.py          # YAML → typed config
│   ├── db.py              # SQLite schema + async helpers (WAL mode)
│   ├── llm.py             # OpenAI-compatible client (OpenRouter now, Ollama later)
│   ├── kernel.py          # main-thread agent loop
│   ├── context.py         # builds the budgeted kernel context
│   ├── compactor.py       # summarization + tool-pair collapsing
│   ├── memory.py          # parallel extraction + FTS5 recall
│   ├── approvals.py       # permission levels, approval queue
│   ├── tools/             # tool registry; every tool tagged with a level
│   ├── agents/            # registry.py (loads YAML) + runner.py (executes)
│   ├── sched/             # heartbeat.py + crons.py (timers, crons)
│   └── gateway/           # api.py (FastAPI + SSE) + events.py (in-proc bus)
├── web/index.html         # the entire frontend. One file, zero build step.
└── data/lity.db           # SQLite (WAL). The single source of truth.
```

## 3. Token discipline in the kernel (the whole point)

1. **Fixed system budget (~1.6k).** Every slot has a cap enforced at load time;
   SOUL.md/USER.md are truncated with a warning if oversized.
2. **Rolling window.** Only the last `kernel.max_window_messages` (20) enter
   context. Older messages are folded into the thread summary by the compactor.
3. **Updatable summary.** One summary row per thread, *rewritten* (not appended)
   each compaction — "timer set for 10 min and running" later becomes "timer
   fired at 14:32", exactly as in the design sketch.
4. **Tool-pair collapsing.** Once a tool call is resolved, the call+result pair
   is marked `collapsed` and replaced in context by a one-line event
   ("delegated research task #12 → done: <300-token result>"). Full records
   stay in SQLite for the UI/audit.
5. **Sub-agent result compression.** A runner's final answer over ~300 tokens
   is compressed by the utility model before it touches the parent thread.
6. **Memories are retrieved, not resident.** Nothing accumulates in the system
   prompt; the top-5 FTS matches for the *current* user message are injected
   per turn and disappear next turn.

Because the kernel only ever sees: tiny system prompt + summary + 20 messages +
9 short tool signatures, a 7–8B local model is a realistic future drop-in — the
`llm.py` provider is OpenAI-compatible, so pointing `provider.base_url` at
Ollama/llama.cpp is a config change, not a code change.

## 4. Kernel tool set (small, fixed — one-liners in the prompt)

| Tool | Level | Purpose |
|---|---|---|
| `recall(query)` | 0 | Search memory (FTS5) |
| `remember(content, kind)` | 2 | Save a durable fact (user/project/feedback/reference) |
| `delegate(agent, task, context)` | 2 | Spawn a sub-agent; creates a sub-thread; returns task id |
| `task_status(task_id)` | 0 | Check / fetch result of a task |
| `task_log(task_id, last_n)` | 0 | Peek inside a task's thread — the sub-agent's recent actions |
| `search_history(query)` | 0 | FTS5 over every past message in every thread ("what did we discuss…") |
| `cancel_task(task_id)` | 2 | Stop a running task |
| `schedule(spec, prompt)` | 2 | Timer or cron ("in 10m", "daily 09:00") |
| `list_schedules()` | 0 | Inspect timers/crons |
| `quick_search(query)` | 1 | One-shot web search, top-3 snippets only (cheap) |
| `update_user_profile(patch)` | 2 | Maintain USER.md |

Anything beyond this (run code, browse, shell, long research, write documents)
is *by construction* a delegation — the kernel has no tool for it.

### Direct-to-user tool output
Read-only tools marked `direct` (recall, quick_search, web_search, task_status,
list_schedules, read_file) expose an extra `direct_to_user: bool` parameter.
When the model sets it true, the tool's raw output is posted straight to the
user as the reply and **the turn ends — no second model pass, no summary**.
Default false keeps the classic call→result→model→reply flow for intermediate
steps. This saves one full model round-trip whenever the output is already
user-ready (weather, search snippets, a file's contents).

### Files in and out
`send_file(path, caption)` (level 0, available to the kernel and every
sub-agent) posts any workspace file into the chat as an attachment — images
render inline, everything else is a download link. Sub-agents send to the
parent (user-facing) thread. Users send files via `POST
/api/threads/{id}/files` (multipart) — saved under `workspace/uploads/`, shown
in chat, and announced to the kernel with the workspace path so agents can
work on them. `GET /api/files/{path}` serves workspace files (jailed).

## 5. Sub-agents

Defined declaratively in `agents/<name>.yaml`:

```yaml
name: coder
description: Writes and executes Python; use for any code, data, or file task.  # one line — this is ALL the kernel ever sees
model: anthropic/claude-sonnet-4.5      # per-agent override; default from config
prompt: prompts/coder.md                # full system prompt, loaded only in the sub-agent
tools: [python_run, read_file, write_file, list_files, shell]
max_turns: 25
max_tokens_total: 200000                # hard budget; task fails gracefully past it
level_cap: 3                            # highest permission level it may request
```

Initial registry:

| Agent | Model class | Tools | Job |
|---|---|---|---|
| **coder** | strong | python_run, file I/O, shell | Write/run Python, data work, file manipulation |
| **researcher** | mid | web_search, fetch_url, file write | Multi-step web research → cited brief |
| **browser** | strong | Playwright suite (goto, snapshot, click, type, screenshot) | Interactive web tasks: forms, logins, navigation |
| **shell** | mid | shell, file I/O | Installs, system setup, diagnostics |
| **writer** | mid | file I/O, fetch_url | Long-form documents, reports |

Runner contract: fresh context = agent prompt + AGENTS.md + task + optional
context hints from the kernel. Runs as an asyncio task in parallel; every tool
call passes the approval gate; progress events stream to its sub-thread in the
UI; on finish the compressed result is written into the parent thread as the
`delegate` tool result and the kernel is woken to relay it conversationally.

Adding a capability later = adding one YAML + one prompt file. No code.

### Hermes executor (optional heavyweight backend)

Any agent YAML may declare `executor: hermes` (see `agents/hermes.yaml`): the
task then runs on an external **Hermes Agent** via its runs API instead of
Lity's native loop, inheriting Hermes's full 70+ tool set (terminal backends,
browser, MCP, skills). Lity keeps the entire task lifecycle — board,
`task_log`, approval cards, `waiting_user` + nag, compression, skill
distillation — by mapping the contract 1:1:

```
runner.spawn(task)   → POST /v1/runs           events → GET /v1/runs/{id}/events (SSE)
approval card        → POST /v1/runs/{id}/approval     cancel → POST /v1/runs/{id}/stop
```

`lity/agents/hermes_executor.py` holds the HTTP/SSE client and a tolerant
event classifier (schemas vary across Hermes versions; the final
`GET /v1/runs/{id}` poll is authoritative). Hermes-bridged approvals carry
`approvals.run_id`; resolving one forwards the decision to the gateway — a
denial stops the run and hard-stops the task (`blocked`), same no-retry
contract as native. Config: `hermes:` block in config.yaml (`enabled`,
`base_url`, `api_key_env`); while disabled, hermes-executor agents are hidden
from the kernel entirely. Run Hermes as a sibling process (`hermes gateway`
with `API_SERVER_ENABLED=true`, own profile; disable its cron/messaging/memory
to avoid split-brain). Keep cheap agents native — a Hermes run carries a much
bigger prompt, and delegation must degrade gracefully if the gateway is down.

### The kernel is never blocked (task board + introspection + nag)

- **Task board (system slot 5):** every `queued/running/waiting_user` task plus
  anything finished in the last 15 minutes is rendered into the kernel's system
  prompt as one line each (`#14 researcher · running · compare NAS drives · 3m ago`).
  The kernel always knows every task id and state without calling anything.
- **`task_log`:** the kernel can read the tail of any task's own thread (all
  sub-agent tool calls are logged there as events), so "what is task 14 doing?"
  is answerable live, without holding the sub-agent's transcript in context.
- **`waiting_user` + nag:** when a sub-agent tool call suspends on an approval,
  the task flips to `waiting_user` (visible on the board and in the UI). If the
  user hasn't decided after `approval_nag_seconds` (default 60), a system event
  wakes the kernel in the parent thread so it can ask the user to look at the
  approval card. Approval → back to `running`; denial/expiry → `blocked` as before.

## 5a. MCP — external services without per-service code

`lity/tools/mcp.py` is a dependency-free MCP client (stdio transport,
newline-delimited JSON-RPC 2.0). Servers are declared in `config.yaml` under
`mcp.servers` (command, args, env, permission level). On boot they connect in
the background — a slow or broken server never delays startup — and each
server's tools register into the shared REGISTRY as `<server>_<tool>`.

Sub-agent YAMLs pull them in with `mcp:<server>` or `mcp:*` in their tools
list (expanded at spawn time, so late-connecting servers still appear). The
bundled **secretary** agent (`tools: ["mcp:*", …]`) is the intended home for
Gmail/Calendar/Drive/Notion-style tasks. MCP tools default to level 3, so with
`autonomy_level: 2` each one asks once and an "always" approval persists per
tool. The kernel's `capabilities` sheet lists connected servers, and
`mcp.connected` / `mcp.failed` events go to the bus.

**Self-service onboarding:** the kernel's `connect_service(name, command,
args, env)` tool (level 3) persists a server to `mcp_servers.yaml`
(gitignored — it holds OAuth secrets; config.yaml keeps its comments) and
hot-connects it, replacing any live server of the same name so retries with
fixed credentials just work. When no services are connected, `capabilities`
includes the full Google Workspace recipe (OAuth client steps for the user,
the exact connect_service call, and the one-time browser-consent flow), so
"set up my calendar" is something the agent can drive end to end — the only
human-only step is creating the OAuth client id/secret in Google Cloud Console.

## 5b. Skill-learning loop + soul adaptation

Closing the Hermes "compounding agent" gap with three mechanisms:

1. **Distill** — after every successfully completed sub-agent task, a
   background utility-model job reviews the task, the tool-call trail, and the
   result, and decides whether anything generalizes into a reusable procedure
   (`skills` table + FTS5). Smooth zero-insight tasks produce nothing.
2. **Recall & refine** — when a sub-agent starts a task, the top-matching
   skills for that agent are injected into its system prompt ("follow this
   proven procedure"). If a later task distills a similar skill, the existing
   one is *updated in place* — repetition sharpens skills instead of
   duplicating them. Usage counts are tracked; the Skills tab in settings
   shows every skill and lets you delete bad ones.
3. **Soul adaptation (LEARNED.md)** — whenever a `feedback`-kind memory is
   saved ("too verbose", "always use metric"), LEARNED.md is rewritten from
   all feedback memories (max 12 bullets) and loaded with SOUL.md into every
   kernel turn. Capped, inspectable, hand-editable.

Known limits: skills are distilled by a cheap model with no verification, so
a wrong lesson can stick until deleted; retrieval is keyword FTS (a very
differently-worded task may miss its skill); skills are procedural text, not
executable code; and LEARNED.md rewrites favour recent feedback.

## 5c. Vision

`models.vision` in config.yaml (falls back to `main`). Two paths:
- **Chat**: the newest `kernel.vision_max_images` image attachments in the
  window are embedded as base64 image parts; when any are present the turn is
  routed to the vision model automatically.
- **Agents**: an `analyze_image(path, question)` tool (level 1, direct-capable)
  lets any sub-agent look at workspace images — screenshots, uploads, plots.

## 6. Memory (parallel, like the sketch)

- **Write path (background, never blocks the reply):** after each exchange, the
  utility model extracts candidate facts typed as `user | project | feedback |
  reference`, dedupes against FTS, inserts into `memories` + `memories_fts`.
- **Read path:** (a) automatic — top-5 FTS matches for the incoming user
  message injected into system slot 5; (b) explicit — the `recall` tool.
- **MEMORY.md** is a nightly human-readable export (heartbeat job), so the
  memory store is inspectable/editable as a file, but SQLite is authoritative.

## 7. Scheduling: heartbeat + crons + timers

- `schedules` table, kinds: `timer` (one-shot), `cron` (recurring: `every:10m`,
  `daily:09:00`, `weekly:mon:09:00`), `heartbeat`.
- A single asyncio scheduler loop wakes every 30s, fires due rows by injecting
  a system message into the target thread and running the kernel on it.
- **Heartbeat** (every `heartbeat.interval_minutes`, default 30): builds a
  micro-context (HEARTBEAT.md + running tasks + due items + Home summary) and
  asks the *utility* model one question: anything need attention? `HB_OK` →
  discard, zero cost to the kernel. Otherwise the action enters Home as a
  system message. This is the OpenClaw/Hermes proactive pattern, capped hard.

## 8. Permissions & restriction levels

Five levels; `autonomy_level` in config.yaml (default 2) auto-approves at or
below; above it, execution suspends and an **approval card** appears in the UI
(SSE event). Decisions: approve once / always (persisted) / deny.

| Level | Meaning | Examples |
|---|---|---|
| 0 | Read internal state | recall, task_status |
| 1 | Read the outside world | web_search, fetch_url, browser navigation/reading |
| 2 | Write inside the sandbox | workspace files, memory, delegate, schedule |
| 3 | Execute | python_run, shell, browser actions that change remote state |
| 4 | Dangerous / outward-facing | deleting outside workspace, sending email/messages, system config |

Level 4 always requires approval regardless of `autonomy_level`. Sub-agents
additionally carry a `level_cap` so e.g. researcher can never shell out.

## 9. Storage — SQLite (WAL), single file

```
threads    (id, parent_id, kind main|sub|agent, title, created_at, archived)
messages   (id, thread_id, role, content, tool_name, tool_call_id,
            tokens, collapsed, created_at)
messages_fts (FTS5 over content — powers search_history)
summaries  (thread_id PK, content, covers_until_message_id, updated_at)
memories   (id, kind, content, source_thread_id, created_at, archived)
memories_fts (FTS5 over content)
tasks      (id, agent, thread_id, parent_thread_id, status, task, result,
            tokens_used, created_at, finished_at)
schedules  (id, kind, spec, prompt, thread_id, next_run, last_run, enabled)
approvals  (id, tool, args_json, level, task_id, status, created_at, decided_at)
```

Thread 1 is the pinned **Home** thread, created at first boot.

## 10. Gateway — FastAPI + SSE, HTML-only frontend

```
GET  /                          → web/index.html (static, no build step)
GET  /api/threads               → sidebar list (Home pinned first)
POST /api/threads               → user opens a sub-thread
GET  /api/threads/{id}/messages
POST /api/threads/{id}/messages → 202; kernel runs async; reply arrives via SSE
GET  /api/events                → global SSE: message.created, task.updated,
                                  thread.created, approval.requested
POST /api/approvals/{id}        → {decision: approve|always|deny}
GET  /api/tasks · /api/memories · /api/schedules
```

`web/index.html`: vanilla JS + `EventSource`. Sidebar = threads + running-task
chips; main pane = chat; approval cards render inline with buttons. Sub-threads
appear automatically when the agent delegates (thread.created event). The
gateway is deliberately thin so Telegram/Discord channels can be added later as
alternative front doors to the same kernel.

### Settings dashboard (⚙ in the header)
Four tabs, all live-editable from the browser:
- **Config** — the raw config.yaml (models, budgets, autonomy level,
  heartbeat…); saving validates the YAML and hot-reloads it (server host/port
  need a restart).
- **Identity files** — SOUL.md, USER.md, HEARTBEAT.md, AGENTS.md, MEMORY.md.
- **Sub-agents** — each agent's YAML (model, tools, budgets, level_cap) and
  system prompt; saving reloads the registry instantly, and renaming in the
  YAML creates a new agent.
- **Tools** — read-only registry view: every tool, its permission level, and
  whether it may output direct-to-user.
Endpoints: `GET /api/settings`, `PUT /api/settings/config`,
`PUT /api/settings/file`, `PUT /api/settings/agent`.

## 11. Model routing (config.yaml)

```yaml
provider:
  base_url: https://openrouter.ai/api/v1     # later: http://localhost:11434/v1
  api_key_env: OPENROUTER_API_KEY
models:
  main:    anthropic/claude-haiku-4.5        # kernel — small & cheap by design
  utility: google/gemini-2.5-flash-lite      # summaries, memory, heartbeat — cheapest
  default_agent: anthropic/claude-sonnet-4.5 # sub-agents; per-agent override in YAML
```

Three cost tiers, one client. The kernel's starved context is what makes the
`main` slot swappable for a local model with no architectural change.

## 12. Raspberry Pi / plug-and-play

- Pure-Python deps (fastapi, uvicorn, aiosqlite, httpx, pyyaml) — all have
  aarch64 wheels. Playwright installs arm64 Chromium on Pi OS 64-bit.
- No vector DB, no embeddings, no Redis, no build step: FTS5 does recall.
- `install.sh`: venv → pip → `playwright install chromium` → optional systemd
  unit (`lity.service`) → prompts for the OpenRouter key → done.

## 13. Build phases

1. **Walking skeleton** — config, DB, gateway, HTML UI, kernel loop with
   OpenRouter, `delegate` + coder & researcher agents end-to-end. *(this scaffold)*
2. **Discipline** — compactor, tool-pair collapsing, memory pipeline, approvals UI.
3. **Proactive** — scheduler loop, heartbeat, timers/crons, MEMORY.md export.
4. **Reach** — browser agent hardening, shell/writer agents, install.sh + systemd, Pi test.
5. **Local-model experiment** — point `main` at Ollama, measure, tune prompts.
