"""The App container — wires every subsystem together and owns their lifetime."""

import asyncio
import contextlib

from . import tools
from .agents.runner import Runner
from .approvals import Approvals
from .compactor import Compactor
from .config import Config
from .db import DB
from .embeddings import Embedder
from .gateway.events import EventBus
from .db import dumps
from .kernel import Kernel
from .llm import LLM
from . import logbuf
from .memory import Memory
from .modules.env import Environment
from .modules.gcal import GoogleCalendar
from .modules.telegram import Telegram
from .quick import Quick
from .reflect import Reflection
from .sched.scheduler import Scheduler
from .skills import Skills
from .voice import VoiceChannel


class App:
    def __init__(self, config_path: str = "config.yaml"):
        self.cfg = Config.load(config_path)
        self.bus = EventBus()
        self.logbuf = logbuf.LogBuffer()
        logbuf.attach(self.logbuf)
        # tap the event bus so bus traffic shows up in the live-log window too
        _orig_emit = self.bus.emit
        def _emit(type_, **payload):
            self.logbuf.add("EVENT", type_, dumps(payload)[:220])
            _orig_emit(type_, **payload)
        self.bus.emit = _emit
        self.db = DB(self.cfg.resolve("database", "./data/lity.db"))
        self.llm = LLM(self.cfg)
        self.llm.on_usage = self._record_llm_usage
        self.embedder = Embedder(self)
        self.memory = Memory(self)
        self.skills = Skills(self)
        self.compactor = Compactor(self)
        self.approvals = Approvals(self)
        self.runner = Runner(self)
        self.kernel = Kernel(self)
        self.quick = Quick(self)
        self.gcal = GoogleCalendar(self)
        self.telegram = Telegram(self)
        self.env = Environment(self)
        self.reflect = Reflection(self)
        self.scheduler = Scheduler(self)
        self.voice = VoiceChannel(self)
        self._scheduler_task: asyncio.Task | None = None
        self._voice_task: asyncio.Task | None = None
        self._telegram_task: asyncio.Task | None = None
        self._env_task: asyncio.Task | None = None
        self._embed_task: asyncio.Task | None = None

    async def _record_llm_usage(self, model: str, purpose: str, usage: dict):
        await self.db.execute(
            "INSERT INTO llm_usage(model, purpose, prompt_tokens, completion_tokens, total_tokens) "
            "VALUES (?,?,?,?,?)",
            (model, purpose or "", int(usage.get("prompt_tokens", 0) or 0),
             int(usage.get("completion_tokens", 0) or 0),
             int(usage.get("total_tokens", 0) or 0)))

    async def start(self):
        tools.load_all()
        await self.db.init()
        # usage accounting is kept 30 days, then dropped (internal stats only)
        await self.db.execute(
            "DELETE FROM llm_usage WHERE created_at < datetime('now','-30 days')")
        await self.approvals.load()
        # tasks that were mid-flight when the process died can never finish — say so
        await self.db.execute(
            "UPDATE tasks SET status='failed', result='interrupted by server restart', "
            "finished_at=datetime('now') WHERE status IN ('queued','running','waiting_user')")
        # loads the local embedding model + backfills memory vectors, off the boot path
        self._embed_task = asyncio.create_task(self.embedder.warmup())
        await self.quick.start()  # restore pending timers/alarms, announce missed ones
        await self.voice.init_cursor()  # pre-boot history is never re-spoken
        self._scheduler_task = asyncio.create_task(self.scheduler.run())
        # idles until telegram: is configured (live config) — no restart needed
        self._telegram_task = asyncio.create_task(self.telegram.run())
        # environment hub: polls drivers, feeds heartbeat, wakes kernel on alerts
        self._env_task = asyncio.create_task(self.env.run())
        if self.cfg.get_path("voice.enabled", False):
            from . import voicebot  # deferred: voice deps are optional
            self._voice_task = asyncio.create_task(voicebot.run(self))

    async def stop(self):
        if self._voice_task:
            self._voice_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._voice_task
        if self._scheduler_task:
            self._scheduler_task.cancel()
        if self._telegram_task:
            self._telegram_task.cancel()
        if self._env_task:
            self._env_task.cancel()
        if self._embed_task:
            self._embed_task.cancel()
        await self.quick.shutdown()
        await self.llm.close()
        await self.db.close()
