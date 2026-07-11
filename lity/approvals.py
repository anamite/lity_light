"""Permission levels and the approval queue.

Levels: 0 read internal · 1 read outside world · 2 write in sandbox ·
3 execute · 4 dangerous/outward-facing (always asks).

Tools at or below `autonomy_level` auto-approve. Above it, execution suspends,
an approval card appears in the UI (SSE), and the tool waits for the decision.
"""

import asyncio
import json

from .db import dumps


class Approvals:
    def __init__(self, app):
        self.app = app
        self._waiters: dict[int, asyncio.Event] = {}
        self._always: set[str] = set()

    async def load(self):
        rows = await self.app.db.fetchall("SELECT DISTINCT tool FROM approvals WHERE status='always'")
        self._always = {r["tool"] for r in rows}
        # pending approvals from a previous process belong to dead tasks — expire them
        await self.app.db.execute(
            "UPDATE approvals SET status='expired', decided_at=datetime('now') WHERE status='pending'")

    async def gate(self, tool, args: dict, ctx) -> bool | str:
        """True to run, or a string reason for denial."""
        if tool.level > ctx.level_cap:
            return f"tool level {tool.level} exceeds this agent's cap {ctx.level_cap}"
        autonomy = int(self.app.cfg.get_path("autonomy_level", 2))
        if tool.level <= autonomy and tool.level < 4:
            return True
        if tool.name in self._always:
            return True

        approval_id = await self.app.db.execute(
            "INSERT INTO approvals(tool, args_json, level, task_id, thread_id) VALUES (?,?,?,?,?)",
            (tool.name, dumps(args), tool.level, ctx.task_id, ctx.thread_id),
        )
        ev = asyncio.Event()
        self._waiters[approval_id] = ev
        self.app.bus.emit("approval.requested", id=approval_id, tool=tool.name,
                          args=args, level=tool.level, thread_id=ctx.thread_id)
        nag_task = None
        if ctx.task_id:
            # the task board shows this task as waiting on the user, and if the
            # user doesn't react, the kernel gets nudged to chase them up
            await self.app.db.execute(
                "UPDATE tasks SET status='waiting_user' WHERE id=? AND status='running'",
                (ctx.task_id,))
            self.app.bus.emit("task.updated", task_id=ctx.task_id, status="waiting_user")
            nag_task = asyncio.create_task(self._nag(approval_id, tool.name, ctx))
        timeout = int(self.app.cfg.get_path("approval_timeout_seconds", 600))
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            await self.app.db.execute(
                "UPDATE approvals SET status='expired', decided_at=datetime('now') WHERE id=? AND status='pending'",
                (approval_id,))
            self.app.bus.emit("approval.resolved", id=approval_id, status="expired")
            return "approval request timed out"
        finally:
            self._waiters.pop(approval_id, None)
            if nag_task:
                nag_task.cancel()

        row = await self.app.db.fetchone("SELECT status FROM approvals WHERE id=?", (approval_id,))
        if row and row["status"] in ("approved", "always"):
            if ctx.task_id:  # back to work — the runner owns denied/expired outcomes
                await self.app.db.execute(
                    "UPDATE tasks SET status='running' WHERE id=? AND status='waiting_user'",
                    (ctx.task_id,))
                self.app.bus.emit("task.updated", task_id=ctx.task_id, status="running")
            return True
        return "user denied the request"

    async def _nag(self, approval_id: int, tool_name: str, ctx):
        """If a task's approval sits unanswered past approval_nag_seconds, wake
        the kernel in the parent thread so it can ask the user to take a look."""
        delay = int(self.app.cfg.get_path("approval_nag_seconds", 60))
        await asyncio.sleep(delay)
        row = await self.app.db.fetchone("SELECT status FROM approvals WHERE id=?", (approval_id,))
        if not row or row["status"] != "pending":
            return
        await self.app.kernel.system_event(
            ctx.user_thread_id,
            f"Task #{ctx.task_id} has been waiting over {delay}s for the user to approve "
            f"`{tool_name}`. Ask the user to check the approval card (task thread "
            f"#{ctx.thread_id}) — the task stays paused and will stop if the approval expires.")

    async def resolve(self, approval_id: int, decision: str) -> bool:
        if decision not in ("approve", "always", "deny"):
            return False
        status = {"approve": "approved", "always": "always", "deny": "denied"}[decision]
        row = await self.app.db.fetchone("SELECT * FROM approvals WHERE id=? AND status='pending'",
                                         (approval_id,))
        if not row:
            return False
        await self.app.db.execute(
            "UPDATE approvals SET status=?, decided_at=datetime('now') WHERE id=?",
            (status, approval_id))
        if row["run_id"]:
            # bridged from a Hermes run: forward the decision to the gateway
            # instead of waking a local waiter (there is none)
            return await self._resolve_hermes(row, status, approval_id)
        if status == "always":
            self._always.add(row["tool"])
        ev = self._waiters.get(approval_id)
        if ev:
            ev.set()
        self.app.bus.emit("approval.resolved", id=approval_id, status=status)
        return True

    async def _resolve_hermes(self, row, status: str, approval_id: int) -> bool:
        decision = "approve" if status in ("approved", "always") else "deny"
        try:
            args = json.loads(row["args_json"])
        except (json.JSONDecodeError, TypeError):
            args = {}
        runner = self.app.runner
        try:
            await runner.hermes.resolve_approval(
                row["run_id"], args.get("_hermes_approval_id"), decision)
        except Exception:
            pass  # on deny we stop the run below anyway; on approve the run
                  # state poll will reconcile if the forward failed
        self.app.bus.emit("approval.resolved", id=approval_id, status=status)
        task = await self.app.db.fetchone("SELECT * FROM tasks WHERE id=?", (row["task_id"],))
        if not task:
            return True
        if decision == "approve":
            await self.app.db.execute(
                "UPDATE tasks SET status='running' WHERE id=? AND status='waiting_user'",
                (task["id"],))
            self.app.bus.emit("task.updated", task_id=task["id"],
                              agent=task["agent"], status="running")
        else:
            await runner.finalize_hermes_block(
                row["run_id"], task["agent"], task["id"], task["parent_thread_id"],
                f"`{row['tool']}` — user denied the request")
        return True
