"""YAML config loader. One file (config.yaml) drives everything."""

from pathlib import Path

import yaml


class Config(dict):
    root: Path

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        p = Path(path).resolve()
        with open(p, encoding="utf-8") as f:
            cfg = cls(yaml.safe_load(f) or {})
        cfg.root = p.parent
        return cfg

    def get_path(self, dotted: str, default=None):
        cur = self
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def resolve(self, dotted: str, default: str) -> Path:
        """Resolve a config path value relative to the config file's directory."""
        return (self.root / str(self.get_path(dotted, default))).resolve()

    @property
    def workspace(self) -> Path:
        return self.resolve("workspace", "./workspace")
