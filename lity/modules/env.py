"""Environment module — Lity's senses (and hands) in the physical world.

A hub of small DRIVERS, each owning one slice of the environment:

  system         this machine's health: disk, memory, load, CPU temp (read-only)
  homeassistant  Home Assistant REST bridge — every entity HA knows (the Zigbee
                 mesh via ZHA/zigbee2mqtt, lights, switches, sensors, locks...)
                 becomes visible, and services can be called (turn things on/off)

Future drivers follow the same shape — zigbee2mqtt/MQTT direct, a desk motor,
a small display, an email watcher: implement enabled()/poll()/owns()/act()
and append to Environment.drivers. Nothing else in Lity changes.

The hub polls every driver on one interval and keeps the latest snapshot,
which feeds three places: the `environment` kernel tool, the heartbeat's
state block, and — for alerts and WATCHED state changes — system events that
wake the kernel so it can react on its own (announce lists are opt-in, so a
chatty sensor can't spam the kernel unless the user asks for it).

Config: `env:` in config.yaml, re-read live on every poll — no restart.
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

import httpx

from . import modules_cfg

log = logging.getLogger("lity.env")

ALERT_COOLDOWN_SECONDS = 3600   # the same alert fires at most once per hour
MAX_ENTITIES = 60               # snapshot cap — the kernel context is sacred

# entity-id prefixes observed when homeassistant.watch is left empty
DEFAULT_WATCH = ["light.", "switch.", "binary_sensor.", "lock.", "climate.",
                 "media_player.", "person.", "cover."]


def _match(entity_id: str, patterns: list[str]) -> bool:
    """A pattern is an exact entity id or a prefix ending in '.'."""
    return any(entity_id == p or (p.endswith(".") and entity_id.startswith(p))
               for p in patterns)


class SystemDriver:
    """This machine's own health. Pure stdlib, works headless, degrades
    gracefully where a metric doesn't exist (e.g. no thermal zone on a VM)."""

    name = "system"

    def __init__(self, hub):
        self.hub = hub

    def cfg(self) -> dict:
        return self.hub.cfg().get("system") or {}

    def enabled(self) -> bool:
        return bool(self.cfg().get("enabled", True))

    def owns(self, target: str) -> bool:
        return False  # read-only: no actuators

    async def act(self, target, action, data):
        return "The system driver is read-only."

    def capabilities(self) -> str:
        return "system: this machine's disk/memory/load/CPU-temperature (read-only)"

    async def poll(self):
        c = self.cfg()
        state, alerts = {}, []
        try:
            du = shutil.disk_usage(self.hub.app.cfg.root)
            pct = du.used * 100 // du.total
            state["disk"] = f"{pct}% used, {du.free // 2**30} GiB free"
            if pct >= int(c.get("disk_alert_percent", 90)):
                alerts.append(f"disk {pct}% full — only {du.free // 2**30} GiB left")
        except OSError:
            pass
        try:  # Linux only; silently absent elsewhere
            info = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0])  # kB
            avail, total = info.get("MemAvailable", 0), info.get("MemTotal", 1)
            state["memory"] = f"{(total - avail) * 100 // total}% used, " \
                              f"{avail // 1024} MiB available"
        except (OSError, ValueError, IndexError):
            pass
        try:
            load = os.getloadavg()
            state["load"] = f"{load[0]:.2f} {load[1]:.2f} {load[2]:.2f}"
        except (AttributeError, OSError):
            pass
        try:  # Pi CPU temperature
            millic = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())
            temp = millic / 1000
            state["cpu_temp"] = f"{temp:.0f}°C"
            if temp >= float(c.get("temp_alert_c", 80)):
                alerts.append(f"CPU temperature {temp:.0f}°C — running hot")
        except (OSError, ValueError):
            pass
        return state, alerts, []


class HomeAssistantDriver:
    """Home Assistant REST bridge. One long-lived access token (HA profile →
    security), no SDK. This is the road to the Zigbee mesh: every paired
    device surfaces as an HA entity, readable here and actionable via
    homeassistant.turn_on/turn_off/toggle (domain-agnostic services)."""

    name = "home"

    def __init__(self, hub):
        self.hub = hub
        self._prev: dict[str, str] | None = None  # last poll, for change detection

    def cfg(self) -> dict:
        return self.hub.cfg().get("homeassistant") or {}

    def _token(self) -> str:
        return os.environ.get(self.cfg().get("token_env") or "HASS_TOKEN", "")

    def enabled(self) -> bool:
        c = self.cfg()
        return bool(c.get("enabled")) and bool(c.get("base_url")) and bool(self._token())

    def owns(self, target: str) -> bool:
        return self.enabled() and "." in target

    def capabilities(self) -> str:
        return ("home: Home Assistant entities (Zigbee sensors, lights, switches, "
                "locks...) — observable, and switchable via env_act")

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=str(self.cfg().get("base_url")).rstrip("/"),
            headers={"Authorization": f"Bearer {self._token()}"}, timeout=10)

    async def poll(self):
        c = self.cfg()
        watch = [str(w) for w in (c.get("watch") or [])] or DEFAULT_WATCH
        announce = [str(a) for a in (c.get("announce") or [])]
        async with self._client() as cl:
            r = await cl.get("/api/states")
            r.raise_for_status()
            entities = r.json()
        state: dict[str, str] = {}
        for e in entities:
            eid = e.get("entity_id", "")
            if not _match(eid, watch) or len(state) >= MAX_ENTITIES:
                continue
            attrs = e.get("attributes") or {}
            val = str(e.get("state", "?"))
            unit = attrs.get("unit_of_measurement")
            if unit:
                val += f" {unit}"
            name = attrs.get("friendly_name")
            state[eid] = f"{val}" + (f" ({name})" if name and name != eid else "")

        changes = []
        if self._prev is not None:  # never announce on the first poll after boot
            for eid, val in state.items():
                old = self._prev.get(eid)
                if old is not None and old != val and _match(eid, announce):
                    changes.append(f"{eid}: {old} → {val}")
        self._prev = state
        return state, [], changes

    async def act(self, target, action, data):
        service = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}.get(action)
        if not service:
            return f"Unknown action '{action}' (use on / off / toggle)."
        payload = {"entity_id": target, **(data or {})}
        async with self._client() as cl:
            r = await cl.post(f"/api/services/homeassistant/{service}", json=payload)
            if r.status_code >= 400:
                return f"Home Assistant refused {service} for {target}: HTTP {r.status_code} {r.text[:200]}"
        return f"Done — called {service} on {target}."


class Environment:
    """The hub. One asyncio loop next to the scheduler; survives anything."""

    def __init__(self, app):
        self.app = app
        self.drivers = [SystemDriver(self), HomeAssistantDriver(self)]
        self.state: dict[str, dict[str, str]] = {}   # driver name -> {key: value}
        self._alert_ts: dict[str, float] = {}        # alert text -> last emit (monotonic)

    def cfg(self) -> dict:
        return modules_cfg(self.app, "env")

    async def run(self):
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("environment tick failed")
            await asyncio.sleep(max(5, int(self.cfg().get("poll_seconds", 60))))

    async def _tick(self):
        for d in self.drivers:
            if not d.enabled():
                self.state.pop(d.name, None)
                continue
            try:
                state, alerts, changes = await d.poll()
            except Exception as e:
                log.warning("env driver %s poll failed: %s", d.name, e)
                continue
            self.state[d.name] = state
            for a in alerts:
                self._emit(f"[env alert] {d.name}: {a}", cooldown=True)
            for ch in changes:
                self._emit(f"[env] {ch}", cooldown=False)

    def _emit(self, text: str, cooldown: bool):
        """Wake the kernel with an environment event. Alerts are cooled down so
        a full disk doesn't nag every poll; state changes are naturally deduped
        by change detection."""
        if cooldown:
            last = self._alert_ts.get(text)
            if last and time.monotonic() - last < ALERT_COOLDOWN_SECONDS:
                return
            self._alert_ts[text] = time.monotonic()
        asyncio.create_task(self.app.kernel.system_event(1, text))

    def snapshot_text(self, cap: int = 1500) -> str:
        """Latest observation of everything, for the tool and the heartbeat."""
        if not self.state:
            return ("(no environment drivers active — system driver polls shortly "
                    "after boot; Home Assistant needs env.homeassistant configured)")
        lines = []
        for name, st in self.state.items():
            lines.append(f"[{name}]")
            lines += [f"  {k}: {v}" for k, v in st.items()]
        return "\n".join(lines)[:cap]

    async def act(self, target: str, action: str, data: dict | None = None) -> str:
        for d in self.drivers:
            if d.owns(target):
                return await d.act(target, action, data)
        return (f"No active driver can act on '{target}'. Check `environment` for known "
                "entity ids; smart-home control needs env.homeassistant configured "
                "(base_url + HASS_TOKEN in .env + enabled: true).")

    def status(self) -> str:
        active = [d.name for d in self.drivers if d.enabled()]
        ha = self.drivers[1]
        if ha.enabled():
            ha_note = "Home Assistant CONNECTED"
        else:
            c = ha.cfg()
            missing = [m for m, ok in [
                ("env.homeassistant.enabled=true", bool(c.get("enabled"))),
                ("base_url", bool(c.get("base_url"))),
                (f"{c.get('token_env') or 'HASS_TOKEN'} in .env", bool(ha._token())),
            ] if not ok]
            ha_note = "Home Assistant off — missing: " + ", ".join(missing)
        return (f"Environment: drivers active [{', '.join(active) or 'none'}]; {ha_note}. "
                "Snapshot via the environment tool; devices switchable via env_act.")
