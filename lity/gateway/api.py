"""FastAPI gateway: a thin REST + SSE front door over the kernel. The HTML UI
is the default channel; Telegram/Discord/etc. can be added later as peers."""

import asyncio
import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .. import voice
from ..db import rows_to_dicts

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
EDITABLE_FILES = ["SOUL.md", "LEARNED.md", "USER.md", "HEARTBEAT.md", "AGENTS.md", "MEMORY.md"]


class MessageIn(BaseModel):
    content: str


class ThreadIn(BaseModel):
    title: str


class DecisionIn(BaseModel):
    decision: str  # approve | always | deny


class ConfigIn(BaseModel):
    yaml: str


class FileIn(BaseModel):
    name: str
    content: str


class MemoryIn(BaseModel):
    content: str
    kind: str = "project"  # user | project | feedback | reference


class NoteIn(BaseModel):
    title: str = ""
    content: str = ""


class ShoppingListIn(BaseModel):
    title: str


class ShoppingItemIn(BaseModel):
    item: str


class ItemDoneIn(BaseModel):
    done: bool


def create_app(core) -> FastAPI:
    api = FastAPI(title="Lity", docs_url="/api/docs")

    # ── UI ────────────────────────────────────────────────────────────────
    @api.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    # ── threads & messages ────────────────────────────────────────────────
    @api.get("/api/threads")
    async def threads():
        rows = await core.db.fetchall(
            "SELECT * FROM threads WHERE archived=0 ORDER BY (id=1) DESC, id DESC")
        return rows_to_dicts(rows)

    @api.post("/api/threads", status_code=201)
    async def create_thread(body: ThreadIn):
        tid = await core.db.create_thread(body.title or "New thread", kind="sub", parent_id=1)
        core.bus.emit("thread.created", thread_id=tid, parent_id=1, title=body.title)
        return {"id": tid}

    @api.get("/api/threads/{thread_id}/messages")
    async def messages(thread_id: int, limit: int = 200):
        rows = await core.db.fetchall(
            "SELECT * FROM messages WHERE thread_id=? ORDER BY id DESC LIMIT ?",
            (thread_id, limit))
        return list(reversed(rows_to_dicts(rows)))

    @api.post("/api/threads/{thread_id}/messages", status_code=202)
    async def post_message(thread_id: int, body: MessageIn):
        row = await core.db.fetchone("SELECT id, kind FROM threads WHERE id=?", (thread_id,))
        if not row:
            raise HTTPException(404, "no such thread")
        if row["kind"] == "agent":
            raise HTTPException(400, "agent threads are read-only")
        asyncio.create_task(core.kernel.on_user_message(thread_id, body.content))
        return {"status": "accepted"}

    # ── files: user → agent and agent → user ─────────────────────────────
    @api.post("/api/threads/{thread_id}/files", status_code=202)
    async def upload_file(thread_id: int, file: UploadFile = File(...),
                          caption: str = Form("")):
        row = await core.db.fetchone("SELECT id, kind FROM threads WHERE id=?", (thread_id,))
        if not row or row["kind"] == "agent":
            raise HTTPException(404, "no such (writable) thread")
        safe = re.sub(r"[^\w.\- ]", "_", file.filename or "upload.bin")
        rel = f"uploads/{int(time.time())}_{safe}"
        dest = core.cfg.workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(await file.read())
        att = {"path": rel, "name": safe,
               "mime": mimetypes.guess_type(safe)[0] or "application/octet-stream"}
        text = (caption.strip() + f"\n[attached file saved at workspace path: {rel}]").strip()
        await core.db.add_message(thread_id, "user", text, attachment=json.dumps(att))
        core.bus.emit("message.created", thread_id=thread_id, role="user",
                      content=text, attachment=att)
        asyncio.create_task(core.kernel._run_turn(thread_id, latest_user_text=text))
        return {"status": "accepted", "path": rel}

    @api.get("/api/files/{path:path}")
    async def serve_file(path: str):
        ws = core.cfg.workspace
        p = (ws / path).resolve()
        if not p.is_relative_to(ws) or not p.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(p)

    # ── settings dashboard ────────────────────────────────────────────────
    @api.get("/api/settings")
    async def settings():
        from ..tools import REGISTRY
        ws = core.cfg.workspace
        files = {n: (ws / n).read_text(encoding="utf-8") if (ws / n).is_file() else ""
                 for n in EDITABLE_FILES}
        tools = [{"name": t.name, "description": t.description, "level": t.level,
                  "direct_ok": t.direct_ok} for t in sorted(REGISTRY.values(),
                                                            key=lambda t: (t.level, t.name))]
        return {"config_yaml": (core.cfg.root / "config.yaml").read_text(encoding="utf-8"),
                "files": files, "tools": tools}

    @api.put("/api/settings/config")
    async def save_config(body: ConfigIn):
        try:
            data = yaml.safe_load(body.yaml)
            assert isinstance(data, dict)
        except Exception as e:
            raise HTTPException(400, f"invalid YAML: {e}")
        (core.cfg.root / "config.yaml").write_text(body.yaml, encoding="utf-8")
        core.cfg.clear()
        core.cfg.update(data)          # hot-reload; server settings need a restart
        return {"status": "ok"}

    @api.put("/api/settings/file")
    async def save_workspace_file(body: FileIn):
        if body.name not in EDITABLE_FILES:
            raise HTTPException(400, f"editable files: {EDITABLE_FILES}")
        (core.cfg.workspace / body.name).write_text(body.content, encoding="utf-8")
        return {"status": "ok"}

    # ── tasks · approvals · memories · schedules ─────────────────────────
    @api.get("/api/tasks")
    async def tasks():
        return rows_to_dicts(await core.db.fetchall(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT 50"))

    @api.get("/api/approvals")
    async def approvals():
        return rows_to_dicts(await core.db.fetchall(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY id"))

    @api.post("/api/approvals/{approval_id}")
    async def decide(approval_id: int, body: DecisionIn):
        ok = await core.approvals.resolve(approval_id, body.decision)
        if not ok:
            raise HTTPException(400, "not pending or bad decision")
        return {"status": "ok"}

    @api.get("/api/skills")
    async def skills():
        return rows_to_dicts(await core.db.fetchall(
            "SELECT * FROM skills WHERE archived=0 ORDER BY uses DESC, id DESC LIMIT 200"))

    @api.delete("/api/skills/{skill_id}")
    async def delete_skill(skill_id: int):
        await core.db.execute("UPDATE skills SET archived=1 WHERE id=?", (skill_id,))
        return {"status": "ok"}

    @api.get("/api/memories")
    async def memories():
        return rows_to_dicts(await core.db.fetchall(
            "SELECT * FROM memories WHERE archived=0 ORDER BY id DESC LIMIT 200"))

    @api.post("/api/memories", status_code=201)
    async def create_memory(body: MemoryIn):
        content = body.content.strip()
        if not content:
            raise HTTPException(400, "memory content is empty")
        mid = await core.memory.save(content, body.kind)
        return {"id": mid}

    @api.put("/api/memories/{memory_id}")
    async def update_memory(memory_id: int, body: MemoryIn):
        content = body.content.strip()
        if not content:
            raise HTTPException(400, "memory content is empty")
        row = await core.db.fetchone(
            "SELECT id FROM memories WHERE id=? AND archived=0", (memory_id,))
        if not row:
            raise HTTPException(404, "no such memory")
        await core.db.execute(
            "UPDATE memories SET content=?, kind=? WHERE id=?",
            (content, body.kind, memory_id))
        return {"status": "ok"}

    @api.delete("/api/memories/{memory_id}")
    async def delete_memory(memory_id: int):
        await core.db.execute("UPDATE memories SET archived=1 WHERE id=?", (memory_id,))
        return {"status": "ok"}

    # ── modules dashboard: shopping · notes · timers/alarms ──────────────
    @api.get("/api/modules")
    async def modules():
        lists = rows_to_dicts(await core.db.fetchall(
            "SELECT * FROM shopping_lists ORDER BY id"))
        items = rows_to_dicts(await core.db.fetchall(
            "SELECT * FROM shopping_items ORDER BY done, id"))
        for lst in lists:
            lst["items"] = [i for i in items if i["list_id"] == lst["id"]]
        notes = rows_to_dicts(await core.db.fetchall(
            "SELECT * FROM notes ORDER BY id DESC LIMIT 100"))
        timers = rows_to_dicts(await core.db.fetchall(
            "SELECT * FROM qtimers WHERE status IN ('pending','ringing') ORDER BY fires_at"))
        return {"shopping": lists, "notes": notes, "timers": timers,
                "now": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}

    @api.post("/api/shopping", status_code=201)
    async def create_shopping_list(body: ShoppingListIn):
        title = body.title.strip()[:60]
        if not title:
            raise HTTPException(400, "list title is empty")
        lid = await core.db.execute(
            "INSERT INTO shopping_lists(title) VALUES (?)", (title,))
        return {"id": lid}

    @api.delete("/api/shopping/{list_id}")
    async def delete_shopping_list(list_id: int):
        await core.db.execute("DELETE FROM shopping_items WHERE list_id=?", (list_id,))
        await core.db.execute("DELETE FROM shopping_lists WHERE id=?", (list_id,))
        return {"status": "ok"}

    @api.post("/api/shopping/{list_id}/items", status_code=201)
    async def add_shopping_item(list_id: int, body: ShoppingItemIn):
        item = body.item.strip()[:80]
        if not item:
            raise HTTPException(400, "item is empty")
        row = await core.db.fetchone(
            "SELECT id FROM shopping_lists WHERE id=?", (list_id,))
        if not row:
            raise HTTPException(404, "no such list")
        iid = await core.db.execute(
            "INSERT INTO shopping_items(list_id, item) VALUES (?,?)", (list_id, item))
        return {"id": iid}

    @api.put("/api/shopping/items/{item_id}")
    async def set_shopping_item(item_id: int, body: ItemDoneIn):
        await core.db.execute(
            "UPDATE shopping_items SET done=? WHERE id=?", (int(body.done), item_id))
        return {"status": "ok"}

    @api.delete("/api/shopping/items/{item_id}")
    async def delete_shopping_item(item_id: int):
        await core.db.execute("DELETE FROM shopping_items WHERE id=?", (item_id,))
        return {"status": "ok"}

    @api.post("/api/notes", status_code=201)
    async def create_note(body: NoteIn):
        title = body.title.strip()[:80]
        content = body.content.strip()[:4000]
        if not title and not content:
            raise HTTPException(400, "a note needs a title or some content")
        if not title:
            title = content.splitlines()[0][:40]
        nid = await core.db.execute(
            "INSERT INTO notes(title, content) VALUES (?,?)", (title, content))
        return {"id": nid}

    @api.put("/api/notes/{note_id}")
    async def update_note(note_id: int, body: NoteIn):
        row = await core.db.fetchone("SELECT id FROM notes WHERE id=?", (note_id,))
        if not row:
            raise HTTPException(404, "no such note")
        await core.db.execute(
            "UPDATE notes SET title=?, content=?, updated_at=datetime('now') WHERE id=?",
            (body.title.strip()[:80], body.content.strip()[:4000], note_id))
        return {"status": "ok"}

    @api.delete("/api/notes/{note_id}")
    async def delete_note(note_id: int):
        await core.db.execute("DELETE FROM notes WHERE id=?", (note_id,))
        return {"status": "ok"}

    @api.delete("/api/timers/{timer_id}")
    async def cancel_timer(timer_id: int):
        return {"status": "ok", "message": await core.quick.cancel_timer(timer_id)}

    @api.get("/api/schedules")
    async def schedules():
        return rows_to_dicts(await core.db.fetchall(
            "SELECT * FROM schedules ORDER BY enabled DESC, next_run"))

    # ── OpenAI-compatible voice front door ───────────────────────────────
    # POST /v1/chat/completions: any STT→LLM→TTS pipeline can use Lity as its
    # "LLM". Lity is stateful: only the LAST user message of the request is
    # used (the Home thread, summary and memory are the real history), and
    # replies are sanitized to plain speakable text. Two kinds of background
    # notification, two behaviors on GET /v1/voice/pending:
    #   - task results (assistant messages the kernel relays) → returned in
    #     `messages`, spoken aloud by the voice client as before;
    #   - approvals (silent 'event' messages) → beep=true exactly once per
    #     new batch; the kernel explains and walks through them when asked.
    # Delivery state (cursor, beeped approvals) is core.voice — shared with
    # the in-process voicebot, so nothing is ever spoken twice. This endpoint
    # remains for REMOTE voice satellites; the local mic runs in-process.

    def _voice_auth(request: Request):
        key = os.environ.get("LITY_API_KEY", "")
        if key and request.headers.get("authorization", "") != f"Bearer {key}":
            raise HTTPException(401, "invalid API key")

    @api.get("/v1/models")
    async def models():
        return {"object": "list",
                "data": [{"id": "lity", "object": "model", "owned_by": "lity"}]}

    @api.get("/v1/voice/pending")
    async def voice_pending(request: Request):
        """`messages`: task results / kernel relays to speak aloud, as before.
        `beep`: true once per batch of NEW pending approvals — the client
        plays a beep instead of speech, and the user asks the kernel about it."""
        _voice_auth(request)
        texts = await core.voice.unheard()
        rows = await core.db.fetchall("SELECT id FROM approvals WHERE status='pending'")
        pending = {r["id"] for r in rows}
        beep = core.voice.approval_beep_poll(pending)
        return {"messages": texts, "beep": beep, "pending_approvals": len(pending)}

    @api.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        _voice_auth(request)
        body = await request.json()
        text = ""
        for m in reversed(body.get("messages") or []):
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, list):  # multimodal parts → text parts only
                    c = " ".join(p.get("text", "") for p in c if isinstance(p, dict)
                                 and p.get("type") == "text")
                text = (c or "").strip()
                break
        if not text:
            raise HTTPException(400, "no user message in request")

        # reply = this turn's answer plus any not-yet-spoken task results;
        # approval events never appear here (they only beep via the poll)
        reply = await core.voice.turn(text)

        cid, created = f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time())
        if not body.get("stream"):
            return {"id": cid, "object": "chat.completion", "created": created,
                    "model": "lity",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": reply}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

        def _chunk(delta: dict, finish=None) -> str:
            return "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": "lity",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]},
                ensure_ascii=False) + "\n\n"

        async def stream():
            # sentence-level chunks: TTS engines speak per sentence anyway
            yield _chunk({"role": "assistant"})
            for s in voice.sentences(reply):
                yield _chunk({"content": s})
            yield _chunk({}, finish="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ── SSE ───────────────────────────────────────────────────────────────
    @api.get("/api/events")
    async def events():
        q = core.bus.subscribe()

        async def stream():
            try:
                yield "data: {\"type\": \"connected\"}\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=25)
                        yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                core.bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    return api
