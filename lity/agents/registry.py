"""Sub-agent registry: every YAML in agents_dir defines one agent.
Adding a capability = adding a YAML + a prompt file. No code."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class AgentDef:
    name: str
    description: str
    model: str
    prompt: str                    # full system prompt text (loaded)
    tools: list[str] = field(default_factory=list)
    max_turns: int = 20
    max_tokens_total: int = 150000
    level_cap: int = 3
    executor: str = "native"       # native = Lity's own loop | hermes = external Hermes run


class AgentRegistry:
    def __init__(self, cfg):
        self.cfg = cfg
        self._agents: dict[str, AgentDef] = {}

    def load(self):
        agents_dir = self.cfg.resolve("agents_dir", "./agents")
        default_model = self.cfg.get_path("models.default_agent")
        self._agents.clear()
        hermes_on = bool(self.cfg.get_path("hermes.enabled", False))
        for path in sorted(Path(agents_dir).glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if data.get("executor") == "hermes" and not hermes_on:
                continue  # hidden from the kernel until the Hermes gateway is configured
            prompt_path = agents_dir / data.get("prompt", "")
            prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else \
                f"You are the {data.get('name')} sub-agent. Complete the task and report concisely."
            a = AgentDef(
                name=data["name"],
                description=data.get("description", data["name"]),
                model=data.get("model") or default_model,
                prompt=prompt,
                tools=data.get("tools", []),
                max_turns=int(data.get("max_turns", 20)),
                max_tokens_total=int(data.get("max_tokens_total", 150000)),
                level_cap=int(data.get("level_cap", 3)),
                executor=str(data.get("executor", "native")),
            )
            self._agents[a.name] = a

    def get(self, name: str) -> AgentDef:
        return self._agents[name]

    def all(self) -> list[AgentDef]:
        return list(self._agents.values())

    def names(self) -> list[str]:
        return list(self._agents)
