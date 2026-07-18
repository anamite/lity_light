"""Builds the kernel's budgeted context. Six slots, hard character caps —
this file is why the main thread can eventually run on a small local model."""

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path

DELEGATION_POLICY = """## Delegation policy — Hermes does the work
You are the lightweight front desk. Hermes is your single executor: a full
agent with terminal, files, coding, web browsing/research, email and every
external service. Anything beyond your own small tools — running
code, touching files, browsing, installing, researching, sending email,
connecting new services — goes to Hermes via
`delegate(task)`. EXCEPTIONS that are LOCAL and instant — never delegate
these: timers/alarms (`timer` tool; when one is RINGING on the task board
and the user says stop, call timer with action stop_ringing), quick notes
(`note`), shopping lists (`shopping`), weather (`weather`), speaker volume
(`volume`: get/set/up/down/mute — "louder"/"quieter" means this), Google
Calendar (`calendar`: agenda/add/update/delete; not connected yet? its
setup action returns a manual — walk the user through it), the physical
environment (`environment` = live snapshot of this machine + smart-home
sensors/devices; `env_act` = switch a device on/off), your goal board
(`goal`: long-horizon follow-ups you pursue across days — add one whenever
something deserves a later check-in), and the current
time/date (already in '## Now' — answer directly, no tool). Do it AUTOMATICALLY: the user never has to say "use
Hermes" or name an executor; that routing is your job. Write the task
complete and self-contained (Hermes has none of this conversation).
Delegated tasks run in parallel; a system event delivers each result — relay
it to the user conversationally, ONCE. The '## Task board' block (when
shown) is the live state of every open task; use task_log(task_id) to peek
inside a task's thread when the user asks what it is doing. If a system
event needs no user-facing reply (internal, duplicate, or something you
already reported), output exactly NO_REPLY and nothing else. Use
quick_search only for one-shot lookups. Keep replies short. Never grind
through heavy work yourself, and never refuse: unsure whether something is
possible? Call `capabilities` FIRST, and when in doubt delegate to Hermes
and let it try.
Style: your replies may be READ ALOUD by a voice assistant. Plain speakable
sentences only — no markdown, no emojis, no tables, no bullet lists, no
code blocks. Simple punctuation.
Notifications: your replies (including relayed task results) are spoken to
the user. Approvals are the exception: they are never announced with speech
— the voice client just plays a beep. When the user asks what the beep or
notification was, summarize ALL pending approvals in one short answer from
the task board: e.g. "three approvals pending on the cron-job task". Never
list them one at a time across turns.
Approvals: pending approvals appear on the task board with their id. If the
user wants details, use task_log on that task and explain in your own words.
When the user is ready to decide, call offer_approval_options(approval_id) —
you can NEVER approve or deny anything yourself, and you never invent option
words; only that tool's exact options, spoken back by the user, execute a
decision. If their answer isn't an exact option, keep conversing and offer
again. If several approvals are pending, work through them in ONE
conversation: after one is decided, immediately offer the next."""


# ── user time context: timezone + quiet hours ───────────────────────────────
# `user:` in config.yaml, re-read live. Used by the kernel clock, daily/weekly
# schedules, goal reviews, the heartbeat and the nightly reflection.

def user_tz(app):
    """The user's tzinfo: user.timezone (IANA) if set, else the system zone."""
    from .modules import modules_cfg
    name = str((modules_cfg(app, "user") or {}).get("timezone") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def user_now(app) -> datetime:
    return datetime.now(user_tz(app))


def quiet_now(app) -> bool:
    """True inside user.quiet_hours ('23:00-07:30', user-local; overnight
    ranges fine). During quiet hours nothing is spoken aloud and the heartbeat
    rests — proactive messages still land silently in the UI for the morning."""
    from .modules import modules_cfg
    spec = str((modules_cfg(app, "user") or {}).get("quiet_hours") or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", spec)
    if not m:
        return False
    start, end = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    if start == end:
        return False
    now = user_now(app)
    cur = now.hour * 60 + now.minute
    return start <= cur < end if start < end else (cur >= start or cur < end)


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

    mems = await app.memory.recall(user_text, k=int(app.cfg.get_path("kernel.memory_inject_top_k", 5)))
    mem_block = ""
    if mems:
        mem_block = "## Possibly relevant memories\n" + "\n".join(
            f"- [{m['kind']}] {m['content']}" for m in mems)
        mem_block = mem_block[:slot]

    summary_row = await app.db.fetchone("SELECT content FROM summaries WHERE thread_id=?", (thread_id,))
    summary_block = f"## Earlier in this thread (summary)\n{summary_row['content'][:slot]}" if summary_row else ""

    tasks_block = await _task_board(app, slot)
    goals_block = await _goal_board(app, slot // 2)

    now_local = user_now(app)
    clock = (f"## Now\n{now_local.strftime('%A, %Y-%m-%d %H:%M')} local time "
             f"({now_local.tzname()}).")

    # today's agenda (gcal module) — cached inside the module, "" when the
    # module is off or set to on_demand
    cal_block = await app.gcal.system_block(slot // 2)

    parts = [soul, user_md, clock, cal_block, DELEGATION_POLICY, tasks_block,
             goals_block, mem_block, summary_block]
    return "\n\n".join(p for p in parts if p)


def _age_secs(secs: int) -> str:
    secs = max(0, secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h {secs % 3600 // 60}m"


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


async def _goal_board(app, slot: int) -> str:
    """Active long-horizon goals — always visible so the kernel keeps pursuing
    them; managed with the goal tool, reviews fired by the scheduler."""
    rows = await app.db.fetchall(
        "SELECT id, title, review_at FROM goals WHERE status='active' ORDER BY id LIMIT 6")
    if not rows:
        return ""
    lines = [f"#{r['id']} {r['title'][:70]}"
             + (f" · review {r['review_at']} UTC" if r["review_at"] else "")
             for r in rows]
    return ("## Goals I'm pursuing (long-horizon — manage via goal tool)\n"
            + "\n".join(lines))[:slot]


async def _task_board(app, slot: int) -> str:
    """Slot 7: live board of open tasks + the last few finished ones, so the
    kernel always knows every task id and its current state."""
    rows = await app.db.fetchall(
        "SELECT id, agent, status, task, created_at FROM tasks "
        "WHERE status IN ('queued','running','waiting_user') "
        "   OR (finished_at IS NOT NULL AND finished_at >= datetime('now','-15 minutes')) "
        "ORDER BY id DESC LIMIT 8")
    approvals = await app.db.fetchall(
        "SELECT id, tool, task_id FROM approvals WHERE status='pending' ORDER BY id DESC LIMIT 4")
    timers = await app.db.fetchall(
        "SELECT id, kind, label, fires_at, status FROM qtimers "
        "WHERE status IN ('pending','ringing') ORDER BY status DESC, fires_at LIMIT 6")
    if not rows and not approvals and not timers:
        return ""
    lines = []
    now = datetime.now(timezone.utc)
    for t in timers:
        if t["status"] == "ringing":
            lines.append(f"RINGING NOW: {t['kind']} #{t['id']} '{t['label']}' — user says "
                         f"stop → timer(action='stop_ringing')")
        else:
            try:
                fires = datetime.strptime(t["fires_at"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc)
                left = _age_secs(int((fires - now).total_seconds()))
            except (ValueError, TypeError):
                left = "?"
            lines.append(f"{t['kind']} #{t['id']} '{t['label']}' fires in {left}")
    lines += [f"#{r['id']} {r['agent']} · {r['status']} · "
              f"{r['task'][:60]} · {_age(r['created_at'])} ago" for r in rows]
    lines += [f"approval #{a['id']} PENDING (tool {a['tool']}, task #{a['task_id']}) "
              f"— decide via offer_approval_options({a['id']})"
              + (" · ALSO sent to the user's Telegram with decision buttons — a tap "
                 "there resolves it" if app.telegram.has_card(a["id"]) else "")
              for a in approvals]
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
