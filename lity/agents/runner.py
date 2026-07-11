"""Sub-agent runner: fresh context, own thread (visible as a sub-thread in
the UI), own budgets, runs in parallel. Only the compressed final result
travels back to the parent thread."""

import asyncio
import json
import platform
from types import SimpleNamespace

import httpx

from ..tools import ToolContext, expand_tools, openai_schema, run_tool
from .hermes_executor import HermesClient, classify, extract_output

COMPRESS_SYSTEM = ("Compress this sub-agent task result for the main agent. Keep: outcome, "
                   "file paths, key facts/numbers, anything the user must know. Max 150 words. "
                   "Output only the compressed result.")

PLATFORM_NOTE = (
    "Host: Windows. The shell tool runs cmd.exe — use dir/move/copy/del; bash, ls and "
    "heredocs (<<) do NOT work, and 'python' is not on PATH (use the python_run tool for "
    "scripts). Prefer the dedicated file tools (move_file, copy_file, delete_file) over shell."
    if platform.system() == "Windows" else
    f"Host: {platform.system()}. The shell tool runs POSIX sh. Prefer the dedicated file "
    "tools (move_file, copy_file, delete_file) over shell for file operations.")


class Runner:
    def __init__(self, app):
        self.app = app
        self._running: dict[int, asyncio.Task] = {}
        self.hermes = HermesClient(app.cfg)
        self.hermes_runs: dict[int, str] = {}   # task_id -> hermes run_id

    async def spawn(self, agent_name: str, task_text: str,
                    parent_thread_id: int, context_hint: str = "") -> tuple[int, int]:
        agent = self.app.agents.get(agent_name)  # raises KeyError for unknown agent
        thread_id = await self.app.db.create_thread(
            f"{agent.name}: {task_text[:48]}", kind="agent", parent_id=parent_thread_id)
        task_id = await self.app.db.execute(
            "INSERT INTO tasks(agent, thread_id, parent_thread_id, status, task) VALUES (?,?,?,?,?)",
            (agent.name, thread_id, parent_thread_id, "running", task_text))
        self.app.bus.emit("thread.created", thread_id=thread_id,
                          parent_id=parent_thread_id, title=f"{agent.name}: {task_text[:48]}")
        self.app.bus.emit("task.updated", task_id=task_id, agent=agent.name, status="running")

        run_fn = self._run_hermes if agent.executor == "hermes" else self._run
        t = asyncio.create_task(run_fn(agent, task_id, thread_id, parent_thread_id,
                                       task_text, context_hint))
        self._running[task_id] = t
        t.add_done_callback(lambda _: self._running.pop(task_id, None))
        return task_id, thread_id

    async def cancel(self, task_id: int) -> bool:
        t = self._running.get(task_id)
        if not t:
            return False
        t.cancel()
        await self.app.db.execute(
            "UPDATE tasks SET status='cancelled', finished_at=datetime('now') WHERE id=?", (task_id,))
        self.app.bus.emit("task.updated", task_id=task_id, status="cancelled")
        return True

    async def _run(self, agent, task_id, thread_id, parent_thread_id, task_text, hint):
        db, bus, cfg = self.app.db, self.app.bus, self.app.cfg
        try:
            agents_md = ""
            p = cfg.workspace / "AGENTS.md"
            if p.is_file():
                agents_md = p.read_text(encoding="utf-8")
            skills = await self.app.skills.recall(agent.name, task_text)
            skills_block = self.app.skills.as_prompt_block(skills)
            system = (f"{agent.prompt}\n\n{agents_md}\n\n{skills_block}\n\n{PLATFORM_NOTE}\n"
                      f"Budget: at most {agent.max_turns} turns. Work efficiently.")
            user = task_text + (f"\n\nContext from the main agent:\n{hint}" if hint else "")
            await db.add_message(thread_id, "user", user)
            bus.emit("message.created", thread_id=thread_id, role="user", content=user)

            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            tools = openai_schema(expand_tools(agent.tools))
            ctx = ToolContext(app=self.app, thread_id=thread_id, task_id=task_id,
                              level_cap=agent.level_cap, parent_thread_id=parent_thread_id)
            tokens_used = 0
            final = ""
            blocked = ""  # set when an approval is denied/expired → hard stop, no improvising
            transcript: list[str] = []  # tool-call trail, fed to the skill distiller

            for _ in range(agent.max_turns):
                msg, usage = await self.app.llm.chat(agent.model, messages, tools=tools,
                                                     max_tokens=4096)
                tokens_used += int(usage.get("total_tokens", 0) or 0)
                calls = msg.get("tool_calls") or []
                if not calls:
                    final = (msg.get("content") or "").strip()
                    break
                if msg.get("content"):
                    await db.add_message(thread_id, "assistant", msg["content"])
                    bus.emit("message.created", thread_id=thread_id,
                             role="assistant", content=msg["content"])
                messages.append(msg)
                for call in calls:
                    fn = call.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = await run_tool(ctx, fn.get("name", ""), args)
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                     "content": result})
                    line = f"{fn.get('name')}({str(args)[:150]}) → {result[:300]}"
                    transcript.append(line)
                    await db.add_message(thread_id, "event", line, tool_name=fn.get("name"))
                    bus.emit("message.created", thread_id=thread_id, role="event", content=line)
                    if result.startswith("Denied:"):
                        # the user's word (or silence) is final — the agent does NOT
                        # get to retry variants of a rejected action
                        blocked = f"`{fn.get('name')}` — {result[8:].strip()}"
                        break
                if blocked:
                    break
                if tokens_used > agent.max_tokens_total:
                    final = "(aborted: token budget exhausted before completion)"
                    break
            else:
                final = "(stopped: turn budget exhausted before completion)"

            if blocked:
                result = (f"Blocked: permission for {blocked}. Task stopped without completing — "
                          f"nothing was retried or improvised. Ask me to re-run it when you're "
                          f"ready to approve (or raise autonomy_level in settings).")
                await db.add_message(thread_id, "assistant", result)
                bus.emit("message.created", thread_id=thread_id, role="assistant", content=result)
                await db.execute(
                    "UPDATE tasks SET status='blocked', result=?, tokens_used=?, finished_at=datetime('now') WHERE id=?",
                    (result, tokens_used, task_id))
                bus.emit("task.updated", task_id=task_id, agent=agent.name, status="blocked")
                await self.app.kernel.system_event(
                    parent_thread_id, f"Task #{task_id} ({agent.name}) BLOCKED:\n{result}")
                return

            if final:
                await db.add_message(thread_id, "assistant", final)
                bus.emit("message.created", thread_id=thread_id, role="assistant", content=final)

            result = await self._compress(final or "(no result)")
            await db.execute(
                "UPDATE tasks SET status='done', result=?, tokens_used=?, finished_at=datetime('now') WHERE id=?",
                (result, tokens_used, task_id))
            bus.emit("task.updated", task_id=task_id, agent=agent.name, status="done")
            exhausted = final.startswith(("(aborted", "(stopped"))
            if not exhausted:
                # skill learning: distill what worked, off the result path
                asyncio.create_task(self.app.skills.distill(
                    agent.name, task_text, final, "\n".join(transcript[-15:]), task_id))
            # routing learning: teach the kernel which agent fits this kind of task
            asyncio.create_task(self.app.skills.distill_routing(
                agent.name, task_text, "budget-exhausted" if exhausted else "done",
                result, task_id))
            await self.app.kernel.system_event(
                parent_thread_id, f"Task #{task_id} ({agent.name}) finished:\n{result}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            await db.execute(
                "UPDATE tasks SET status='failed', result=?, finished_at=datetime('now') WHERE id=?",
                (f"{type(e).__name__}: {e}", task_id))
            bus.emit("task.updated", task_id=task_id, agent=agent.name, status="failed")
            await self.app.kernel.system_event(
                parent_thread_id, f"Task #{task_id} ({agent.name}) FAILED: {type(e).__name__}: {e}")

    # ── Hermes executor path ──────────────────────────────────────────────
    async def _run_hermes(self, agent, task_id, thread_id, parent_thread_id, task_text, hint):
        """Same lifecycle as _run, but execution happens on an external Hermes
        Agent (runs API). Progress events are mirrored into the task thread,
        Hermes approvals are bridged onto Lity approval cards, and the final
        GET /v1/runs/{id} poll is authoritative for status and output."""
        db, bus, cfg = self.app.db, self.app.bus, self.app.cfg
        run_id = ""
        try:
            if not self.hermes.enabled:
                raise RuntimeError("Hermes executor is disabled (hermes.enabled=false in config.yaml)")
            agents_md = ""
            p = cfg.workspace / "AGENTS.md"
            if p.is_file():
                agents_md = p.read_text(encoding="utf-8")
            user = task_text + (f"\n\nContext from the main agent:\n{hint}" if hint else "")
            await db.add_message(thread_id, "user", user)
            bus.emit("message.created", thread_id=thread_id, role="user", content=user)

            run_id = await self.hermes.create_run(user, f"lity-task-{task_id}", agents_md)
            self.hermes_runs[task_id] = run_id
            transcript: list[str] = []
            final, failed, tokens = "", "", 0

            try:
                async for evt in self.hermes.events(run_id):
                    kind, info = classify(evt)
                    if kind == "progress":
                        line = info["line"]
                        transcript.append(line)
                        await db.add_message(thread_id, "event", line, tool_name="hermes")
                        bus.emit("message.created", thread_id=thread_id, role="event", content=line)
                    elif kind == "approval":
                        await self._bridge_approval(info, run_id, agent.name,
                                                    task_id, thread_id, parent_thread_id)
                    elif kind == "final":
                        final = info["output"]
                        tokens = int((info["usage"] or {}).get("total_tokens", 0) or 0)
                    elif kind == "error":
                        failed = info["message"]
            except httpx.HTTPError:
                pass  # stream broke — the state poll below is authoritative

            row = await db.fetchone("SELECT status FROM tasks WHERE id=?", (task_id,))
            if row and row["status"] == "blocked":
                return  # a denied/expired approval already finalized this task

            if not final and not failed:
                state = await self.hermes.get_run(run_id)
                st = str(state.get("status", "")).lower()
                if st == "completed":
                    final = extract_output(state)
                elif st in ("failed", "error"):
                    failed = str(state.get("error") or "run failed")[:300]
                elif st in ("cancelled", "stopped"):
                    await asyncio.sleep(1.0)  # a deny/expiry finalizer may be mid-flight
                    row = await db.fetchone("SELECT status FROM tasks WHERE id=?", (task_id,))
                    if row and row["status"] == "blocked":
                        return
                    failed = "run was stopped before completing"
                tokens = tokens or int((state.get("usage") or {}).get("total_tokens", 0) or 0)
            if failed:
                raise RuntimeError(failed)

            if final:
                await db.add_message(thread_id, "assistant", final)
                bus.emit("message.created", thread_id=thread_id, role="assistant", content=final)
            result = await self._compress(final or "(no result)")
            await db.execute(
                "UPDATE tasks SET status='done', result=?, tokens_used=?, finished_at=datetime('now') WHERE id=?",
                (result, tokens, task_id))
            bus.emit("task.updated", task_id=task_id, agent=agent.name, status="done")
            asyncio.create_task(self.app.skills.distill(
                agent.name, task_text, final or result, "\n".join(transcript[-15:]), task_id))
            asyncio.create_task(self.app.skills.distill_routing(
                agent.name, task_text, "done", result, task_id))
            await self.app.kernel.system_event(
                parent_thread_id, f"Task #{task_id} ({agent.name}) finished:\n{result}")

        except asyncio.CancelledError:
            if run_id:
                try:
                    await self.hermes.stop(run_id)
                except Exception:
                    pass
            raise
        except Exception as e:
            row = await db.fetchone("SELECT status FROM tasks WHERE id=?", (task_id,))
            if row and row["status"] in ("blocked", "cancelled"):
                return  # already finalized by the approval/cancel path
            await db.execute(
                "UPDATE tasks SET status='failed', result=?, finished_at=datetime('now') WHERE id=?",
                (f"{type(e).__name__}: {e}", task_id))
            bus.emit("task.updated", task_id=task_id, agent=agent.name, status="failed")
            await self.app.kernel.system_event(
                parent_thread_id, f"Task #{task_id} ({agent.name}) FAILED: {type(e).__name__}: {e}")
        finally:
            self.hermes_runs.pop(task_id, None)

    async def _bridge_approval(self, info, run_id, agent_name, task_id, thread_id, parent_thread_id):
        """A Hermes run paused on a human decision → surface it as a normal
        Lity approval card (same UI, same waiting_user status, same nag)."""
        db, bus = self.app.db, self.app.bus
        args = dict(info.get("args") or {})
        args["_hermes_approval_id"] = info.get("approval_id")
        approval_id = await db.execute(
            "INSERT INTO approvals(tool, args_json, level, task_id, thread_id, run_id) "
            "VALUES (?,?,?,?,?,?)",
            (info["tool"], json.dumps(args, default=str), 3, task_id, thread_id, run_id))
        await db.execute(
            "UPDATE tasks SET status='waiting_user' WHERE id=? AND status='running'", (task_id,))
        bus.emit("task.updated", task_id=task_id, agent=agent_name, status="waiting_user")
        bus.emit("approval.requested", id=approval_id, tool=info["tool"],
                 args=args, level=3, thread_id=thread_id)
        ctx = SimpleNamespace(task_id=task_id, thread_id=thread_id,
                              user_thread_id=parent_thread_id)
        asyncio.create_task(self.app.approvals._nag(approval_id, info["tool"], ctx))
        asyncio.create_task(self._expire_hermes_approval(
            approval_id, run_id, agent_name, task_id, parent_thread_id))

    async def _expire_hermes_approval(self, approval_id, run_id, agent_name, task_id, parent_thread_id):
        """Hermes approvals have no local waiter, so expiry is a watchdog."""
        await asyncio.sleep(int(self.app.cfg.get_path("approval_timeout_seconds", 600)))
        row = await self.app.db.fetchone(
            "SELECT status, tool FROM approvals WHERE id=?", (approval_id,))
        if not row or row["status"] != "pending":
            return
        await self.app.db.execute(
            "UPDATE approvals SET status='expired', decided_at=datetime('now') WHERE id=?",
            (approval_id,))
        self.app.bus.emit("approval.resolved", id=approval_id, status="expired")
        await self.finalize_hermes_block(run_id, agent_name, task_id, parent_thread_id,
                                         f"`{row['tool']}` — approval request timed out")

    async def finalize_hermes_block(self, run_id, agent_name, task_id, parent_thread_id, reason):
        """Denied/expired approval on a Hermes run: stop the run, hard-stop the
        task — the same no-retry contract as the native runner."""
        try:
            await self.hermes.stop(run_id)
        except Exception:
            pass
        result = (f"Blocked: permission for {reason}. Task stopped without completing — "
                  f"nothing was retried or improvised. Ask me to re-run it when you're "
                  f"ready to approve (or raise autonomy_level in settings).")
        await self.app.db.execute(
            "UPDATE tasks SET status='blocked', result=?, finished_at=datetime('now') WHERE id=?",
            (result, task_id))
        self.app.bus.emit("task.updated", task_id=task_id, agent=agent_name, status="blocked")
        await self.app.kernel.system_event(
            parent_thread_id, f"Task #{task_id} ({agent_name}) BLOCKED:\n{result}")

    async def _compress(self, text: str) -> str:
        cap = int(self.app.cfg.get_path("subagents.result_max_chars", 1200))
        if len(text) <= cap:
            return text
        try:
            out = await self.app.llm.complete(
                self.app.cfg.get_path("models.utility"), COMPRESS_SYSTEM, text[:12000], max_tokens=400)
            return out or text[:cap]
        except Exception:
            return text[:cap] + "…"
