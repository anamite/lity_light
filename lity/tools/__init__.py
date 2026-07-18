"""Tool registry. Every tool carries a permission level (0–4); execution always
passes through the approval gate. Kernel and sub-agents draw from the same
registry but see different subsets."""

from dataclasses import dataclass, field
from typing import Any, Callable

REGISTRY: dict[str, "Tool"] = {}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    level: int
    handler: Callable  # async (ctx: ToolContext, args: dict) -> str
    direct_ok: bool = False  # may the model route this tool's output straight to the user?


@dataclass
class ToolContext:
    app: Any                     # the App container (db, cfg, llm, bus, ...)
    thread_id: int               # thread the tool call belongs to
    task_id: int | None = None   # set when running inside a sub-agent
    level_cap: int = 4           # sub-agent level_cap; kernel is uncapped (4)
    parent_thread_id: int | None = None  # where user-facing output goes for sub-agents

    @property
    def user_thread_id(self) -> int:
        return self.parent_thread_id or self.thread_id


def tool(name: str, description: str, parameters: dict, level: int, direct: bool = False):
    def deco(fn):
        REGISTRY[name] = Tool(name, description, parameters, level, fn, direct_ok=direct)
        return fn
    return deco


def params(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props)}


DIRECT_PARAM = {
    "type": "boolean",
    "description": "Set true to deliver this tool's raw output straight to the user as your "
                   "reply — no further model step. Use when the output is already user-ready. "
                   "Default false (output comes back to you as an intermediate step).",
}


def openai_schema(names: list[str]) -> list[dict]:
    out = []
    for n in names:
        t = REGISTRY.get(n)
        if not t:
            continue
        parameters = t.parameters
        if t.direct_ok:
            parameters = dict(parameters)
            parameters["properties"] = {**parameters.get("properties", {}),
                                        "direct_to_user": DIRECT_PARAM}
        out.append({"type": "function", "function": {
            "name": t.name, "description": t.description, "parameters": parameters}})
    return out


def load_all():
    """Import all tool modules so their @tool decorators register."""
    from . import core, gcal, quick  # noqa: F401


async def run_tool(ctx: ToolContext, name: str, args: dict) -> str:
    """Gate + execute. Always returns a string result for the model."""
    t = REGISTRY.get(name)
    if not t:
        return f"Error: unknown tool '{name}'."
    verdict = await ctx.app.approvals.gate(t, args, ctx)
    if verdict is not True:
        return f"Denied: {verdict}"
    try:
        return await t.handler(ctx, args)
    except Exception as e:  # tools must never crash the loop
        return f"Error running {name}: {type(e).__name__}: {e}"
