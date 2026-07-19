"""Memory: parallel extraction (never blocks a reply) + hybrid recall —
FTS5 keywords blended with local semantic embeddings, plus recency and
usage boosts. SQLite is authoritative; MEMORY.md is a human-readable export."""

import asyncio
import json
import math
import re
import time
from datetime import datetime, timezone


def _fts_query(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9_]{3,}", text)[:12]
    return " OR ".join(words)


EXTRACT_SYSTEM = """You extract durable facts from a conversation exchange for an AI agent's long-term memory.
The input may begin with a THREAD SUMMARY — use it only to interpret the exchange; extract facts
from the USER/ASSISTANT exchange itself.
Return a JSON array (possibly empty). Each item: {"kind": "user"|"project"|"feedback"|"reference", "content": "<one self-contained sentence>"}.
kinds: user = who the user is / preferences; project = ongoing work or decisions; feedback = how the agent should behave; reference = URLs/resources.
Only durable facts worth recalling weeks later. No small talk, no transient state. Convert relative dates to absolute. Return [] if nothing qualifies."""

# recall scoring: semantic hits score by cosine (relevant ≈ 0.4–0.8); FTS hits
# get a rank-decayed floor so exact keyword matches always stay competitive.
_FTS_TOP = 20
_VEC_TOP = 20
_CACHE_TTL = 120  # seconds; also invalidated on every write


class Memory:
    def __init__(self, app):
        self.app = app
        self._cache = None  # (expires_monotonic, ids, matrix, meta)

    def invalidate_cache(self):
        self._cache = None

    async def save(self, content: str, kind: str = "project", thread_id: int | None = None,
                   supersedes: int | None = None) -> int:
        content = content.strip()
        blob = await self.app.embedder.blob(content)
        mid = await self.app.db.execute(
            "INSERT INTO memories(kind, content, source_thread_id, embedding) VALUES (?,?,?,?)",
            (kind, content, thread_id, blob))
        if supersedes:  # updated version of an old fact — archive the old one
            await self.app.db.execute("UPDATE memories SET archived=1 WHERE id=?", (supersedes,))
        self.invalidate_cache()
        if kind == "feedback":  # feedback reshapes the adaptation layer (LEARNED.md)
            asyncio.create_task(self.app.skills.soul_learn())
        return mid

    async def _matrix(self):
        """Cached (ids, matrix, meta) over every active embedded memory —
        small enough (a few hundred rows × ~2 KB) to brute-force in numpy."""
        now = time.monotonic()
        if self._cache and self._cache[0] > now:
            return self._cache[1], self._cache[2], self._cache[3]
        rows = await self.app.db.fetchall(
            "SELECT id, kind, content, embedding, created_at, recall_count "
            "FROM memories WHERE archived=0 AND embedding IS NOT NULL")
        try:
            import numpy as np
        except ImportError:
            return [], None, {}
        ids, vecs, meta, dim = [], [], {}, None
        for r in rows:
            v = np.frombuffer(r["embedding"], dtype=np.float32)
            if dim is None:
                dim = v.shape[0]
            if v.shape[0] != dim:
                continue
            ids.append(r["id"])
            vecs.append(v)
            m = dict(r)
            m.pop("embedding", None)
            meta[r["id"]] = m
        matrix = np.vstack(vecs) if vecs else None
        self._cache = (now + _CACHE_TTL, ids, matrix, meta)
        return ids, matrix, meta

    async def recall(self, query: str, k: int = 5, min_score: float = 0.0,
                     update_stats: bool = True) -> list[dict]:
        """Hybrid recall. Returns [{id, kind, content, score}, ...] sorted by
        score: max(cosine, FTS floor) + recency boost + small usage boost."""
        query = (query or "").strip()
        if not query:
            return []
        cand: dict[int, dict] = {}

        q = _fts_query(query)
        if q:
            rows = await self.app.db.fetchall(
                """SELECT m.id, m.kind, m.content, m.created_at, m.recall_count
                   FROM memories_fts f JOIN memories m ON m.id = f.rowid
                   WHERE memories_fts MATCH ? AND m.archived=0
                   ORDER BY rank LIMIT ?""",
                (q, _FTS_TOP))
            for pos, r in enumerate(rows):
                d = dict(r)
                d["score"] = max(0.30, 0.48 - 0.03 * pos)
                cand[d["id"]] = d

        qv = await self.app.embedder.embed_one(query)
        if qv is not None:
            ids, matrix, meta = await self._matrix()
            if matrix is not None and matrix.shape[1] == qv.shape[0]:
                import numpy as np
                sims = matrix @ qv
                for i in np.argsort(-sims)[:_VEC_TOP]:
                    mid, sim = ids[int(i)], float(sims[int(i)])
                    if mid in cand:
                        cand[mid]["score"] = max(cand[mid]["score"], sim)
                    else:
                        cand[mid] = {**meta[mid], "score": sim}

        now = datetime.now(timezone.utc)
        for d in cand.values():
            d["score"] += _recency_boost(d.get("created_at"), now) \
                + 0.01 * min(int(d.get("recall_count") or 0), 5) / 5
        hits = sorted(cand.values(), key=lambda d: -d["score"])
        hits = [h for h in hits if h["score"] >= min_score][:k]
        if hits and update_stats:
            marks = ",".join("?" * len(hits))
            await self.app.db.execute(
                f"UPDATE memories SET recall_count=recall_count+1, "
                f"last_recalled_at=datetime('now') WHERE id IN ({marks})",
                tuple(h["id"] for h in hits))
        return hits

    async def extract(self, thread_id: int, user_text: str, assistant_text: str):
        """Background job: distill durable facts from one exchange, grounded by
        the thread summary. A fact similar to a stored one SUPERSEDES it
        (old is archived) instead of being dropped, so updates win."""
        summary = await self.app.db.fetchone(
            "SELECT content FROM summaries WHERE thread_id=?", (thread_id,))
        ctx = (f"THREAD SUMMARY (context only):\n{summary['content'][:800]}\n\n"
               if summary else "")
        exchange = ctx + f"USER: {user_text[:2000]}\n\nASSISTANT: {assistant_text[:2000]}"
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
            kind = item.get("kind", "project")
            supersedes = None
            dupes = await self.recall(content, k=1, update_stats=False)
            if dupes:
                old = dupes[0]
                jac = _jaccard(old["content"], content)
                cos = await self.app.embedder.similarity(old["content"], content)
                if jac > 0.85 or (cos is not None and cos > 0.95):
                    continue  # same fact, already stored
                if jac > 0.5 or (cos is not None and cos > 0.88):
                    supersedes = old["id"]  # updated fact — replace, don't drop
            await self.save(content, kind, thread_id, supersedes=supersedes)
            self.app.bus.emit("memory.created", content=content, kind=kind)

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


def _recency_boost(created_at, now) -> float:
    """Newer memories float up: +0.06 fresh, ~+0.02 after a month, →0."""
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0.0
    days = max(0.0, (now - dt).total_seconds() / 86400)
    return 0.06 * math.exp(-days / 30)


def _jaccard(a: str, b: str) -> float:
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _similar(a: str, b: str) -> bool:
    return _jaccard(a, b) > 0.6
