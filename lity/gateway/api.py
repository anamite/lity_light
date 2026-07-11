"""FastAPI gateway: a thin REST + SSE front door over the kernel. The HTML UI
is the default channel; Telegram/Discord/etc. can be added later as peers."""

import asyncio
import json
import mimetypes
import re
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

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


class AgentIn(BaseModel):
    name: str
    yaml: str
    prompt: str


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
        agents_dir = core.cfg.resolve("agents_dir", "./agents")
        files = {n: (ws / n).read_text(encoding="utf-8") if (ws / n).is_file() else ""
                 for n in EDITABLE_FILES}
        agents = []
        for yml in sorted(agents_dir.glob("*.yaml")):
            data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            prompt_file = agents_dir / data.get("prompt", f"prompts/{yml.stem}.md")
            agents.append({"name": yml.stem, "yaml": yml.read_text(encoding="utf-8"),
                           "prompt": prompt_file.read_text(encoding="utf-8")
                           if prompt_file.is_file() else ""})
        tools = [{"name": t.name, "description": t.description, "level": t.level,
                  "direct_ok": t.direct_ok} for t in sorted(REGISTRY.values(),
                                                            key=lambda t: (t.level, t.name))]
        return {"config_yaml": (core.cfg.root / "config.yaml").read_text(encoding="utf-8"),
                "files": files, "agents": agents, "tools": tools}

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
        core.agents.load()             # default_agent model may have changed
        return {"status": "ok"}

    @api.put("/api/settings/file")
    async def save_workspace_file(body: FileIn):
        if body.name not in EDITABLE_FILES:
            raise HTTPException(400, f"editable files: {EDITABLE_FILES}")
        (core.cfg.workspace / body.name).write_text(body.content, encoding="utf-8")
        return {"status": "ok"}

    @api.put("/api/settings/agent")
    async def save_agent(body: AgentIn):
        if not re.fullmatch(r"[a-z0-9_\-]+", body.name):
            raise HTTPException(400, "agent name must be lowercase alphanumeric")
        try:
            data = yaml.safe_load(body.yaml)
            assert isinstance(data, dict) and data.get("name")
        except Exception as e:
            raise HTTPException(400, f"invalid agent YAML: {e}")
        agents_dir = core.cfg.resolve("agents_dir", "./agents")
        (agents_dir / f"{body.name}.yaml").write_text(body.yaml, encoding="utf-8")
        prompt_rel = data.get("prompt", f"prompts/{body.name}.md")
        prompt_path = agents_dir / prompt_rel
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(body.prompt, encoding="utf-8")
        core.agents.load()
        return {"status": "ok", "agents": core.agents.names()}

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

    @api.get("/api/schedules")
    async def schedules():
        return rows_to_dicts(await core.db.fetchall(
            "SELECT * FROM schedules ORDER BY enabled DESC, next_run"))

    @api.get("/api/agents")
    async def agents():
        return [{"name": a.name, "description": a.description, "model": a.model}
                for a in core.agents.all()]

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
