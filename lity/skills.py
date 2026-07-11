"""The skill-learning loop (the Hermes 'compounding agent' pattern).

Two halves:
1. DISTILL — after a sub-agent finishes a task, a background utility-model job
   decides whether anything generalizes into a reusable procedure. New skills
   are inserted; similar existing skills are refined in place, so repetition
   makes them better instead of duplicating them.
2. RECALL — when a sub-agent starts a task, the top-matching skills (FTS5) are
   injected into its system prompt: the agent follows a proven procedure
   instead of improvising.

Plus SOUL LEARNING: whenever a `feedback`-kind memory is saved, LEARNED.md is
rewritten from all feedback memories — a capped, user-editable adaptation
layer loaded with SOUL.md into every kernel turn.
"""

import json
import re

from .memory import _fts_query, _similar

DISTILL_SYSTEM = """You review a completed AI-agent task and decide whether it yields a REUSABLE SKILL.
A skill is a procedure that would make a SIMILAR future task faster or more reliable:
the working approach, the right commands/selectors/steps, the pitfall that was hit and how it was avoided.
NOT a skill: plain facts (that's memory), one-off results, tasks that went smoothly with zero non-obvious insight.
Reply with JSON only:
  {"name": "<3-6 word imperative name>", "description": "<one line: when this applies>", "steps": "<3-8 terse numbered steps>"}
or exactly: null"""

ROUTE_SYSTEM = """You watch a personal AI agent (the "kernel") delegate tasks to specialist sub-agents.
From the completed delegation below, decide if there is a REUSABLE ROUTING LESSON: which sub-agent
(or kernel tool) is — or is NOT — the right choice for this KIND of request, or a task phrasing that
made the delegation succeed. NOT a lesson: task-specific facts, one-offs, smooth runs with an obvious route.
Reply with JSON only:
  {"name": "<3-6 word imperative name>", "description": "<one line: when this applies>", "steps": "<1-3 terse lines: the route + why / the pitfall>"}
or exactly: null"""

LEARN_SYSTEM = """You maintain LEARNED.md — the adaptation layer of a personal AI agent's personality.
Given the user's accumulated feedback notes, write the new full LEARNED.md:
max 12 bullet lines, each one durable behavioural guidance (tone, format, preferences, dos/don'ts).
Merge related notes, drop contradictions in favour of the newest, no headers except the first line '# LEARNED'.
Output only the file content."""


class Skills:
    def __init__(self, app):
        self.app = app

    def enabled(self) -> bool:
        return bool(self.app.cfg.get_path("skills.enabled", True))

    async def recall(self, agent: str, text: str, k: int | None = None) -> list[dict]:
        if not self.enabled():
            return []
        k = k or int(self.app.cfg.get_path("skills.inject_top_k", 2))
        q = _fts_query(text)
        if not q:
            return []
        rows = await self.app.db.fetchall(
            """SELECT s.* FROM skills_fts f JOIN skills s ON s.id = f.rowid
               WHERE skills_fts MATCH ? AND s.archived=0 AND s.agent IN (?, '*')
               ORDER BY rank LIMIT ?""",
            (q, agent, k))
        if rows:
            ids = [r["id"] for r in rows]
            await self.app.db.execute(
                f"UPDATE skills SET uses=uses+1 WHERE id IN ({','.join('?'*len(ids))})",
                tuple(ids))
        return [dict(r) for r in rows]

    @staticmethod
    def as_prompt_block(skills: list[dict]) -> str:
        if not skills:
            return ""
        parts = ["## Learned skills (proven on past tasks — follow unless clearly wrong here)"]
        for s in skills:
            parts.append(f"### {s['name']}\n_{s['description']}_\n{s['content']}")
        return "\n\n".join(parts)

    async def distill(self, agent: str, task_text: str, result: str,
                      transcript: str, task_id: int | None = None):
        """Background job — never blocks the task result."""
        if not self.enabled():
            return
        prompt = (f"AGENT: {agent}\nTASK: {task_text[:800]}\n\n"
                  f"WHAT HAPPENED (tool calls):\n{transcript[:3000]}\n\n"
                  f"FINAL RESULT:\n{result[:1500]}")
        try:
            raw = await self.app.llm.complete(
                self.app.cfg.get_path("models.utility"), DISTILL_SYSTEM, prompt, max_tokens=500)
        except Exception:
            return
        await self._store(agent, raw, task_id)

    async def distill_routing(self, agent: str, task_text: str, status: str,
                              result: str, task_id: int | None = None):
        """Background job: learn WHICH agent/tool fits which kind of request.
        Lessons are stored under agent='kernel' and injected into the kernel's
        system prompt by context.build_system. Runs on failures too — a wrong
        route is exactly what's worth remembering."""
        if not self.enabled():
            return
        prompt = (f"DELEGATED TO: {agent}\nOUTCOME: {status}\nTASK: {task_text[:800]}\n\n"
                  f"RESULT:\n{result[:1200]}")
        try:
            raw = await self.app.llm.complete(
                self.app.cfg.get_path("models.utility"), ROUTE_SYSTEM, prompt, max_tokens=300)
        except Exception:
            return
        await self._store("kernel", raw, task_id)

    async def _store(self, agent: str, raw: str, task_id: int | None):
        """Parse a distiller reply; refine an existing similar skill or insert a new one."""
        m = re.search(r"\{.*\}", raw or "", re.S)
        if not m:
            return
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return
        if not data or not data.get("name") or not data.get("steps"):
            return
        name, desc = data["name"].strip(), (data.get("description") or "").strip()
        content = str(data["steps"]).strip()

        # refine an existing similar skill instead of duplicating it
        existing = await self.recall(agent, f"{name} {desc}", k=1)
        if existing and _similar(existing[0]["name"] + " " + existing[0]["description"],
                                 name + " " + desc):
            await self.app.db.execute(
                "UPDATE skills SET description=?, content=?, updated_at=datetime('now') WHERE id=?",
                (desc, content, existing[0]["id"]))
            self.app.bus.emit("skill.updated", id=existing[0]["id"], name=existing[0]["name"])
            return
        sid = await self.app.db.execute(
            "INSERT INTO skills(agent, name, description, content, source_task_id) VALUES (?,?,?,?,?)",
            (agent, name, desc, content, task_id))
        self.app.bus.emit("skill.created", id=sid, name=name, agent=agent)

    async def soul_learn(self):
        """Rewrite LEARNED.md from all feedback-kind memories (capped, editable)."""
        rows = await self.app.db.fetchall(
            "SELECT content, created_at FROM memories WHERE kind='feedback' AND archived=0 "
            "ORDER BY id DESC LIMIT 40")
        if not rows:
            return
        notes = "\n".join(f"- ({r['created_at']}) {r['content']}" for r in rows)
        try:
            out = await self.app.llm.complete(
                self.app.cfg.get_path("models.utility"), LEARN_SYSTEM, notes, max_tokens=400)
        except Exception:
            return
        if out:
            (self.app.cfg.workspace / "LEARNED.md").write_text(out.strip() + "\n", encoding="utf-8")
            self.app.bus.emit("soul.learned")
