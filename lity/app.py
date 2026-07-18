"""The App container — wires every subsystem together and owns their lifetime."""

import asyncio
import contextlib

from . import tools
from .agents.runner import Runner
from .approvals import Approvals
from .compactor import Compactor
from .config import Config
from .db import DB
from .gateway.events import EventBus
from .kernel import Kernel
from .llm import LLM
from .memory import Memory
from .modules.gcal import GoogleCalendar
from .modules.telegram import Telegram
from .quick import Quick
from .sched.scheduler import Scheduler
from .skills import Skills
from .voice import VoiceChannel


class App:
    def __init__(self, config_path: str = "config.yaml"):
        self.cfg = Config.load(config_path)
        self.bus = EventBus()
        self.db = DB(self.cfg.resolve("database", "./data/lity.db"))
        self.llm = LLM(self.cfg)
        self.memory = Memory(self)
        self.skills = Skills(self)
        self.compactor = Compactor(self)
        self.approvals = Approvals(self)
        self.runner = Runner(self)
        self.kernel = Kernel(self)
        self.quick = Quick(self)
        self.gcal = GoogleCalendar(self)
        self.telegram = Telegram(self)
        self.scheduler = Scheduler(self)
        self.voice = VoiceChannel(self)
        self._scheduler_task: asyncio.Task | None = None
        self._voice_task: asyncio.Task | None = None
        self._telegram_task: asyncio.Task | None = None

    async def start(self):
        tools.load_all()
        await self.db.init()
        await self.approvals.load()
        # tasks that were mid-flight when the process died can never finish — say so
        await self.db.execute(
            "UPDATE tasks SET status='failed', result='interrupted by server restart', "
            "finished_at=datetime('now') WHERE status IN ('queued','running','waiting_user')")
        await self.quick.start()  # restore pending timers/alarms, announce missed ones
        await self.voice.init_cursor()  # pre-boot history is never re-spoken
        self._scheduler_task = asyncio.create_task(self.scheduler.run())
        # idles until telegram: is configured (live config) — no restart needed
        self._telegram_task = asyncio.create_task(self.telegram.run())
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
        await self.quick.shutdown()
        await self.llm.close()
        await self.db.close()
