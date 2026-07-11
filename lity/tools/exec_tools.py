"""Execution tools for sub-agents: Python, shell, and workspace-jailed file I/O."""

import asyncio
import platform
import shutil
import sys
import time
from pathlib import Path

from . import params, tool

MAX_OUTPUT = 6000
IS_WINDOWS = platform.system() == "Windows"
SHELL_DESC = (
    "Run a cmd.exe command in the workspace (Windows host: dir/move/copy/del, NOT bash/ls; "
    "heredocs (<<) do not work; 'python' is not on PATH — use python_run for scripts). 180s timeout."
    if IS_WINDOWS else
    "Run a POSIX shell command in the workspace. 180s timeout.")


def _jail(ctx, rel: str) -> Path:
    ws = ctx.app.cfg.workspace
    p = (ws / rel).resolve()
    if not p.is_relative_to(ws):
        raise PermissionError(f"path escapes the workspace: {rel}")
    return p


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n…[truncated, {len(text)} chars total]"


async def _run_subprocess(cmd, cwd: Path, timeout: int) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"Timed out after {timeout}s."
    text = out.decode(errors="replace")
    return _clip(text or "(no output)") + (f"\n[exit code {proc.returncode}]" if proc.returncode else "")


@tool("python_run", "Run a Python script (saved to the workspace first) and return its output. 120s timeout.",
      params({"code": {"type": "string"},
              "filename": {"type": "string", "description": "optional script name, e.g. analyze.py"}},
             required=["code"]), level=3)
async def python_run(ctx, args):
    name = args.get("filename") or f"run_{int(time.time())}.py"
    path = _jail(ctx, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["code"], encoding="utf-8")
    out = await _run_subprocess(f'"{sys.executable}" "{path}"', ctx.app.cfg.workspace, 120)
    return f"[saved to {name}]\n{out}"


@tool("shell", SHELL_DESC,
      params({"command": {"type": "string"}}), level=3)
async def shell(ctx, args):
    return await _run_subprocess(args["command"], ctx.app.cfg.workspace, 180)


@tool("move_file", "Move or rename a file inside the workspace (binary-safe; use this instead of "
      "shell for moves). Creates target directories as needed.",
      params({"src": {"type": "string"}, "dst": {"type": "string"},
              "overwrite": {"type": "boolean", "description": "replace an existing target (default false)"}},
             required=["src", "dst"]), level=2)
async def move_file(ctx, args):
    src, dst = _jail(ctx, args["src"]), _jail(ctx, args["dst"])
    if not src.is_file():
        return f"Error: no such file '{args['src']}'."
    replaced = dst.is_file()
    if replaced and not args.get("overwrite"):
        return f"Error: '{args['dst']}' already exists. Pass overwrite=true to replace it."
    dst.parent.mkdir(parents=True, exist_ok=True)
    if replaced:
        dst.unlink()
    shutil.move(src, dst)
    rel = dst.relative_to(ctx.app.cfg.workspace)
    return f"Moved '{args['src']}' → '{rel}'" + (" (replaced existing file)." if replaced else ".")


@tool("copy_file", "Copy a file inside the workspace (binary-safe). Creates target directories as needed.",
      params({"src": {"type": "string"}, "dst": {"type": "string"},
              "overwrite": {"type": "boolean", "description": "replace an existing target (default false)"}},
             required=["src", "dst"]), level=2)
async def copy_file(ctx, args):
    src, dst = _jail(ctx, args["src"]), _jail(ctx, args["dst"])
    if not src.is_file():
        return f"Error: no such file '{args['src']}'."
    replaced = dst.is_file()
    if replaced and not args.get("overwrite"):
        return f"Error: '{args['dst']}' already exists. Pass overwrite=true to replace it."
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    rel = dst.relative_to(ctx.app.cfg.workspace)
    return f"Copied '{args['src']}' → '{rel}'" + (" (replaced existing file)." if replaced else ".")


@tool("delete_file", "Delete a single file from the workspace.",
      params({"path": {"type": "string"}}), level=3)
async def delete_file(ctx, args):
    p = _jail(ctx, args["path"])
    if not p.is_file():
        return f"Error: no such file '{args['path']}'."
    p.unlink()
    return f"Deleted '{args['path']}'."


@tool("read_file", "Read a file from the workspace.",
      params({"path": {"type": "string"}}), level=0, direct=True)
async def read_file(ctx, args):
    return _clip(_jail(ctx, args["path"]).read_text(encoding="utf-8", errors="replace"))


@tool("write_file", "Write/overwrite a file in the workspace.",
      params({"path": {"type": "string"}, "content": {"type": "string"}}), level=2)
async def write_file(ctx, args):
    p = _jail(ctx, args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"], encoding="utf-8")
    return f"Wrote {len(args['content'])} chars to {args['path']}."


@tool("list_files", "List workspace files (optionally under a subdirectory).",
      params({"path": {"type": "string", "description": "subdirectory, default root"}}, required=[]),
      level=0)
async def list_files(ctx, args):
    base = _jail(ctx, args.get("path", "."))
    if not base.exists():
        return "Directory does not exist."
    lines = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            rel = p.relative_to(ctx.app.cfg.workspace)
            lines.append(f"{rel}  ({p.stat().st_size} B)")
        if len(lines) >= 200:
            lines.append("…[truncated]")
            break
    return "\n".join(lines) or "(empty)"
