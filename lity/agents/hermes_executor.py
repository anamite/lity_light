"""Hermes executor — runs a Lity task on an external Hermes Agent through its
runs API (`hermes gateway` with API_SERVER_ENABLED=true).

Division of labor: Lity keeps the task lifecycle (task board, approval cards,
waiting_user + nag, result compression, skill learning); Hermes supplies its
curated 70+ tool set. Contract mapping:

    runner.spawn(task)     → POST /v1/runs
    task-thread events     → GET  /v1/runs/{id}/events   (SSE)
    approval card          → POST /v1/runs/{id}/approval
    cancel_task            → POST /v1/runs/{id}/stop
    status/result          → GET  /v1/runs/{id}

Event schemas vary across Hermes versions, so `classify` recognizes events by
name patterns and the final GET /v1/runs/{id} poll is authoritative for
status and output."""

import json
import os

import httpx


class HermesClient:
    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get_path("hermes.enabled", False))

    @property
    def base(self) -> str:
        return str(self.cfg.get_path("hermes.base_url", "http://127.0.0.1:8642")).rstrip("/")

    def _headers(self) -> dict:
        key = os.environ.get(str(self.cfg.get_path("hermes.api_key_env", "HERMES_API_KEY")), "")
        return {"Authorization": f"Bearer {key}"}

    async def create_run(self, input_text: str, session_key: str, instructions: str = "") -> str:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base}/v1/runs",
                             headers={**self._headers(), "X-Hermes-Session-Key": session_key},
                             json={"input": input_text, "session_id": session_key,
                                   "instructions": instructions or None})
            r.raise_for_status()
            data = r.json()
            return str(data.get("id") or data.get("run_id"))

    async def events(self, run_id: str):
        """Yield parsed SSE event dicts until the stream closes."""
        timeout = httpx.Timeout(10, read=None)  # runs are long; reads must not time out
        async with httpx.AsyncClient(timeout=timeout) as c:
            async with c.stream("GET", f"{self.base}/v1/runs/{run_id}/events",
                                headers=self._headers()) as r:
                r.raise_for_status()
                event_name = ""
                async for line in r.aiter_lines():
                    line = line.strip()
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload in ("", "[DONE]"):
                            continue
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(evt, dict):
                            if event_name and not evt.get("type"):
                                evt["type"] = event_name
                            yield evt
                        event_name = ""

    async def get_run(self, run_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{self.base}/v1/runs/{run_id}", headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def stop(self, run_id: str):
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{self.base}/v1/runs/{run_id}/stop", headers=self._headers())

    async def resolve_approval(self, run_id: str, approval_id, decision: str):
        """Verified against api_server.py: body is {"choice": ...} with allowed
        values once|session|always|deny (server aliases approve/allow → once).
        One pending approval per run — addressed by run_id, approval_id unused."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{self.base}/v1/runs/{run_id}/approval",
                             headers=self._headers(),
                             json={"choice": decision})
            r.raise_for_status()


def extract_output(obj: dict) -> str:
    """Best-effort final text from a completion event or a run-state poll."""
    for key in ("output", "output_text", "text", "content", "response", "result"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            inner = extract_output(v)
            if inner:
                return inner
    return ""


def classify(evt: dict) -> tuple[str, dict]:
    """Map a run event onto Lity's lifecycle. Returns (kind, info) where kind
    is 'approval' | 'final' | 'error' | 'progress' | 'other'."""
    t = str(evt.get("type") or evt.get("event") or "").lower()
    status = str(evt.get("status") or "").lower()
    if "approval" in t or status in ("requires_approval", "awaiting_approval",
                                     "waiting_for_approval"):
        args = evt.get("arguments") or evt.get("args") or {}
        if evt.get("command") and isinstance(args, dict) and "command" not in args:
            args = {**args, "command": evt["command"]}  # pre-redacted by Hermes
        return "approval", {
            "approval_id": evt.get("approval_id") or evt.get("id"),
            "tool": str(evt.get("tool") or evt.get("tool_name") or evt.get("name")
                        or "hermes_action"),
            "args": args,
            "choices": [str(c) for c in (evt.get("choices") or [])
                        ] or ["once", "session", "always", "deny"],
        }
    if t.endswith((".completed", ".done")) or t in ("completed", "done") or status == "completed":
        return "final", {"output": extract_output(evt), "usage": evt.get("usage") or {}}
    if "fail" in t or "error" in t or status in ("failed", "error"):
        return "error", {"message": str(evt.get("error") or evt.get("message") or evt)[:300]}
    if "tool" in t:
        name = evt.get("name") or evt.get("tool") or "tool"
        args = evt.get("arguments") or evt.get("args") or ""
        out = evt.get("output") or evt.get("result") or ""
        line = f"{name}({str(args)[:150]})" + (f" → {str(out)[:300]}" if out else "")
        return "progress", {"line": line}
    return "other", {}
