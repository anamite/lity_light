"""Nightly reflection — the agent's sleep cycle. Once per night (user-local
time, during quiet hours by default), two cheap utility-model passes:

1. MEMORY CONSOLIDATION — archive duplicate/superseded memories and write
   merged replacements. Archive, never delete: SQLite keeps everything, and
   MEMORY.md is re-exported afterwards.
2. DAY REVIEW — digest the last 24 hours (messages, finished tasks, goals,
   new memories) and, if anything deserves follow-up, wake the kernel with
   it — which may become a goal, a schedule, a memory, or nothing (NO_REPLY).

Runs off the scheduler tick; `reflection:` in config.yaml, re-read live.
The last-run date persists in the kv table, so a restart never re-runs it."""

import json
import logging

from .context import user_now
from .modules import modules_cfg

log = logging.getLogger("lity.reflect")

CONSOLIDATE_SYSTEM = """You are the nightly memory-consolidation pass of a personal agent.
Below is every active memory as `id | kind | content`.
Return STRICT JSON, nothing else: {"archive": [ids], "merged": [{"kind": "...", "content": "..."}]}
- archive: ONLY clear duplicates, superseded facts, or trivia with no future value.
- merged: when several archived memories collapse into one better fact, write it here
  (kind: user | project | feedback | reference).
Be conservative — when unsure, keep. Empty lists are a fine answer."""

REVIEW_SYSTEM = """You are the nightly reflection of a personal agent. Below is a digest of
the last 24 hours. Reply with at most 4 short sentences: what mattered, anything unresolved
worth following up, and at most ONE concrete suggestion (a goal to add, a routine to
schedule, a fact worth remembering). If the day genuinely needs no follow-up, reply exactly:
RF_OK"""

MEMORY_KINDS = ("user", "project", "feedback", "reference")


def _json_block(text: str):
    """Tolerant JSON extraction: strips code fences, grabs the outer object."""
    t = (text or "").strip()
    try:
        return json.loads(t[t.index("{"): t.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None


class Reflection:
    def __init__(self, app):
        self.app = app

    def cfg(self) -> dict:
        return modules_cfg(self.app, "reflection")

    async def maybe_run(self):
        c = self.cfg()
        if not c.get("enabled", True):
            return
        now = user_now(self.app)
        try:
            hh, mm = map(int, str(c.get("time") or "03:30").split(":"))
        except ValueError:
            hh, mm = 3, 30
        if (now.hour, now.minute) < (hh, mm):
            return
        today = now.strftime("%Y-%m-%d")
        if await self.app.db.get_kv("reflection.last_date") == today:
            return
        await self.app.db.set_kv("reflection.last_date", today)
        log.info("nightly reflection running for %s", today)
        try:
            await self._consolidate()
        except Exception:
            log.exception("memory consolidation failed")
        try:
            await self._day_review()
        except Exception:
            log.exception("day review failed")

    async def _consolidate(self):
        rows = await self.app.db.fetchall(
            "SELECT id, kind, content FROM memories WHERE archived=0 ORDER BY id LIMIT 150")
        if len(rows) < 8:
            return  # nothing worth a model call yet
        listing = "\n".join(f"{r['id']} | {r['kind']} | {r['content'][:200]}" for r in rows)
        out = await self.app.llm.complete(
            self.app.cfg.get_path("models.utility"), CONSOLIDATE_SYSTEM, listing,
            max_tokens=600)
        data = _json_block(out)
        if not isinstance(data, dict):
            return
        known = {r["id"] for r in rows}
        archive = [i for i in (data.get("archive") or [])
                   if isinstance(i, int) and i in known][:15]
        merged = [m for m in (data.get("merged") or [])
                  if isinstance(m, dict) and str(m.get("content") or "").strip()][:5]
        for i in archive:
            await self.app.db.execute("UPDATE memories SET archived=1 WHERE id=?", (i,))
        for m in merged:
            kind = m.get("kind") if m.get("kind") in MEMORY_KINDS else "project"
            await self.app.memory.save(str(m["content"]).strip(), kind, 1)
        if archive or merged:
            log.info("reflection: archived %d memories, wrote %d merged",
                     len(archive), len(merged))
            await self.app.memory.export_md()

    async def _day_review(self):
        db = self.app.db
        counts = await db.fetchone(
            "SELECT SUM(role='user') AS u, SUM(role='assistant') AS a FROM messages "
            "WHERE created_at >= datetime('now','-1 day')")
        tasks = await db.fetchall(
            "SELECT id, status, task, result FROM tasks "
            "WHERE finished_at >= datetime('now','-1 day') ORDER BY id DESC LIMIT 8")
        goals = await db.fetchall(
            "SELECT id, title, review_at FROM goals WHERE status='active' LIMIT 8")
        mems = await db.fetchall(
            "SELECT kind, content FROM memories WHERE archived=0 "
            "AND created_at >= datetime('now','-1 day') ORDER BY id DESC LIMIT 10")
        task_lines = "\n".join(
            f"#{t['id']} {t['status']}: {t['task'][:80]} → {(t['result'] or '')[:120]}"
            for t in tasks) or "(none)"
        goal_lines = "\n".join(
            f"#{g['id']} {g['title'][:70]}"
            + ("" if g["review_at"] else " (no review set)") for g in goals) or "(none)"
        mem_lines = "\n".join(f"[{m['kind']}] {m['content'][:100]}" for m in mems) or "(none)"
        digest = (f"MESSAGES: {counts['u'] or 0} from the user, {counts['a'] or 0} replies.\n\n"
                  f"FINISHED TASKS:\n{task_lines}\n\n"
                  f"ACTIVE GOALS:\n{goal_lines}\n\n"
                  f"NEW MEMORIES:\n{mem_lines}")
        out = await self.app.llm.complete(
            self.app.cfg.get_path("models.utility"), REVIEW_SYSTEM, digest, max_tokens=250)
        if not out or out.strip().startswith("RF_OK"):
            return
        await self.app.kernel.system_event(
            1, "[nightly reflection] " + out.strip() +
            " (Act if warranted: add/update a goal, set a schedule, or save a memory. "
            "If nothing is needed, reply NO_REPLY.)")
