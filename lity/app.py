"""The App container — wires every subsystem together and owns their lifetime."""

import asyncio

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
from .sched.scheduler import Scheduler
from .skills import Skills


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
        self.scheduler = Scheduler(self)
        self._scheduler_task: asyncio.Task | None = None

    async def start(self):
        tools.load_all()
        await self.db.init()
        await self.approvals.load()
        # tasks that were mid-flight when the process died can never finish — say so
        await self.db.execute(
            "UPDATE tasks SET status='failed', result='interrupted by server restart', "
            "finished_at=datetime('now') WHERE status IN ('queued','running','waiting_user')")
        self._scheduler_task = asyncio.create_task(self.scheduler.run())

    async def stop(self):
        if self._scheduler_task:
            self._scheduler_task.cancel()
        await self.llm.close()
        await self.db.close()
