# Lity — Lightweight Personal Agent Architecture

A personal AI agent rebuilt around one governing principle:

> **The main thread is sacred.** It stays tiny, cheap, and clean at all times — small
> enough that it can eventually run on a small local model on a Raspberry Pi.
> Everything heavy runs on an external **Hermes Agent** that burns its own
> tokens in its own context and returns only a compressed result.

Division of labor: **Lity is the front desk** (conversation, memory,
personality, scheduling, approvals, task lifecycle); **Hermes is the
workshop** (terminal, files, coding, browser, web research, email/calendar,
MCP services — its full 70+ tool set). There are no native sub-agents:
`delegate(task)` always creates a Hermes run.

---

## 1. The two-tier model

```
┌─────────────────────────────────────────────────────────────────┐
│  KERNEL (main thread)                    ~1.6k token system     │
│  cheap/small model, later local          ~6–8k token window     │
│                                                                 │
│  System prompt slots (fixed budget):                            │
│    1. SOUL.md + LEARNED.md   personality, tone, adaptation      │
│    2. USER.md                who the user is                    │
│    3. Delegation policy      Hermes does ALL real work          │
│    4. Task board             open tasks: id·state·age           │
│    5. Injected memories      top-5 FTS hits, per turn           │
│    6. Thread summary         rolling, updatable                 │
│                                                                 │
│  Window: last 20 messages verbatim; older → summary;            │
│  resolved tool call/result pairs collapsed to one line.         │
└───────────────┬─────────────────────────────────────────────────┘
                │ delegate(task)                 compressed result
                ▼                                (≤ 300 tokens) ▲
┌─────────────────────────────────────────────────────────────────┐
│  HERMES AGENT (external process / machine — runs API)           │
│  POST /v1/runs · SSE events · approval bridge · stop            │
│  full toolbelt: terminal, files, code, browser, web, MCP        │
└─────────────────────────────────────────────────────────────────┘
```

The kernel NEVER: writes long code, reads whole web pages, drives a browser,
or holds a task transcript. It converses, remembers, schedules, and
delegates. A task's work lives in its own DB thread (visible in the UI as a
sub-thread) mirroring the Hermes run's progress events; only the final
compressed result enters the kernel's context.

## 2. Directory layout

```
Lity_light/
├── config.yaml            # ALL runtime configuration (YAML, single file)
├── .env                   # provider API key + HERMES_API_KEY (never in git)
├── requirements.txt       # pure-Python: fastapi, uvicorn, aiosqlite, httpx, pyyaml
├── install.sh             # plug-and-play setup (venv, deps, systemd)
├── workspace/             # the agent's "home" — human-editable markdown
│   ├── SOUL.md            # identity, voice, behavioral defaults (system slot 1)
│   ├── LEARNED.md         # adaptation layer rewritten from feedback memories
│   ├── USER.md            # user profile, kept current by the agent
│   ├── MEMORY.md          # human-readable export of memory DB (regenerated)
│   ├── HEARTBEAT.md       # conditions the heartbeat evaluates each tick
│   └── AGENTS.md          # standing rules sent with every Hermes task
├── lity/                  # the Python package
│   ├── __main__.py        # python -m lity → starts everything
│   ├── config.py          # YAML → typed config
│   ├── db.py              # SQLite schema + async helpers (WAL mode)
│   ├── llm.py             # OpenAI-compatible client (OpenRouter now, Ollama later)
│   ├── kernel.py          # main-thread agent loop
│   ├── context.py         # builds the budgeted kernel context
│   ├── compactor.py       # summarization + tool-pair collapsing
│   ├── memory.py          # parallel extraction + FTS5 recall
│   ├── skills.py          # lesson distillation + LEARNED.md soul adaptation
│   ├── approvals.py       # permission levels, approval queue, Hermes bridge
│   ├── tools/             # the kernel's small tool registry (core.py, web.py)
│   ├── agents/            # runner.py (task lifecycle) + hermes_executor.py (HTTP/SSE)
│   ├── sched/             # scheduler.py + crons.py (timers, crons, heartbeat)
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
   fired at 14:32".
4. **Tool-pair collapsing.** Once a tool call is resolved, the call+result pair
   is collapsed in context to a one-line event ("delegated task #12 → done:
   <300-token result>"). Full records stay in SQLite for the UI/audit.
5. **Result compression.** A Hermes run's final answer over
   `hermes.result_max_chars` is compressed by the utility model before it
   touches the parent thread.
6. **Memories are retrieved, not resident.** Nothing accumulates in the system
   prompt; the top-5 FTS matches for the *current* user message are injected
   per turn and disappear next turn.

Because the kernel only ever sees: tiny system prompt + summary + 20 messages +
a dozen short tool signatures, a 7–8B local model is a realistic future
drop-in — the `llm.py` provider is OpenAI-compatible, so pointing
`provider.base_url` at Ollama/llama.cpp is a config change, not a code change.

## 4. Kernel tool set (small, fixed — one-liners in the prompt)

| Tool | Level | Purpose |
|---|---|---|
| `recall(query)` | 0 | Search memory (FTS5) |
| `remember(content, kind)` | 2 | Save a durable fact (user/project/feedback/reference) |
| `delegate(task, context)` | 2 | Start a Hermes run in its own sub-thread; returns task id |
| `task_status(task_id)` | 0 | Check / fetch result of a task |
| `task_log(task_id, last_n)` | 0 | Peek inside a task's thread — recent Hermes actions |
| `search_history(query)` | 0 | FTS5 over every past message in every thread |
| `cancel_task(task_id)` | 2 | Stop a running task (stops the Hermes run) |
| `schedule(spec, prompt)` | 2 | Timer or cron ("in:10m", "daily:09:00", "every:12s") |
| `list_schedules()` | 0 | Inspect timers/crons |
| `quick_search(query)` | 1 | One-shot web search, top-3 snippets only (cheap) |
| `update_user_profile(fact)` | 2 | Maintain USER.md |
| `send_file(path, caption)` | 0 | Post a workspace file into the chat |
| `capabilities()` | 0 | Live self-inspection sheet (Hermes status, tools, schedules, skills) |

Anything beyond this (run code, browse, shell, research, documents, email,
calendar, connecting new services) is *by construction* a delegation — the
kernel has no tool for it, and the delegation policy orders it to route such
work to Hermes automatically, without the user ever naming an executor.
`capabilities` exists so the kernel introspects its real registry instead of
inventing false limitations.

### Direct-to-user tool output
Read-only tools marked `direct` (recall, quick_search, task_status, task_log,
search_history, list_schedules) expose an extra `direct_to_user: bool`
parameter. When the model sets it true, the tool's raw output is posted
straight to the user as the reply and **the turn ends — no second model pass**.
(`capabilities` is deliberately NOT direct-capable: it is an internal sheet
the kernel must paraphrase.)

### Files in and out
`send_file(path, caption)` posts any workspace file into the chat as an
attachment — images render inline, everything else is a download link. Users
send files via `POST /api/threads/{id}/files` (multipart) — saved under
`workspace/uploads/`, shown in chat, and announced to the kernel with the
workspace path. `GET /api/files/{path}` serves workspace files (jailed).
Note: Hermes runs in its own workspace — pass file *content* (or a shared
path, if Lity and Hermes share a filesystem) inside the task text.

### Vision
`models.vision` in config.yaml (falls back to `main`): the newest
`kernel.vision_max_images` image attachments in the window are embedded as
base64 image parts; when any are present the turn is routed to the vision
model automatically.

## 5. The Hermes executor (the only executor)

Every `delegate(task)` becomes a run on an external **Hermes Agent**
(`hermes gateway` with `API_SERVER_ENABLED=true`, bearer auth). Lity keeps
the entire task lifecycle — board, `task_log`, approval cards,
`waiting_user` + nag, compression, skill distillation — by mapping the
contract 1:1:

```
runner.spawn(task)   → POST /v1/runs           events → GET /v1/runs/{id}/events (SSE)
approval card        → POST /v1/runs/{id}/approval     cancel → POST /v1/runs/{id}/stop
status/result        → GET  /v1/runs/{id}      (authoritative final poll)
```

- `lity/agents/hermes_executor.py` holds the HTTP/SSE client and a tolerant
  event classifier (event schemas vary across Hermes versions; the final
  `GET /v1/runs/{id}` poll is authoritative for status and output).
- `workspace/AGENTS.md` is sent as run instructions with every task.
- Progress events are mirrored as one-liners into the task's sub-thread, so
  `task_log` and the UI work exactly as before.
- **Approval bridge:** a Hermes run pausing on a human decision surfaces as a
  normal Lity approval card (`approvals.run_id` set). Approving forwards the
  decision to the gateway; a denial or expiry stops the run and hard-stops
  the task (`blocked`) — the no-retry contract: the user's word (or silence)
  is final.
- Config: `hermes:` block in config.yaml (`enabled`, `base_url`,
  `api_key_env`, `result_max_chars`). Same-host Hermes:
  `http://127.0.0.1:8642`; another machine: its LAN IP. If the gateway is
  down or disabled, `delegate` fails with a clear message and Lity can still
  chat, remember, search history and schedule.

Run Hermes as a sibling process with its own profile; disable its
cron/messaging/memory to avoid split-brain — Lity owns the user
relationship, memory and scheduling.

### The kernel is never blocked (task board + introspection + nag)

- **Task board (system slot):** every `queued/running/waiting_user` task plus
  anything finished in the last 15 minutes is rendered into the kernel's
  system prompt as one line each (`#14 hermes · running · compare NAS drives
  · 3m ago`).
- **`task_log`:** the kernel can read the tail of any task's own thread, so
  "what is task 14 doing?" is answerable live.
- **`waiting_user` + nag:** when a run suspends on an approval, the task flips
  to `waiting_user`. If the user hasn't decided after `approval_nag_seconds`
  (default 60), a system event wakes the kernel so it can chase the user.
  Approval → back to `running`; denial/expiry → `blocked`.

## 5a. Skill-learning loop + soul adaptation

1. **Distill** — after every completed task, a background utility-model job
   reviews the task and result and decides whether anything generalizes into
   a reusable lesson (`skills` table + FTS5). Smooth zero-insight tasks
   produce nothing. The Skills tab in settings shows every lesson and lets
   you delete bad ones.
2. **Soul adaptation (LEARNED.md)** — whenever a `feedback`-kind memory is
   saved ("too verbose", "always use metric"), LEARNED.md is rewritten from
   all feedback memories (max 12 bullets) and loaded with SOUL.md into every
   kernel turn. Capped, inspectable, hand-editable.

Known limits: lessons are distilled by a cheap model with no verification;
retrieval is keyword FTS; skills are text, not executable procedures.

## 6. Memory (parallel)

- **Write path (background, never blocks the reply):** after each exchange, the
  utility model extracts candidate facts typed as `user | project | feedback |
  reference`, dedupes against FTS, inserts into `memories` + `memories_fts`.
- **Read path:** (a) automatic — top-5 FTS matches for the incoming user
  message injected per turn; (b) explicit — the `recall` tool.
- **MEMORY.md** is a human-readable export, so the memory store is
  inspectable/editable as a file, but SQLite is authoritative.

## 7. Scheduling: heartbeat + crons + timers

- `schedules` table, kinds: `timer` (one-shot `in:45s/10m/2h`), `cron`
  (recurring: `every:12s` — min 5s — `every:30m`, `daily:09:00`,
  `weekly:mon:09:00`, all UTC).
- A single asyncio scheduler loop sleeps until the nearest due job (min 0.5s,
  capped at `scheduler.tick_seconds` when idle), fires due rows by injecting
  a system message into the target thread and running the kernel on it.
- **Heartbeat** (every `heartbeat.interval_minutes`, default 30): builds a
  micro-context (HEARTBEAT.md + running tasks + due items) and asks the
  *utility* model one question: anything need attention? `HB_OK` → discard,
  zero cost to the kernel. The last 5 heartbeat reports are fed back as
  ALREADY REPORTED to prevent repeats.

## 8. Permissions & restriction levels

Five levels; `autonomy_level` in config.yaml (default 2) auto-approves at or
below; above it, execution suspends and an **approval card** appears in the UI
(SSE event). Decisions: approve once / always (persisted) / deny.

| Level | Meaning | Examples |
|---|---|---|
| 0 | Read internal state | recall, task_status, capabilities |
| 1 | Read the outside world | quick_search |
| 2 | Write inside the sandbox | memory, delegate, schedule, USER.md |
| 3 | Execute | bridged Hermes approvals default here |
| 4 | Dangerous / outward-facing | always asks regardless of autonomy_level |

Hermes-side tool calls are governed by Hermes's own approval flow, which is
bridged onto the same Lity cards at level 3. A denied OR timed-out approval
hard-stops the task — enforced in the runner, not just prompts.

## 9. Storage — SQLite (WAL), single file

```
threads    (id, parent_id, kind main|sub|agent, title, created_at, archived)
messages   (id, thread_id, role, content, tool_name, tool_call_id,
            tokens, collapsed, attachment, created_at)
messages_fts (FTS5 over content — powers search_history)
summaries  (thread_id PK, content, covers_until_message_id, updated_at)
memories   (id, kind, content, source_thread_id, created_at, archived)
memories_fts (FTS5 over content)
skills     (id, agent, name, description, content, uses, archived, ...)
tasks      (id, agent='hermes', thread_id, parent_thread_id, status, task,
            result, tokens_used, created_at, finished_at)
schedules  (id, kind, spec, prompt, thread_id, next_run, last_run, enabled)
approvals  (id, tool, args_json, level, task_id, thread_id, run_id,
            status, created_at, decided_at)
```

Thread 1 is the pinned **Home** thread, created at first boot. On boot,
orphaned running tasks are failed and stale pending approvals expired.

## 10. Gateway — FastAPI + SSE, HTML-only frontend

```
GET  /                          → web/index.html (static, no build step)
GET  /api/threads               → sidebar list (Home pinned first)
POST /api/threads               → user opens a sub-thread
GET  /api/threads/{id}/messages
POST /api/threads/{id}/messages → 202; kernel runs async; reply arrives via SSE
POST /api/threads/{id}/files    → multipart upload into workspace/uploads/
GET  /api/files/{path}          → serve workspace files (jailed)
GET  /api/events                → global SSE: message.created, task.updated,
                                  thread.created, approval.requested/resolved
POST /api/approvals/{id}        → {decision: approve|always|deny}
GET  /api/tasks · /api/skills · /api/memories · /api/schedules
```

`web/index.html`: vanilla JS + `EventSource`. Sidebar = threads + running-task
chips; main pane = chat; approval cards render inline with buttons. Task
sub-threads appear automatically on delegation (thread.created event). The
gateway is deliberately thin so Telegram/Discord channels can be added later
as alternative front doors to the same kernel.

### Settings dashboard (⚙ in the header)
Four tabs, all live-editable from the browser:
- **Config** — the raw config.yaml; saving validates the YAML and hot-reloads
  it (server host/port need a restart).
- **Identity files** — SOUL.md, LEARNED.md, USER.md, HEARTBEAT.md, AGENTS.md,
  MEMORY.md.
- **Skills** — every distilled lesson, with delete.
- **Tools** — read-only registry view: every kernel tool, its permission
  level, and whether it may output direct-to-user.
Endpoints: `GET /api/settings`, `PUT /api/settings/config`,
`PUT /api/settings/file`.

## 10a. Voice front door — OpenAI-compatible API

Any STT→LLM→TTS pipeline can use Lity as its "LLM":

```
GET  /v1/models                → [{"id": "lity"}]
POST /v1/chat/completions      → standard OpenAI body; stream: true gives
                                 chat.completion.chunk SSE (sentence chunks)
GET  /v1/voice/pending         → unheard assistant messages (poll & speak)
```

- Lity is **stateful**: only the last user message of the request is used;
  the Home thread (id 1), summary and memory are the real history. Voice and
  dashboard share the same conversation.
- Replies pass a **TTS sanitizer** (`lity/voice.py`): markdown, tables,
  emojis, links and code are stripped to plain speakable sentences; the
  kernel prompt also demands speakable style at the source.
- **Piggyback + polling**: assistant messages produced while no voice
  request was in flight (approval announcements, finished-task reports) are
  prepended to the next reply, or fetched proactively via
  `/v1/voice/pending` (cursor-tracked, never re-spoken).
- Auth: set `LITY_API_KEY` in .env to require a bearer key on `/v1/*`.

### Voice approval dialogue (deterministic by construction)

Verified against Hermes `api_server.py`: the approval event is
`approval.request` carrying `choices: ["once","session","always","deny"]`
(+ a credential-redacted `command`), and resolution is
`POST /v1/runs/{run_id}/approval {"choice": <one of choices>}`.

1. A Hermes approval bridges in → Lity posts the **fixed announcement**
   (never LLM-generated): *"Your task '<title>' needs an approval. Do you
   want more details about it right now and approve it?"*
2. Free-form replies go through the kernel LLM, which can explain via
   `task_log`. When the user is ready it calls
   `offer_approval_options(approval_id)` — a level-0 tool that emits the
   **fixed options question** built from the run's own choices ("Would you
   like to once, session, always or deny for it?") and arms a 1:1 matcher
   on the thread.
3. Next user message: an exact option match (case/punctuation-insensitive,
   plus Hermes's own approve→once aliases) executes the decision
   **bypassing the LLM entirely** and answers with a fixed confirmation.
   Anything else disarms the matcher and returns to the LLM, which may
   re-offer. The LLM can never approve, deny, or invent option words — the
   deterministic matcher is the only path to a decision.

## 11. Model routing (config.yaml)

```yaml
provider:
  base_url: https://openrouter.ai/api/v1     # later: http://localhost:11434/v1
  api_key_env: OPENROUTER_API_KEY
models:
  main:    anthropic/claude-haiku-4.5        # kernel — small & cheap by design
  utility: google/gemini-2.5-flash-lite      # summaries, memory, heartbeat — cheapest
  vision:  anthropic/claude-haiku-4.5        # image turns
```

Hermes's models are configured on the Hermes side, independently. The
kernel's starved context is what makes the `main` slot swappable for a local
model with no architectural change.

## 12. Raspberry Pi / plug-and-play

- Pure-Python deps (fastapi, uvicorn, aiosqlite, httpx, pyyaml) — all have
  aarch64 wheels. No browser, no Playwright, no MCP subprocesses: the heavy
  runtime lives entirely in Hermes.
- No vector DB, no embeddings, no Redis, no build step: FTS5 does recall.
- `install.sh`: venv → pip → optional systemd unit (`lity.service`) →
  prompts for the OpenRouter key → done. Run Hermes on the same Pi (or any
  reachable machine) and point `hermes.base_url` at it.
