"""MCP client (Model Context Protocol) — the Hermes `mcp_tool.py` equivalent.

Servers are declared in config.yaml under `mcp.servers`; each one is a stdio
subprocess speaking JSON-RPC 2.0 over newline-delimited JSON. On connect, the
server's tools are registered into the global REGISTRY as '<server>_<tool>'
and become available to sub-agents through 'mcp:<server>' or 'mcp:*' entries
in their YAML tools list. No SDK dependency — pure asyncio subprocess, which
keeps the Raspberry Pi footprint at zero extra packages."""

import asyncio
import json
import os
import shutil

import yaml

from . import REGISTRY, Tool

PROTOCOL_VERSION = "2025-03-26"

# server name -> list of REGISTRY tool names it contributed (used by expand_tools)
MCP_TOOLS: dict[str, list[str]] = {}


def tools_for(selector: str) -> list[str]:
    """'mcp:*' → every MCP tool; 'mcp:<server>' → that server's tools."""
    if selector in ("*", ""):
        return [t for ts in MCP_TOOLS.values() for t in ts]
    return list(MCP_TOOLS.get(selector, []))


class MCPServer:
    """One stdio MCP server: subprocess + JSON-RPC 2.0, newline-delimited."""

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        # default 3 (execute): above the default autonomy_level, so external
        # services ask once — 'always' approval then persists per tool
        self.level = int(spec.get("level", 3))
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_tail: list[str] = []

    async def start(self) -> list[dict]:
        """Spawn, handshake, and return the server's tool list."""
        raw_cmd = str(self.spec.get("command", ""))
        cmd = shutil.which(raw_cmd) or raw_cmd  # resolves npx→npx.cmd on Windows
        args = [str(a) for a in self.spec.get("args", []) or []]
        env = {**os.environ, **{k: str(v) for k, v in (self.spec.get("env") or {}).items()}}
        self.proc = await asyncio.create_subprocess_exec(
            cmd, *args, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self._reader_task = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._drain_stderr())
        # first-run `npx`/`uvx` may download the server package — generous timeout
        await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "lity", "version": "1.0"},
        }, timeout=float(self.spec.get("startup_timeout", 180)))
        await self._notify("notifications/initialized", {})
        res = await self._request("tools/list", {})
        return res.get("tools", [])

    async def call(self, tool_name: str, args: dict) -> str:
        res = await self._request(
            "tools/call", {"name": tool_name, "arguments": args or {}},
            timeout=float(self.spec.get("call_timeout", 120)))
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            else:
                parts.append(f"[{c.get('type', 'non-text')} content omitted]")
        text = "\n".join(p for p in parts if p).strip() or "(empty result)"
        if res.get("isError"):
            return f"Error from {self.name}.{tool_name}: {text[:2000]}"
        return text[:8000]

    # ── JSON-RPC plumbing ─────────────────────────────────────────────────
    async def _request(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        self._id += 1
        rid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            msg = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            err = msg["error"]
            raise RuntimeError(f"{method}: {err.get('message', err)}")
        return msg.get("result") or {}

    async def _notify(self, method: str, params: dict):
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, obj: dict):
        if not self.proc or self.proc.returncode is not None:
            raise RuntimeError(f"MCP server '{self.name}' is not running"
                               + self._stderr_hint())
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def _read_loop(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # servers sometimes log junk to stdout — skip it
                if "id" in msg and "method" not in msg:      # response to us
                    fut = self._pending.get(msg["id"])
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif "id" in msg:                             # server→client request
                    await self._send({"jsonrpc": "2.0", "id": msg["id"],
                                      "error": {"code": -32601, "message": "not supported"}})
                # notifications are ignored
        except (asyncio.CancelledError, Exception):
            pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(
                    f"MCP server '{self.name}' disconnected" + self._stderr_hint()))

    async def _drain_stderr(self):
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                self._stderr_tail.append(line.decode(errors="replace").strip())
                del self._stderr_tail[:-20]
        except Exception:
            pass

    def _stderr_hint(self) -> str:
        return f" ({' | '.join(self._stderr_tail[-3:])})" if self._stderr_tail else ""

    async def stop(self):
        if self._reader_task:
            self._reader_task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()


class MCP:
    """Connects every configured server and registers its tools."""

    def __init__(self, app):
        self.app = app
        self.servers: dict[str, MCPServer] = {}

    def _servers_file(self):
        """Agent-added servers live here (config.yaml keeps its comments)."""
        return self.app.cfg.root / "mcp_servers.yaml"

    def _all_specs(self) -> dict:
        specs = dict(self.app.cfg.get_path("mcp.servers", {}) or {})
        f = self._servers_file()
        if f.is_file():
            specs.update(yaml.safe_load(f.read_text(encoding="utf-8")) or {})
        return specs

    async def start(self):
        # servers connect concurrently; a broken one never blocks the others
        await asyncio.gather(*(self._connect(n, s or {}) for n, s in self._all_specs().items()))

    async def add_server(self, name: str, spec: dict) -> list[str]:
        """Runtime onboarding: persist the server and hot-connect it (replacing
        any live connection of the same name). Returns the registered tool
        names; raises with the connect error otherwise."""
        f = self._servers_file()
        data = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.is_file() else {}
        data[name] = spec
        f.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

        old = self.servers.pop(name, None)
        if old:
            await old.stop()
        for t in MCP_TOOLS.pop(name, []):
            REGISTRY.pop(t, None)
        err = await self._connect(name, spec)
        if err:
            raise RuntimeError(err)
        return list(MCP_TOOLS.get(name, []))

    async def _connect(self, name: str, spec: dict) -> str | None:
        server = MCPServer(name, spec)
        try:
            tools = await server.start()
        except Exception as e:
            await server.stop()
            err = f"{type(e).__name__}: {e}"
            self.app.bus.emit("mcp.failed", server=name, error=err)
            return err
        self.servers[name] = server
        self._register(server, tools)
        self.app.bus.emit("mcp.connected", server=name, tools=len(MCP_TOOLS.get(name, [])))
        return None

    def _register(self, server: MCPServer, tools: list[dict]):
        names = []
        for t in tools:
            if not t.get("name"):
                continue
            tname = f"{server.name}_{t['name']}"
            schema = t.get("inputSchema") or {}
            if schema.get("type") != "object":
                schema = {"type": "object", "properties": schema.get("properties", {})}
            desc = (t.get("description") or t["name"]).strip()[:350]

            async def handler(ctx, args, _srv=server, _tn=t["name"]):
                return await _srv.call(_tn, args)

            REGISTRY[tname] = Tool(tname, f"[{server.name}] {desc}", schema,
                                   server.level, handler)
            names.append(tname)
        MCP_TOOLS[server.name] = names

    async def stop(self):
        for s in self.servers.values():
            await s.stop()
        self.servers.clear()
