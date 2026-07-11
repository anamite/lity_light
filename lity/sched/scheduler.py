"""One loop, three duties: fire due timers/crons, run the heartbeat, and keep
MEMORY.md exported. Fired jobs enter their thread as system events; the
heartbeat uses only the cheap utility model and discards 'all clear' ticks."""

import asyncio
from datetime import datetime, timezone

from .crons import FMT, next_run

HEARTBEAT_SYSTEM = """You are the heartbeat of a personal agent. Below are the user's standing
checks (HEARTBEAT.md) and the current system state. If NOTHING needs attention, reply exactly:
HB_OK
Otherwise reply with one short message describing what needs attention (it will be posted to
the user's Home thread). Be conservative — only speak up when a check clearly triggers.
NEVER repeat something already covered by the 'ALREADY REPORTED' list — reply HB_OK instead."""


class Scheduler:
    def __init__(self, app):
        self.app = app
        self._last_heartbeat: datetime | None = None

    async def run(self):
        tick = int(self.app.cfg.get_path("scheduler.tick_seconds", 30))
        while True:
            try:
                await self._fire_due()
                await self._maybe_heartbeat()
            except Exception:
                pass  # the scheduler must survive anything
            await asyncio.sleep(await self._next_sleep(tick))

    async def _next_sleep(self, tick: int) -> float:
        """Sleep until the nearest due job (sub-minute schedules fire on time),
        capped at the normal tick when idle."""
        try:
            row = await self.app.db.fetchone(
                "SELECT MIN(next_run) AS nr FROM schedules WHERE enabled=1 AND next_run IS NOT NULL")
            if row and row["nr"]:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                until = (datetime.strptime(row["nr"], FMT) - now).total_seconds()
                return min(tick, max(0.5, until))
        except Exception:
            pass
        return tick

    async def _fire_due(self):
        now = datetime.now(timezone.utc).strftime(FMT)
        rows = await self.app.db.fetchall(
            "SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?", (now,))
        for r in rows:
            if r["kind"] == "timer":
                await self.app.db.execute(
                    "UPDATE schedules SET enabled=0, last_run=? WHERE id=?", (now, r["id"]))
            else:
                await self.app.db.execute(
                    "UPDATE schedules SET last_run=?, next_run=? WHERE id=?",
                    (now, next_run(r["spec"]), r["id"]))
            self.app.bus.emit("schedule.fired", schedule_id=r["id"], spec=r["spec"])
            asyncio.create_task(self.app.kernel.system_event(
                r["thread_id"], f"[scheduled job #{r['id']} fired — {r['spec']}] {r['prompt']}"))

    async def _maybe_heartbeat(self):
        cfg = self.app.cfg
        if not cfg.get_path("heartbeat.enabled", True):
            return
        interval = int(cfg.get_path("heartbeat.interval_minutes", 30)) * 60
        now = datetime.now(timezone.utc)
        if self._last_heartbeat and (now - self._last_heartbeat).total_seconds() < interval:
            return
        self._last_heartbeat = now

        hb_path = cfg.workspace / "HEARTBEAT.md"
        checks = hb_path.read_text(encoding="utf-8") if hb_path.is_file() else ""
        tasks = await self.app.db.fetchall(
            "SELECT id, agent, status, task, created_at FROM tasks "
            "WHERE status IN ('running','failed') ORDER BY id DESC LIMIT 10")
        sched = await self.app.db.fetchall(
            "SELECT id, spec, prompt, next_run FROM schedules WHERE enabled=1 LIMIT 10")
        reported = await self.app.db.fetchall(
            "SELECT content FROM messages WHERE thread_id=1 AND role='event' "
            "AND content LIKE '[heartbeat]%' ORDER BY id DESC LIMIT 5")
        state = ("ALREADY REPORTED (do not repeat):\n" +
                 "\n".join(f"- {r['content'][:120]}" for r in reported) +
                 "\n\nRUNNING/FAILED TASKS:\n" +
                 "\n".join(f"#{t['id']} {t['agent']} {t['status']} since {t['created_at']}: {t['task'][:80]}"
                           for t in tasks) + "\n\nSCHEDULES:\n" +
                 "\n".join(f"#{s['id']} {s['spec']} next {s['next_run']}: {s['prompt'][:60]}"
                           for s in sched) +
                 f"\n\nCURRENT TIME (UTC): {now.strftime(FMT)}")
        try:
            verdict = await self.app.llm.complete(
                cfg.get_path("models.utility"), HEARTBEAT_SYSTEM,
                f"{checks}\n\n---\n{state}", max_tokens=200)
        except Exception:
            return
        if verdict and not verdict.startswith("HB_OK"):
            await self.app.kernel.system_event(1, f"[heartbeat] {verdict}")

        # cheap housekeeping piggybacked on the tick
        await self.app.memory.export_md()
