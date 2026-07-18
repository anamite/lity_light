"""Google Calendar module — service-account REST client, no Google SDK.

Auth: the service account's private key signs a JWT (PyJWT + cryptography);
one POST to Google's token endpoint yields a ~1h access token (cached). The
user shares their calendar with the service account's email ONCE — after
that there is no login flow ever, which is what a headless Pi needs.

Config lives under `gcal:` in config.yaml and is re-read live (modules_cfg),
so enabling or re-pointing the module needs no restart. The daily agenda for
the kernel's system prompt is cached for CACHE_SECONDS and invalidated by
every write, so injection does not mean one API call per kernel turn."""

import json
import logging
import time as _time
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx

from . import modules_cfg
from ..quick import parse_duration

log = logging.getLogger("lity.gcal")

SCOPE = "https://www.googleapis.com/auth/calendar"
API = "https://www.googleapis.com/calendar/v3"
CACHE_SECONDS = 600
DEPS_HINT = ("The gcal module needs its auth libraries — run in the Lity venv: "
             "pip install -r requirements-modules.txt  (PyJWT + cryptography).")


class GoogleCalendar:
    def __init__(self, app):
        self.app = app
        self._token: tuple[float, str] | None = None      # (expiry, access_token)
        self._sys_cache: tuple[float, str] | None = None  # (expiry, block)

    # ── config / status ─────────────────────────────────────────────────────
    def cfg(self) -> dict:
        return modules_cfg(self.app, "gcal")

    def key_path(self):
        rel = self.cfg().get("service_account_file") or "data/gcal_service_account.json"
        return (self.app.cfg.root / str(rel)).resolve()

    @property
    def configured(self) -> bool:
        c = self.cfg()
        return bool(c.get("enabled")) and bool(str(c.get("calendar_id") or "").strip()) \
            and self.key_path().is_file()

    def client_email(self) -> str:
        try:
            return json.loads(self.key_path().read_text(encoding="utf-8")).get("client_email", "")
        except (OSError, json.JSONDecodeError):
            return ""

    def _deps_ok(self) -> bool:
        try:
            import jwt  # noqa: F401
            return True
        except ImportError:
            return False

    def status(self) -> str:
        c = self.cfg()
        checks = [
            ("gcal.enabled is true in config.yaml", bool(c.get("enabled"))),
            (f"service account key file exists at {self.key_path()}", self.key_path().is_file()),
            ("gcal.calendar_id is set (the user's gmail address)",
             bool(str(c.get("calendar_id") or "").strip())),
            ("python deps installed (PyJWT + cryptography)", self._deps_ok()),
        ]
        missing = [name for name, ok in checks if not ok]
        if not missing:
            email = self.client_email()
            return ("Google Calendar: READY (calendar "
                    f"{c.get('calendar_id')}, service account {email or 'unknown'}, "
                    f"daily agenda injection: {c.get('inject_daily') or 'always'}). "
                    "If calls fail with 'not found', the calendar isn't shared with "
                    "the service account yet.")
        return ("Google Calendar: NOT ready — still missing: "
                + "; ".join(missing)
                + ". The calendar tool's setup action has the full manual.")

    def setup_manual(self) -> str:
        email = self.client_email()
        return f"""GOOGLE CALENDAR — SETUP MANUAL (internal: read this, then guide the user ONE step at a time in your own words; never dump all steps at once. Steps 2, 4 and 5 can be delegated to Hermes if the user prefers.)

How it works: Lity uses a Google SERVICE ACCOUNT — a robot Google account with its own email. The user creates it once in a browser, saves its key file on the Lity machine, and shares their calendar with the robot's email. No login flow on the Lity machine, and config is re-read live: NO RESTART needed.

Step 1 — create the key (user, in a browser):
  a. console.cloud.google.com — create or pick any project.
  b. APIs & Services > Library — search "Google Calendar API" — Enable.
  c. APIs & Services > Credentials > Create credentials > Service account — any name, skip optional screens.
  d. Open the new service account > Keys > Add key > Create new key > JSON — a .json key file downloads.

Step 2 — put that file on the Lity machine at exactly: {self.key_path()}
  Options: scp/copy it there themselves, OR the user pastes the JSON content into the chat and you delegate to Hermes: "create the file {self.key_path()} with this exact content: …".

Step 3 — share the calendar with the robot:
  The key file's "client_email" is{f" {email}" if email else " inside the .json (…@…iam.gserviceaccount.com)"}. In calendar.google.com > Settings > (their calendar) > "Share with specific people or groups" > add that email with permission "Make changes to events".

Step 4 — config (user runs on the Lity machine, or delegate to Hermes):
  ./lityctl set gcal.enabled true
  ./lityctl set gcal.calendar_id THEIR_GMAIL_ADDRESS
  (optional: ./lityctl set gcal.inject_daily on_demand — default 'always' puts today's agenda in your context every turn)

Step 5 — python deps, once, in the Lity venv (delegatable to Hermes):
  pip install -r requirements-modules.txt

Step 6 — verify: call calendar(action='agenda', day='today'). Errors name the step to fix. No restart is required at any point.

CURRENT STATUS: {self.status()}"""

    # ── auth ────────────────────────────────────────────────────────────────
    async def _access_token(self) -> str:
        if self._token and self._token[0] > _time.time():
            return self._token[1]
        try:
            import jwt
        except ImportError:
            raise ValueError(DEPS_HINT)
        try:
            info = json.loads(self.key_path().read_text(encoding="utf-8"))
            now = int(_time.time())
            assertion = jwt.encode(
                {"iss": info["client_email"], "scope": SCOPE, "aud": info["token_uri"],
                 "iat": now, "exp": now + 3600},
                info["private_key"], algorithm="RS256")
        except (OSError, KeyError, ValueError) as e:
            raise ValueError(f"Service account key file at {self.key_path()} is missing or "
                             f"not a valid Google JSON key ({e}). Redo setup step 1-2.")
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.post(info["token_uri"], data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion})
        if r.status_code != 200:
            raise ValueError(f"Google rejected the service account key "
                             f"(HTTP {r.status_code}: {r.text[:150]}). The key may be "
                             "revoked — create a fresh JSON key (setup step 1d).")
        tok = r.json()
        self._token = (_time.time() + int(tok.get("expires_in", 3600)) - 60,
                       tok["access_token"])
        return self._token[1]

    async def _call(self, method: str, path: str, params=None, body=None):
        token = await self._access_token()
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.request(method, API + path, params=params, json=body,
                                  headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 404:
            raise ValueError("Google says: calendar or event not found. Check that "
                             "gcal.calendar_id is the user's gmail address AND that the "
                             "calendar is shared with the service account "
                             f"({self.client_email()}) — setup step 3.")
        if r.status_code in (401, 403):
            raise ValueError(f"Google denied access (HTTP {r.status_code}). Usually the "
                             "calendar is shared read-only — the share permission must be "
                             "'Make changes to events' (setup step 3).")
        if r.status_code >= 400:
            raise ValueError(f"Google Calendar API error HTTP {r.status_code}: {r.text[:150]}")
        return r.json() if r.content else {}

    def _cal(self) -> str:
        return quote(str(self.cfg().get("calendar_id") or "").strip(), safe="")

    # ── time helpers ────────────────────────────────────────────────────────
    def _tz(self):
        name = str(self.cfg().get("timezone") or "").strip()
        if name:
            try:
                from zoneinfo import ZoneInfo
                return ZoneInfo(name)
            except Exception:
                log.warning(f"gcal.timezone {name!r} unknown — using system tz")
        return datetime.now().astimezone().tzinfo

    def _day_bounds(self, day: str | None):
        d = (day or "today").strip().lower()
        tz = self._tz()
        today = datetime.now(tz).date()
        if d in ("", "today"):
            target = today
        elif d == "tomorrow":
            target = today + timedelta(days=1)
        elif d == "yesterday":
            target = today - timedelta(days=1)
        else:
            try:
                target = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                raise ValueError("Give the day as 'today', 'tomorrow', 'yesterday' "
                                 "or 'YYYY-MM-DD'.")
        start = datetime(target.year, target.month, target.day, tzinfo=tz)
        return target, start, start + timedelta(days=1)

    def _parse_start(self, start: str):
        """'YYYY-MM-DD HH:MM' | 'HH:MM' (today) | 'YYYY-MM-DD' (all-day).
        Returns (datetime|None, date|None) — exactly one is set."""
        s = str(start or "").strip()
        tz = self._tz()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=tz), None
            except ValueError:
                pass
        try:
            t = datetime.strptime(s, "%H:%M")
            now = datetime.now(tz)
            return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0), None
        except ValueError:
            pass
        try:
            return None, datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Give the start as 'YYYY-MM-DD HH:MM', 'HH:MM' (today), "
                             "or 'YYYY-MM-DD' for an all-day event.")

    # ── formatting ──────────────────────────────────────────────────────────
    def _fmt(self, ev: dict, n: int | None = None) -> str:
        s, e = ev.get("start") or {}, ev.get("end") or {}
        if "date" in s:
            when = "all day"
        else:
            try:
                st = datetime.fromisoformat(s.get("dateTime", "")).astimezone(self._tz())
                en = datetime.fromisoformat(e.get("dateTime", "")).astimezone(self._tz())
                when = f"{st:%H:%M} to {en:%H:%M}"
            except ValueError:
                when = "?"
        line = f"{when}: {ev.get('summary') or '(no title)'}"
        extras = [x for x in (ev.get("location"),
                              (ev.get("description") or "").strip()[:80] or None) if x]
        if extras:
            line += " — " + "; ".join(extras)
        return f"{n}. {line}" if n else line

    # ── operations (all return speakable strings) ───────────────────────────
    async def _events(self, day: str | None):
        d, t0, t1 = self._day_bounds(day)
        data = await self._call("GET", f"/calendars/{self._cal()}/events", params={
            "timeMin": t0.isoformat(), "timeMax": t1.isoformat(),
            "singleEvents": "true", "orderBy": "startTime", "maxResults": "25"})
        return d, data.get("items", [])

    async def agenda(self, day: str | None) -> str:
        d, items = await self._events(day)
        head = f"{d:%A %Y-%m-%d}"
        if not items:
            return f"{head}: no events."
        lines = [self._fmt(ev, i) for i, ev in enumerate(items, 1)]
        return f"{head}: {len(items)} event{'s' if len(items) != 1 else ''}.\n" + "\n".join(lines)

    async def _resolve(self, day: str | None, ref) -> dict:
        d, items = await self._events(day)
        if not items:
            raise ValueError(f"No events on {d:%Y-%m-%d} to match.")
        r = str(ref or "").strip()
        if not r:
            raise ValueError("Which event? Give its number or title from the agenda.")
        if r.isdigit() and 1 <= int(r) <= len(items):
            return items[int(r) - 1]
        matches = [ev for ev in items if r.lower() in (ev.get("summary") or "").lower()]
        if len(matches) == 1:
            return matches[0]
        listing = "\n".join(self._fmt(ev, i) for i, ev in enumerate(items, 1))
        raise ValueError((f"Several events match '{r}'" if matches else f"No event matches '{r}'")
                         + f" on {d:%Y-%m-%d} — pick a number:\n{listing}")

    def _time_body(self, start: str, duration: str | None) -> dict:
        dt, allday = self._parse_start(start)
        if allday:
            return {"start": {"date": allday.isoformat()},
                    "end": {"date": (allday + timedelta(days=1)).isoformat()}}
        secs = parse_duration(duration) if duration else 3600
        return {"start": {"dateTime": dt.isoformat()},
                "end": {"dateTime": (dt + timedelta(seconds=secs)).isoformat()}}

    async def add_event(self, title, start, duration=None, details=None, location=None) -> str:
        if not str(title or "").strip():
            raise ValueError("The event needs a title.")
        if not str(start or "").strip():
            raise ValueError("The event needs a start ('YYYY-MM-DD HH:MM', 'HH:MM' for "
                             "today, or 'YYYY-MM-DD' for all-day).")
        body = {"summary": str(title).strip(), **self._time_body(start, duration)}
        if details:
            body["description"] = str(details)
        if location:
            body["location"] = str(location)
        ev = await self._call("POST", f"/calendars/{self._cal()}/events", body=body)
        self._sys_cache = None
        return f"Added to the calendar — {self._fmt(ev)}."

    async def update_event(self, day, ref, title=None, start=None, duration=None,
                           details=None, location=None) -> str:
        ev = await self._resolve(day, ref)
        body = {}
        if title:
            body["summary"] = str(title).strip()
        if details is not None and str(details).strip():
            body["description"] = str(details)
        if location is not None and str(location).strip():
            body["location"] = str(location)
        if start:
            body.update(self._time_body(start, duration))
        elif duration:
            s = (ev.get("start") or {}).get("dateTime")
            if not s:
                raise ValueError("That's an all-day event — give a start time too.")
            dt = datetime.fromisoformat(s)
            body["end"] = {"dateTime": (dt + timedelta(seconds=parse_duration(duration))).isoformat()}
        if not body:
            raise ValueError("Nothing to change — give a new title, start, duration, "
                             "details or location.")
        upd = await self._call("PATCH", f"/calendars/{self._cal()}/events/{ev['id']}", body=body)
        self._sys_cache = None
        return f"Updated — {self._fmt(upd)}."

    async def delete_event(self, day, ref) -> str:
        ev = await self._resolve(day, ref)
        await self._call("DELETE", f"/calendars/{self._cal()}/events/{ev['id']}")
        self._sys_cache = None
        return f"Deleted '{ev.get('summary') or '(no title)'}' from the calendar."

    # ── system-prompt injection ─────────────────────────────────────────────
    async def system_block(self, cap: int) -> str:
        """'## Today's calendar' block for the kernel context, or ''. Only when
        configured and inject_daily != on_demand; cached CACHE_SECONDS."""
        if not self.configured:
            return ""
        if str(self.cfg().get("inject_daily") or "always").lower() == "on_demand":
            return ""
        now = _time.time()
        if self._sys_cache and self._sys_cache[0] > now:
            return self._sys_cache[1][:cap]
        try:
            d, items = await self._events("today")
            lines = [self._fmt(ev, i) for i, ev in enumerate(items, 1)] or ["(no events today)"]
            block = f"## Today's calendar ({d:%a %Y-%m-%d})\n" + "\n".join(lines)
        except Exception as e:
            log.warning(f"gcal: daily agenda fetch failed: {e}")
            block = ""  # cache the failure too — no hammering a broken setup
        self._sys_cache = (now + CACHE_SECONDS, block)
        return block[:cap]
