"""Builds the kernel's budgeted context. Six slots, hard character caps —
this file is why the main thread can eventually run on a small local model."""

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

DELEGATION_POLICY = """## Delegation policy
You are the lightweight front desk. Delegate anything heavy with `delegate`:
code/data work → coder · deep web research → researcher · interactive web
pages → browser · installs/system work → shell · long documents → writer.
Delegated tasks run in parallel; a system event delivers each result — relay
it to the user conversationally, ONCE. The '## Task board' block (when shown)
is the live state of every open task; use task_log(task_id) to peek inside a
task's thread when the user asks what it is doing. If a system event needs no user-facing
reply (internal, duplicate, or something you already reported), output exactly
NO_REPLY and nothing else. Use quick_search only for one-shot lookups.
Keep replies short. Never do a sub-agent's job yourself.
External services (calendar, email, drive...) not connected yet? You can
connect them YOURSELF: call `capabilities` for the exact recipe, walk the
user through it, then connect_service — never just say it's unavailable.
Unsure whether you can do something (schedules, file ops, agents, tools)?
Call `capabilities` FIRST — never refuse or invent limits from assumption."""


def _read(path: Path, cap: int) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text if len(text) <= cap else text[:cap] + "\n…[truncated to fit budget]"


async def build_system(app, thread_id: int, user_text: str) -> str:
    ws = app.cfg.workspace
    budget = int(app.cfg.get_path("kernel.system_char_budget", 6400))
    slot = budget // 5

    soul = _read(ws / "SOUL.md", slot)
    learned = _read(ws / "LEARNED.md", slot // 2)  # adaptation layer, grows from feedback
    if learned:
        soul = f"{soul}\n\n{learned}"
    user_md = _read(ws / "USER.md", slot)

    agent_lines = "\n".join(f"- {a.name}: {a.description}" for a in app.agents.all())
    agents_block = f"## Sub-agents available\n{agent_lines}"[:slot]

    mems = await app.memory.recall(user_text, k=int(app.cfg.get_path("kernel.memory_inject_top_k", 5)))
    mem_block = ""
    if mems:
        mem_block = "## Possibly relevant memories\n" + "\n".join(
            f"- [{m['kind']}] {m['content']}" for m in mems)
        mem_block = mem_block[:slot]

    summary_row = await app.db.fetchone("SELECT content FROM summaries WHERE thread_id=?", (thread_id,))
    summary_block = f"## Earlier in this thread (summary)\n{summary_row['content'][:slot]}" if summary_row else ""

    # routing lessons learned from past delegations (skills stored under agent='kernel')
    hints = await app.skills.recall("kernel", user_text, k=2)
    hints_block = ""
    if hints:
        hints_block = ("## Learned routing hints\n" + "\n".join(
            f"- {h['name']}: {h['content']}" for h in hints))[:slot // 2]

    tasks_block = await _task_board(app, slot)

    parts = [soul, user_md, agents_block, DELEGATION_POLICY, tasks_block,
             hints_block, mem_block, summary_block]
    return "\n\n".join(p for p in parts if p)


def _age(ts: str) -> str:
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "?"
    secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h"


async def _task_board(app, slot: int) -> str:
    """Slot 7: live board of open tasks + the last few finished ones, so the
    kernel always knows every task id and its current state."""
    rows = await app.db.fetchall(
        "SELECT id, agent, status, task, created_at FROM tasks "
        "WHERE status IN ('queued','running','waiting_user') "
        "   OR (finished_at IS NOT NULL AND finished_at >= datetime('now','-15 minutes')) "
        "ORDER BY id DESC LIMIT 8")
    if not rows:
        return ""
    lines = [f"#{r['id']} {r['agent']} · {r['status']} · "
             f"{r['task'][:60]} · {_age(r['created_at'])} ago" for r in rows]
    return ("## Task board (waiting_user = needs the user's approval NOW)\n"
            + "\n".join(lines))[:slot]


MAX_IMAGE_BYTES = 6_000_000


def _image_part(ws: Path, att: dict) -> dict | None:
    p = (ws / att.get("path", "")).resolve()
    if not p.is_relative_to(ws) or not p.is_file() or p.stat().st_size > MAX_IMAGE_BYTES:
        return None
    data = base64.b64encode(p.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{att.get('mime', 'image/png')};base64,{data}"}}


async def build_messages(app, thread_id: int) -> tuple[list[dict], bool]:
    """History window: user/assistant verbatim, resolved tool activity as one-line
    events. The most recent image attachments are embedded as vision content;
    returns (messages, has_images) so the kernel can pick the vision model."""
    limit = int(app.cfg.get_path("kernel.max_window_messages", 20))
    max_imgs = int(app.cfg.get_path("kernel.vision_max_images", 2))
    rows = await app.db.recent_messages(thread_id, limit)
    ws = app.cfg.workspace

    # only the newest N images are embedded (older ones stay text-only)
    image_msg_ids = []
    for r in reversed(rows):
        if len(image_msg_ids) >= max_imgs:
            break
        if r["role"] == "user" and r["attachment"]:
            try:
                att = json.loads(r["attachment"])
            except (json.JSONDecodeError, TypeError):
                continue
            if str(att.get("mime", "")).startswith("image/"):
                image_msg_ids.append(r["id"])

    out, has_images = [], False
    for r in rows:
        if r["role"] == "user":
            if r["id"] in image_msg_ids:
                part = _image_part(ws, json.loads(r["attachment"]))
                if part:
                    out.append({"role": "user", "content": [
                        {"type": "text", "text": r["content"]}, part]})
                    has_images = True
                    continue
            out.append({"role": "user", "content": r["content"]})
        elif r["role"] == "assistant":
            out.append({"role": "assistant", "content": r["content"]})
        elif r["role"] == "event":
            out.append({"role": "user", "content": f"[SYSTEM EVENT] {r['content']}"})
    return out, has_images
