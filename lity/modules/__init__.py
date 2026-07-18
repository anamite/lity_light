"""External service modules — optional integrations beyond the kernel's local
quick tools (Google Calendar now; Spotify, email, … later follow the same
shape). A module:

  - has its own top-level config.yaml section (e.g. `gcal:`) which is RE-READ
    on every call via modules_cfg() below, so setup needs no restart;
  - exposes `configured`, `status()` and `setup_manual()` — the manual is a
    step-by-step text the KERNEL reads (via its tool's 'setup' action) and
    relays to the user, so setup instructions never bloat the system prompt;
  - registers one kernel tool in lity/tools/ like any other tool.
"""

import yaml

_cache: dict[str, tuple[float, dict]] = {}  # config path -> (mtime, parsed)


def modules_cfg(app, section: str) -> dict:
    """Live view of one top-level config.yaml section. Falls back to the
    boot-time config if the file is unreadable."""
    path = app.cfg.root / "config.yaml"
    try:
        mtime = path.stat().st_mtime
        cached = _cache.get(str(path))
        if not cached or cached[0] != mtime:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            _cache[str(path)] = (mtime, data)
        return _cache[str(path)][1].get(section) or {}
    except (OSError, yaml.YAMLError):
        return app.cfg.get(section) or {}
