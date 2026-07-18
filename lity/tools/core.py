"""The kernel's small, fixed tool set. Anything heavier than these is,
by construction, a delegation."""

from . import params, tool

KERNEL_TOOLS = [
    "recall", "remember", "delegate", "continue_task", "task_status", "task_log", "cancel_task",
    "fresh_start",
    "schedule", "list_schedules", "quick_search", "search_history",
    "update_user_profile", "send_file", "capabilities", "offer_approval_options",
    "timer", "note", "shopping", "weather", "volume", "calendar", "speech_mode",
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
      "Hand a task to Hermes, your executor: a full agent with terminal, browser, files, "
      "coding, web research, email/calendar and more. ANY real-world work beyond your own "
      "small tools goes here — never refuse or attempt it yourself. Runs in parallel in its "
      "own thread; a system event delivers the result.",
      params({"task": {"type": "string", "description": "complete, self-contained task description"},
              "context": {"type": "string", "description": "optional extra context Hermes needs"}},
             required=["task"]), level=2)
async def delegate(ctx, args):
    if not ctx.app.runner.hermes.enabled:
        return ("Hermes executor is not configured (hermes.enabled=false in config.yaml). "
                "Tell the user to point hermes.base_url at their Hermes gateway and enable it.")
    task_id, thread_id = await ctx.app.runner.spawn(
        args["task"], ctx.thread_id, args.get("context", ""))
    return f"Task #{task_id} started (hermes, sub-thread {thread_id}). You'll be notified when it finishes."


@tool("continue_task",
      "Send a follow-up into a FINISHED task's own thread and Hermes session. Use this — "
      "not delegate — whenever the user answers a question a task asked, supplies missing "
      "info, or wants more work on the same job: Hermes still has that task's full context. "
      "Only start a fresh delegate for genuinely unrelated work.",
      params({"task_id": {"type": "integer", "description": "the finished task to continue"},
              "message": {"type": "string",
                          "description": "the user's answer / follow-up, self-contained"}},
             required=["task_id", "message"]), level=2)
async def continue_task(ctx, args):
    ok, msg = await ctx.app.runner.resume(args["task_id"], args["message"])
    if ok:
        msg += " You'll be notified when it finishes."
    return msg


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


@tool("speech_mode",
      "Whether replies to TYPED (web UI) messages are ALSO spoken by the voice assistant. "
      "Default ui_only: typed input gets a silent on-screen reply (voice input is always "
      "spoken; task results and schedules are always announced). Actions: status, "
      "speak_all (read typed replies aloud too), ui_only (back to default). Use when the "
      "user says things like 'stop reading my typed messages' or 'speak everything'.",
      params({"action": {"type": "string", "enum": ["status", "speak_all", "ui_only"]}},
             required=["action"]), level=1, direct=True)
async def speech_mode(ctx, args):
    a = str(args.get("action") or "status").lower()
    if a in ("speak_all", "ui_only"):
        from ..setup import config_set
        config_set("voice.speak_text_replies", a == "speak_all")
    on = ctx.app.voice.speak_text_replies
    return ("Speech mode: replies to typed messages are "
            + ("spoken aloud as well." if on else "shown in the UI only, not spoken.")
            + " Voice input is always answered aloud; task results are always announced.")


@tool("fresh_start",
      "Clear THIS conversation's working context: every past message leaves your context "
      "window and the rolling summary is deleted, so your next turn starts from a clean "
      "slate. NOTHING is lost — full history stays in the database (search_history still "
      "finds it) and long-term memory is untouched. Use when the user says start fresh / "
      "clear the conversation / clean slate / forget this conversation.",
      params({}, required=[]), level=2)
async def fresh_start(ctx, args):
    n = await ctx.app.db.reset_context(ctx.thread_id)
    ctx.app.bus.emit("thread.compacted", thread_id=ctx.thread_id)
    return (f"Done — context cleared ({n} messages folded away, summary dropped). "
            "History remains searchable and memories are kept. Tell the user briefly; "
            "from the next turn you start clean.")


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
      "One-shot web search returning top-3 snippets. For anything deeper, delegate to Hermes.",
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
      "Your live capability sheet: the Hermes executor's status and what it can do, your own "
      "kernel tools, schedule formats, learned skills. Check this BEFORE telling the user "
      "something is impossible or unavailable. "
      "INTERNAL reference — read it, then answer the user in your own words.",
      params({}, required=[]), level=0)
async def capabilities(ctx, args):
    from . import INTERNAL, REGISTRY
    lines = [INTERNAL + "# What I can do", ""]
    hermes = ctx.app.runner.hermes
    if hermes.enabled:
        lines.append(
            "## Hermes executor: CONNECTED — this is how ALL real work gets done\n"
            f"- gateway: {hermes.base}\n"
            "- Hermes is a full agent: terminal/shell, file operations, coding, web browsing "
            "and research, and any external services connected on the Hermes side "
            "(email, calendar, MCP servers...).\n"
            "- Use delegate(task) for ANYTHING beyond the kernel tools below. If unsure "
            "whether Hermes can do it, delegate anyway and let it try — never refuse first.\n"
            "- Setting up a new external service (e.g. Google Calendar)? That also happens "
            "on the Hermes side — delegate the setup itself as a task.")
    else:
        lines.append(
            "## Hermes executor: NOT CONFIGURED\n"
            "Without Hermes you can only chat, remember, search and schedule — no shell, "
            "files, browsing or external services. Tell the user to set hermes.enabled=true "
            "and hermes.base_url in Settings → config.yaml (their Hermes gateway must run "
            "with API_SERVER_ENABLED=true, key in .env as HERMES_API_KEY).")
    lines.append("\n## Kernel tools (I use these directly)")
    for n in KERNEL_TOOLS:
        t = REGISTRY.get(n)
        if t:
            lines.append(f"- {t.name}: {t.description}")
    lines.append("\n## External modules\n- " + ctx.app.gcal.status())
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


@tool("offer_approval_options",
      "Ask the user to DECIDE a pending approval (from the task board). Emits the fixed "
      "decision question with the exact allowed options and arms 1:1 matching: if the user's "
      "next message is exactly one of the options, the decision executes directly without you. "
      "You NEVER approve or deny anything yourself — this tool is the only path to a decision. "
      "Use it when the user says they are ready to decide; if they answer anything else, "
      "converse normally and call it again when they're ready.",
      params({"approval_id": {"type": "integer", "description": "pending approval id"}}),
      level=0, direct=True)
async def offer_approval_options(ctx, args):
    import json as _json
    from .. import voice
    row = await ctx.app.db.fetchone(
        "SELECT * FROM approvals WHERE id=? AND status='pending'", (args["approval_id"],))
    if not row:
        return "No pending approval with that id — check the task board."
    choices = ["approve", "deny"]
    if row["run_id"]:
        try:
            choices = _json.loads(row["args_json"]).get("_hermes_choices") or \
                ["once", "session", "always", "deny"]
        except (_json.JSONDecodeError, TypeError):
            choices = ["once", "session", "always", "deny"]
    ctx.app.approvals.arm_options(ctx.user_thread_id, row["id"], choices)
    return voice.APPROVAL_OPTIONS.format(options=voice.options_sentence(choices))


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
