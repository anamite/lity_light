"""Memory: parallel extraction (never blocks a reply) + FTS5 recall.
SQLite is authoritative; MEMORY.md is a periodic human-readable export."""

import json
import re


def _fts_query(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9_]{3,}", text)[:12]
    return " OR ".join(words)


EXTRACT_SYSTEM = """You extract durable facts from a conversation exchange for an AI agent's long-term memory.
Return a JSON array (possibly empty). Each item: {"kind": "user"|"project"|"feedback"|"reference", "content": "<one self-contained sentence>"}.
kinds: user = who the user is / preferences; project = ongoing work or decisions; feedback = how the agent should behave; reference = URLs/resources.
Only durable facts worth recalling weeks later. No small talk, no transient state. Convert relative dates to absolute. Return [] if nothing qualifies."""


class Memory:
    def __init__(self, app):
        self.app = app

    async def save(self, content: str, kind: str = "project", thread_id: int | None = None) -> int:
        mid = await self.app.db.execute(
            "INSERT INTO memories(kind, content, source_thread_id) VALUES (?,?,?)",
            (kind, content.strip(), thread_id))
        if kind == "feedback":  # feedback reshapes the adaptation layer (LEARNED.md)
            import asyncio
            asyncio.create_task(self.app.skills.soul_learn())
        return mid

    async def recall(self, query: str, k: int = 5) -> list[dict]:
        q = _fts_query(query)
        if not q:
            return []
        rows = await self.app.db.fetchall(
            """SELECT m.id, m.kind, m.content FROM memories_fts f
               JOIN memories m ON m.id = f.rowid
               WHERE memories_fts MATCH ? AND m.archived=0
               ORDER BY rank LIMIT ?""",
            (q, k))
        return [dict(r) for r in rows]

    async def extract(self, thread_id: int, user_text: str, assistant_text: str):
        """Background job: distill durable facts from one exchange."""
        exchange = f"USER: {user_text[:2000]}\n\nASSISTANT: {assistant_text[:2000]}"
        try:
            raw = await self.app.llm.complete(
                self.app.cfg.get_path("models.utility"), EXTRACT_SYSTEM, exchange, max_tokens=600)
            m = re.search(r"\[.*\]", raw, re.S)
            items = json.loads(m.group(0)) if m else []
        except Exception:
            return
        for item in items[:5]:
            content = (item.get("content") or "").strip()
            if not content:
                continue
            dupes = await self.recall(content, k=1)
            if dupes and _similar(dupes[0]["content"], content):
                continue
            await self.save(content, item.get("kind", "project"), thread_id)
            self.app.bus.emit("memory.created", content=content, kind=item.get("kind", "project"))

    async def export_md(self):
        rows = await self.app.db.fetchall(
            "SELECT kind, content, created_at FROM memories WHERE archived=0 ORDER BY kind, id")
        lines = ["# MEMORY (export)", "",
                 "Human-readable export of the memory database. Regenerated automatically —",
                 "edits here are NOT read back; the SQLite store is authoritative.", ""]
        if not rows:
            lines.append("(no memories yet)")
        last_kind = None
        for r in rows:
            if r["kind"] != last_kind:
                lines.append(f"\n## {r['kind']}")
                last_kind = r["kind"]
            lines.append(f"- {r['content']}  _(saved {r['created_at']})_")
        (self.app.cfg.workspace / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _similar(a: str, b: str) -> bool:
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) > 0.6
