"""Local semantic embeddings for memory recall (model2vec 'potion' models).

Static embeddings = token lookup + mean pooling: no transformer forward pass,
numpy-only inference, sub-millisecond per sentence even on a Pi. The model is
downloaded from HuggingFace on first start and cached on disk; if model2vec
isn't installed or the download fails, everything degrades gracefully to
FTS-only recall — no hard dependency."""

import asyncio
import logging

log = logging.getLogger("lity.embeddings")

DEFAULT_MODEL = "minishlab/potion-retrieval-32M"


class Embedder:
    def __init__(self, app):
        self.app = app
        self._model = None
        self._failed = False
        self._lock = asyncio.Lock()

    def enabled(self) -> bool:
        return bool(self.app.cfg.get_path("memory.embeddings", True))

    def model_name(self) -> str:
        return str(self.app.cfg.get_path("memory.embed_model", DEFAULT_MODEL))

    async def _ensure(self):
        if self._model is not None or self._failed or not self.enabled():
            return self._model
        async with self._lock:
            if self._model is not None or self._failed:
                return self._model
            name = self.model_name()

            def _load():
                from model2vec import StaticModel
                return StaticModel.from_pretrained(name)

            try:
                self._model = await asyncio.get_running_loop().run_in_executor(None, _load)
                log.info("embedding model '%s' loaded", name)
            except Exception as e:
                self._failed = True
                log.warning("embeddings unavailable (%s) — memory recall is FTS-only", e)
        return self._model

    async def embed(self, texts: list[str]):
        """list[str] -> L2-normalised float32 matrix (n, dim), or None."""
        model = await self._ensure()
        if model is None or not texts:
            return None
        import numpy as np

        def _enc():
            v = np.asarray(model.encode(list(texts)), dtype=np.float32)
            n = np.linalg.norm(v, axis=1, keepdims=True)
            n[n == 0] = 1.0
            return v / n

        return await asyncio.get_running_loop().run_in_executor(None, _enc)

    async def embed_one(self, text: str):
        m = await self.embed([text])
        return None if m is None else m[0]

    async def blob(self, text: str) -> bytes | None:
        v = await self.embed_one(text)
        return None if v is None else v.tobytes()

    async def similarity(self, a: str, b: str) -> float | None:
        m = await self.embed([a, b])
        return None if m is None else float(m[0] @ m[1])

    async def warmup(self):
        """Startup background task: load the model and backfill embeddings for
        memories that don't have one. If the configured model changed, every
        stored vector is wiped and re-embedded (dimensions/space differ)."""
        model = await self._ensure()
        if model is None:
            return
        db = self.app.db
        if await db.get_kv("memory.embed_model") != self.model_name():
            await db.execute("UPDATE memories SET embedding=NULL")
            await db.set_kv("memory.embed_model", self.model_name())
        rows = await db.fetchall(
            "SELECT id, content FROM memories WHERE archived=0 AND embedding IS NULL LIMIT 2000")
        if rows:
            mat = await self.embed([r["content"] for r in rows])
            if mat is not None:
                for r, vec in zip(rows, mat):
                    await db.execute("UPDATE memories SET embedding=? WHERE id=?",
                                     (vec.tobytes(), r["id"]))
                log.info("backfilled embeddings for %d memories", len(rows))
        self.app.memory.invalidate_cache()
