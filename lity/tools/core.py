"""The kernel's small, fixed tool set. Anything heavier than these is,
by construction, a delegation."""

from . import params, tool

KERNEL_TOOLS = [
    "recall", "remember", "delegate", "task_status", "task_log", "cancel_task",
    "schedule", "list_schedules", "quick_search", "search_history",
    "update_user_profile", "send_file", "capabilities", "connect_service",
]


@tool("recall", "Search long-term memory. Use before saying you don't know something about the user or past work.",
      params({"query": {"type": "string", "description": "keywords to search for"}}), level=0, direct=True)
async def recall(ctx, args):
    hits = await ctx.app.memory.recall(args.get("query", ""), k=8)
    if not hits:
        return "No matching memories."
    return "\n".join(f"- [{h['kind']}] {h['content']}" for h in hits)


@tool("remember", "Save a durable fact to long-term memory.",
      params({"content": {"type": "string"},
              "kind": {"type": "string", "enum": ["user", "project", "feedback", "reference"]}},
             required=["content"]), level=2)
async def remember(ctx, args):
    mid = await ctx.app.memory.save(args["content"], args.get("kind", "project"), ctx.thread_id)
    return f"Saved (memory #{mid})."


@tool("delegate",
      "Hand a task to a specialist sub-agent. It runs in parallel in its own thread; "
      "you'll get a system event with the result. Available agents are listed in your system prompt.",
      params({"agent": {"type": "string", "description": "agent name from the registry"},
              "task": {"type": "string", "description": "complete, self-contained task description"},
              "context": {"type": "string", "description": "optional extra context the agent needs"}},
             required=["agent", "task"]), level=2)
async def delegate(ctx, args):
    try:
        task_id, thread_id = await ctx.app.runner.spawn(
            args["agent"], args["task"], ctx.thread_id, args.get("context", ""))
    except KeyError:
        names = ", ".join(ctx.app.agents.names())
        return f"Unknown agent '{args['agent']}'. Available: {names}"
    return f"Task #{task_id} started ({args['agent']}, sub-thread {thread_id}). You'll be notified when it finishes."


@tool("task_status", "Check the status/result of a delegated task.",
      params({"task_id": {"type": "integer"}}), level=0, direct=True)
async def task_status(ctx, args):
    row = await ctx.app.db.fetchone("SELECT * FROM tasks WHERE id=?", (args["task_id"],))
    if not row:
        return "No such task."
    out = f"Task #{row['id']} ({row['agent']}): {row['status']}"
    if row["result"]:
        out += f"\nResult: {row['result']}"
    return out


@tool("task_log",
      "Look inside a delegated task's own thread: the sub-agent's recent actions and messages. "
      "Use when the user asks what a task is doing, why it's stuck, or wants progress details.",
      params({"task_id": {"type": "integer"},
              "last_n": {"type": "integer", "description": "recent entries to show (default 10)"}},
             required=["task_id"]), level=0, direct=True)
async def task_log(ctx, args):
    row = await ctx.app.db.fetchone("SELECT * FROM tasks WHERE id=?", (args["task_id"],))
    if not row:
        return "No such task."
    n = min(int(args.get("last_n") or 10), 30)
    msgs = await ctx.app.db.recent_messages(row["thread_id"], n)
    head = f"Task #{row['id']} ({row['agent']}) — {row['status']}\nTask: {row['task'][:200]}"
    if row["result"]:
        head += f"\nResult: {row['result'][:400]}"
    lines = [f"[{m['role']}] {m['content'][:200]}" for m in msgs]
    return head + "\n\nRecent activity:\n" + ("\n".join(lines) if lines else "(no activity yet)")


@tool("search_history",
      "Full-text search over ALL past conversation messages, every thread. "
      "Use for 'what did we discuss...' / finding earlier decisions or details beyond memory.",
      params({"query": {"type": "string", "description": "keywords to search for"},
              "limit": {"type": "integer", "description": "max hits (default 6)"}},
             required=["query"]), level=0, direct=True)
async def search_history(ctx, args):
    from ..memory import _fts_query
    q = _fts_query(args.get("query", ""))
    if not q:
        return "Nothing to search for."
    rows = await ctx.app.db.fetchall(
        """SELECT m.thread_id, m.role, m.content, m.created_at, t.title
           FROM messages_fts f
           JOIN messages m ON m.id = f.rowid
           JOIN threads t ON t.id = m.thread_id
           WHERE messages_fts MATCH ? AND m.role IN ('user','assistant')
           ORDER BY rank LIMIT ?""",
        (q, min(int(args.get("limit") or 6), 15)))
    if not rows:
        return "No matches in conversation history."
    return "\n".join(
        f"[{r['created_at']}] ({r['title'][:40]}, thread {r['thread_id']}) "
        f"{r['role']}: {r['content'][:160]}" for r in rows)


@tool("cancel_task", "Cancel a running delegated task.",
      params({"task_id": {"type": "integer"}}), level=2)
async def cancel_task(ctx, args):
    ok = await ctx.app.runner.cancel(args["task_id"])
    return "Cancelled." if ok else "Task not running (already finished or unknown)."


@tool("schedule",
      "Set a timer or recurring job. spec formats: 'in:45s' / 'in:10m' / 'in:2h' (one-shot timer), "
      "'every:12s' (seconds OK, min 5s) / 'every:30m', 'daily:09:00', 'weekly:mon:09:00' (UTC). "
      "The prompt is executed in this thread when due.",
      params({"spec": {"type": "string"}, "prompt": {"type": "string"}}), level=2)
async def schedule(ctx, args):
    from ..sched.crons import next_run, spec_kind
    try:
        kind = spec_kind(args["spec"])
        nxt = next_run(args["spec"])
    except ValueError as e:
        return f"Bad spec: {e}"
    sid = await ctx.app.db.execute(
        "INSERT INTO schedules(kind, spec, prompt, thread_id, next_run) VALUES (?,?,?,?,?)",
        (kind, args["spec"], args["prompt"], ctx.thread_id, nxt))
    return f"Scheduled #{sid} ({args['spec']}), next run {nxt} UTC."


@tool("list_schedules", "List active timers and recurring jobs.", params({}, required=[]),
      level=0, direct=True)
async def list_schedules(ctx, args):
    rows = await ctx.app.db.fetchall("SELECT * FROM schedules WHERE enabled=1 ORDER BY next_run")
    if not rows:
        return "No active schedules."
    return "\n".join(f"#{r['id']} [{r['kind']}] {r['spec']} → next {r['next_run']}: {r['prompt'][:80]}"
                     for r in rows)


@tool("quick_search",
      "One-shot web search returning top-3 snippets. For anything deeper, delegate to the researcher.",
      params({"query": {"type": "string"}}), level=1, direct=True)
async def quick_search(ctx, args):
    from .web import ddg_search
    results = await ddg_search(args["query"], limit=3)
    if not results:
        return "No results."
    return "\n\n".join(f"{r['title']}\n{r['url']}\n{r['snippet']}" for r in results)


@tool("update_user_profile", "Append a fact to USER.md (the always-loaded user profile). Keep it to one short line.",
      params({"fact": {"type": "string"}}), level=2)
async def update_user_profile(ctx, args):
    path = ctx.app.cfg.workspace / "USER.md"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- {args['fact'].strip()}\n")
    return "USER.md updated."


@tool("capabilities",
      "Your live capability sheet: connected external services + setup recipes to connect new "
      "ones (calendar/email), kernel tools, sub-agents, schedule formats, learned skills. "
      "Check this BEFORE telling the user something is impossible or unavailable. "
      "INTERNAL reference — read it, then answer the user in your own words.",
      params({}, required=[]), level=0)
async def capabilities(ctx, args):
    from . import REGISTRY
    from .mcp import MCP_TOOLS
    # external services FIRST: when the user asks about calendar/email/etc.,
    # the connection status and setup recipe are what must never be overlooked
    lines = ["# What I can do", ""]
    if MCP_TOOLS:
        lines.append("## Connected external services (MCP — usable by sub-agents)")
        for srv, tnames in MCP_TOOLS.items():
            short = ", ".join(t.removeprefix(f"{srv}_") for t in tnames)
            lines.append(f"- {srv}: {short[:250]}")
    else:
        lines.append(
            "## External services (calendar/email/drive): NONE connected — SET ONE UP NOW\n"
            "You CAN connect them yourself with connect_service — do NOT refuse, and do NOT "
            "delegate to the secretary before connecting. Google recipe (Calendar + Gmail + "
            "Drive via the 'workspace-mcp' server):\n"
            "1. Send the user these steps (the ONLY part they must do themselves): "
            "console.cloud.google.com → create/pick a project → 'APIs & Services' → enable "
            "the Google Calendar API and Gmail API → 'Credentials' → 'Create credentials' → "
            "'OAuth client ID' → application type 'Desktop app' → copy the Client ID and "
            "Client Secret, and paste both here in chat.\n"
            "2. When they paste the credentials, IMMEDIATELY call: connect_service("
            "name='google', command='uvx', args=['workspace-mcp'], "
            "env={'GOOGLE_OAUTH_CLIENT_ID': '<id>', 'GOOGLE_OAUTH_CLIENT_SECRET': '<secret>'}). "
            "If uvx is missing, delegate installing uv (astral.sh/uv) to the shell agent first.\n"
            "3. Delegate the original task to the secretary; the first Google tool call opens "
            "a one-time browser consent (if the task reports an auth URL instead, send that "
            "URL to the user, then re-run the task).")
    lines.append("\n## Kernel tools (I use these directly)")
    for n in KERNEL_TOOLS:
        t = REGISTRY.get(n)
        if t:
            lines.append(f"- {t.name}: {t.description}")
    lines.append("\n## Sub-agents (delegate — they run in parallel and report back)")
    for a in ctx.app.agents.all():
        lines.append(f"- {a.name}: {a.description}\n  tools: {', '.join(a.tools)}")
    from ..sched.crons import MIN_EVERY_SECONDS
    lines.append(
        "\n## Scheduling\n"
        f"- one-shot: in:45s / in:10m / in:2h · recurring: every:12s (min {MIN_EVERY_SECONDS}s) / "
        "every:30m / daily:09:00 / weekly:mon:09:00 (UTC)\n"
        "- fires with roughly second-level precision; the prompt runs in the scheduling thread")
    n = await ctx.app.db.fetchone("SELECT COUNT(*) AS c FROM skills WHERE archived=0")
    top = await ctx.app.db.fetchall(
        "SELECT agent, name, uses FROM skills WHERE archived=0 ORDER BY uses DESC LIMIT 5")
    lines.append(f"\n## Learned skills: {n['c']} stored, auto-injected when relevant" +
                 ("".join(f"\n- [{s['agent']}] {s['name']} (used {s['uses']}×)" for s in top)))
    return "\n".join(lines)


@tool("connect_service",
      "Connect an external service (MCP server) at runtime — e.g. Google Calendar/Gmail. "
      "Call `capabilities` FIRST: it has the exact recipe and the OAuth steps to walk the user through.",
      params({"name": {"type": "string", "description": "short service name, e.g. 'google'"},
              "command": {"type": "string", "description": "executable, e.g. 'uvx' or 'npx'"},
              "args": {"type": "array", "items": {"type": "string"}},
              "env": {"type": "object", "description": "env vars, e.g. OAuth client id/secret",
                      "additionalProperties": {"type": "string"}}},
             required=["name", "command"]), level=3)
async def connect_service(ctx, args):
    spec = {"command": args["command"], "args": args.get("args") or [],
            "env": args.get("env") or {}}
    try:
        tools = await ctx.app.mcp.add_server(args["name"].strip(), spec)
    except Exception as e:
        return (f"Could not connect '{args['name']}': {e}\n"
                "Check the command exists (delegate to the shell agent to install it), "
                "the args, and the credentials — then call connect_service again to retry.")
    short = ", ".join(t.removeprefix(args["name"].strip() + "_") for t in tools)
    return (f"Connected '{args['name']}' — {len(tools)} tools now available to sub-agents "
            f"(secretary has mcp:*): {short[:500]}. Note: the first call to a Google tool "
            "triggers a one-time browser consent; if a task reports an auth URL, pass it to the user.")


@tool("send_file",
      "Send a workspace file (image, document, data, screenshot...) to the user. "
      "It appears in the chat as a downloadable attachment (images render inline).",
      params({"path": {"type": "string", "description": "path relative to the workspace"},
              "caption": {"type": "string", "description": "optional short caption"}},
             required=["path"]), level=0)
async def send_file(ctx, args):
    import json
    import mimetypes
    ws = ctx.app.cfg.workspace
    p = (ws / args["path"]).resolve()
    if not p.is_relative_to(ws):
        return "Error: path escapes the workspace."
    if not p.is_file():
        return f"Error: no such file '{args['path']}'."
    rel = p.relative_to(ws).as_posix()
    att = json.dumps({"path": rel, "name": p.name,
                      "mime": mimetypes.guess_type(p.name)[0] or "application/octet-stream"})
    caption = (args.get("caption") or "").strip() or f"📎 {p.name}"
    tid = ctx.user_thread_id
    await ctx.app.db.add_message(tid, "assistant", caption, attachment=att)
    ctx.app.bus.emit("message.created", thread_id=tid, role="assistant",
                     content=caption, attachment=json.loads(att))
    return f"File '{rel}' delivered to the user."
