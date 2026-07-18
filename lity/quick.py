"""Quick local tool system — the small stuff that must never need Hermes:
timers & alarms (with a real ringing beep on the server's speaker), notes,
shopping lists, weather and speaker volume. One service (`app.quick`) validates every request,
owns the async timer engine, and answers in plain speakable strings.

Timer lifecycle: pending → (fires) ringing → done | missed. Ringing beeps in
a loop until stop_ringing/cancel or quick.ring_seconds auto-silence. Pending
timers survive restarts (reloaded from qtimers on boot); ones that came due
while the process was down are announced as missed, never silently dropped."""

import asyncio
import contextlib
import math
import re
import struct
import subprocess
import sys
import time
import wave
from datetime import datetime, timedelta, timezone

import httpx

FMT = "%Y-%m-%d %H:%M:%S"
MAX_TIMER_SECONDS = 7 * 86400
MAX_ALARM_DAYS = 30

_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
          "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "d": 86400, "day": 86400, "days": 86400}

_DUR_TOKEN = r"(\d+(?:\.\d+)?)\s*([a-z]+)"

WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
       45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
       55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
       67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
       77: "snow grains", 80: "light rain showers", 81: "rain showers",
       82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail"}


def human_secs(secs: float) -> str:
    secs = max(0, int(secs))
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = [f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (m, "m"), (s, "s")) if v]
    return " ".join(parts) or "0s"


def parse_duration(text: str) -> int:
    s = str(text or "").strip().lower()
    if not s:
        raise ValueError("Give a duration like '30s', '5m' or '1h30m'.")
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return _check_secs(float(s))  # bare number = seconds
    total, matched = 0.0, 0
    for m in re.finditer(_DUR_TOKEN, s):
        unit = _UNITS.get(m.group(2))
        if unit is None:
            raise ValueError(f"I don't understand the unit '{m.group(2)}' — use s, m, h or d.")
        total += float(m.group(1)) * unit
        matched += 1
    leftover = re.sub(_DUR_TOKEN, "", s)
    leftover = re.sub(r"[\s,]|and", "", leftover)
    if not matched or leftover:
        raise ValueError("Give a duration like '30s', '5m' or '1h30m'.")
    return _check_secs(total)


def _check_secs(total: float) -> int:
    secs = int(total)
    if secs < 1:
        raise ValueError("Minimum timer is 1 second.")
    if secs > MAX_TIMER_SECONDS:
        raise ValueError("Maximum timer is 7 days — use an alarm (or a schedule) instead.")
    return secs


def parse_alarm(text: str) -> datetime:
    """'HH:MM' (local; past → tomorrow) or 'YYYY-MM-DD HH:MM'. Returns aware local dt."""
    t = str(text or "").strip()
    now = datetime.now().astimezone()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(t, fmt).replace(tzinfo=now.tzinfo)
        except ValueError:
            continue
        if dt <= now:
            raise ValueError("That date and time is already in the past.")
        if dt - now > timedelta(days=MAX_ALARM_DAYS):
            raise ValueError(f"Alarms only reach {MAX_ALARM_DAYS} days ahead — use schedule for further out.")
        return dt
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            tm = datetime.strptime(t, fmt)
        except ValueError:
            continue
        dt = now.replace(hour=tm.hour, minute=tm.minute, second=tm.second, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)  # "07:00" after 07:00 means tomorrow
        return dt
    raise ValueError("Give the alarm time as 'HH:MM' (24h, local time) or 'YYYY-MM-DD HH:MM'.")


def _write_beep_wav(path):
    """Three short 1568 Hz beeps ('peep peep peep') — stdlib only."""
    rate = 22050
    frames = bytearray()
    for _ in range(3):
        n = int(rate * 0.16)
        for i in range(n):
            env = min(1.0, i / 200, (n - i) / 400)  # click-free attack/release
            frames += struct.pack("<h", int(14000 * env * math.sin(2 * math.pi * 1568 * i / rate)))
        frames += b"\x00\x00" * int(rate * 0.08)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


class Quick:
    def __init__(self, app):
        self.app = app
        self._waiters: dict[int, asyncio.Task] = {}
        self._ringers: dict[int, asyncio.Task] = {}
        self._wcache: dict[str, tuple[float, str]] = {}  # city -> (expiry, report)
        self._wav = None
        self._mixer: tuple[str, str] | None = None  # detected (alsa card, control)

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self):
        self._wav = self.app.cfg.resolve("database", "./data/lity.db").parent / "beep.wav"
        try:
            if not self._wav.is_file():
                _write_beep_wav(self._wav)
        except OSError:
            self._wav = None
        rows = await self.app.db.fetchall(
            "SELECT * FROM qtimers WHERE status IN ('pending','ringing')")
        now = datetime.now(timezone.utc)
        for r in rows:
            fires = datetime.strptime(r["fires_at"], FMT).replace(tzinfo=timezone.utc)
            if r["status"] == "ringing" or fires <= now:
                await self.app.db.execute(
                    "UPDATE qtimers SET status='missed' WHERE id=?", (r["id"],))
                asyncio.create_task(self.app.kernel.system_event(
                    r["thread_id"],
                    f"[timer] {r['kind']} #{r['id']} '{r['label']}' came due while Lity was "
                    f"offline and was MISSED. Tell the user briefly."))
            else:
                self._spawn_waiter(r["id"], fires, r["thread_id"], r["kind"], r["label"])

    async def shutdown(self):
        for t in [*self._waiters.values(), *self._ringers.values()]:
            t.cancel()

    # ── timers & alarms ───────────────────────────────────────────────────
    async def set_timer(self, duration_text: str, label: str | None, thread_id: int) -> str:
        secs = parse_duration(duration_text)
        fires = datetime.now(timezone.utc) + timedelta(seconds=secs)
        label = (label or "").strip()[:60] or f"{human_secs(secs)} timer"
        tid = await self.app.db.execute(
            "INSERT INTO qtimers(kind, label, fires_at, thread_id) VALUES ('timer',?,?,?)",
            (label, fires.strftime(FMT), thread_id))
        self._spawn_waiter(tid, fires, thread_id, "timer", label)
        self.app.bus.emit("timer.updated", id=tid, status="pending")
        return f"Timer #{tid} '{label}' set — goes off in {human_secs(secs)}."

    async def set_alarm(self, time_text: str, label: str | None, thread_id: int) -> str:
        local = parse_alarm(time_text)
        fires = local.astimezone(timezone.utc)
        label = (label or "").strip()[:60] or "alarm"
        tid = await self.app.db.execute(
            "INSERT INTO qtimers(kind, label, fires_at, thread_id) VALUES ('alarm',?,?,?)",
            (label, fires.strftime(FMT), thread_id))
        self._spawn_waiter(tid, fires, thread_id, "alarm", label)
        self.app.bus.emit("timer.updated", id=tid, status="pending")
        day = ("today" if local.date() == datetime.now().astimezone().date()
               else "tomorrow" if local.date() == (datetime.now().astimezone() + timedelta(days=1)).date()
               else local.strftime("%A %Y-%m-%d"))
        return f"Alarm #{tid} '{label}' set for {local.strftime('%H:%M')} {day}."

    async def open_timers(self):
        return await self.app.db.fetchall(
            "SELECT * FROM qtimers WHERE status IN ('pending','ringing') ORDER BY fires_at")

    async def timers_text(self) -> str:
        rows = await self.open_timers()
        if not rows:
            return "No timers or alarms are set."
        now = datetime.now(timezone.utc)
        lines = []
        for r in rows:
            fires = datetime.strptime(r["fires_at"], FMT).replace(tzinfo=timezone.utc)
            if r["status"] == "ringing":
                lines.append(f"{r['kind']} #{r['id']} '{r['label']}' is RINGING right now")
            else:
                lines.append(f"{r['kind']} #{r['id']} '{r['label']}' goes off in "
                             f"{human_secs((fires - now).total_seconds())}")
        return ". ".join(lines) + "."

    async def cancel_timer(self, tid: int) -> str:
        row = await self.app.db.fetchone("SELECT * FROM qtimers WHERE id=?", (tid,))
        if not row:
            return f"No timer or alarm #{tid}."
        if row["status"] == "ringing":
            return await self.stop_ringing(only_id=tid)
        if row["status"] != "pending":
            return f"{row['kind'].capitalize()} #{tid} '{row['label']}' is already {row['status']}."
        w = self._waiters.pop(tid, None)
        if w:
            w.cancel()
        await self.app.db.execute("UPDATE qtimers SET status='cancelled' WHERE id=?", (tid,))
        self.app.bus.emit("timer.updated", id=tid, status="cancelled")
        return f"Cancelled {row['kind']} #{tid} '{row['label']}'."

    async def stop_ringing(self, only_id: int | None = None) -> str:
        """The 'stop it' path: silence what is ringing (all of it, unless only_id)."""
        stopped = []
        for tid, task in list(self._ringers.items()):
            if only_id is not None and tid != only_id:
                continue
            task.cancel()
            self._ringers.pop(tid, None)
            row = await self.app.db.fetchone("SELECT * FROM qtimers WHERE id=?", (tid,))
            await self.app.db.execute("UPDATE qtimers SET status='done' WHERE id=?", (tid,))
            self.app.bus.emit("timer.updated", id=tid, status="done")
            stopped.append(f"'{row['label']}'" if row else f"#{tid}")
        if only_id is None:
            # crash-orphaned 'ringing' rows with no live ringer task
            await self.app.db.execute(
                "UPDATE qtimers SET status='done' WHERE status='ringing'")
        if stopped:
            return "Stopped " + " and ".join(stopped) + "."
        rows = await self.open_timers()
        if rows:
            return "Nothing is ringing right now. Still set: " + await self.timers_text()
        return "Nothing is ringing and no timers or alarms are set."

    def _spawn_waiter(self, tid, fires_utc, thread_id, kind, label):
        t = asyncio.create_task(self._wait(tid, fires_utc, thread_id, kind, label))
        self._waiters[tid] = t
        t.add_done_callback(lambda _: self._waiters.pop(tid, None))

    async def _wait(self, tid, fires_utc, thread_id, kind, label):
        delay = (fires_utc - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        row = await self.app.db.fetchone("SELECT status FROM qtimers WHERE id=?", (tid,))
        if not row or row["status"] != "pending":
            return  # cancelled while we slept
        ring = int(self.app.cfg.get_path("quick.ring_seconds", 60))
        await self.app.db.execute("UPDATE qtimers SET status='ringing' WHERE id=?", (tid,))
        self.app.bus.emit("timer.updated", id=tid, status="ringing", kind=kind, label=label)
        r = asyncio.create_task(self._ring(tid, thread_id, kind, label, ring))
        self._ringers[tid] = r
        asyncio.create_task(self.app.kernel.system_event(
            thread_id,
            f"[timer] {kind} #{tid} '{label}' just went off and is RINGING "
            f"(auto-silences in {ring}s). Tell the user NOW, in one short sentence. "
            f"If they say stop, call timer with action stop_ringing."))

    async def _ring(self, tid, thread_id, kind, label, ring_seconds):
        end = time.monotonic() + ring_seconds
        try:
            while time.monotonic() < end:
                await asyncio.to_thread(self._beep_once)
                await asyncio.sleep(1.4)
        except asyncio.CancelledError:
            raise  # stopped by the user — stop_ringing already updates the row
        finally:
            self._ringers.pop(tid, None)
        # nobody stopped it → auto-silence, but leave a trace
        await self.app.db.execute(
            "UPDATE qtimers SET status='missed' WHERE id=? AND status='ringing'", (tid,))
        self.app.bus.emit("timer.updated", id=tid, status="missed")
        await self.app.kernel.system_event(
            thread_id,
            f"[timer] {kind} #{tid} '{label}' rang for {ring_seconds}s with no response and "
            f"auto-silenced. Mention it once (or NO_REPLY if already acknowledged).")

    def _beep_once(self):
        """peep peep peep — winsound on Windows, generated WAV via aplay on the
        Pi, terminal bell as last resort. Must never raise."""
        if not self.app.cfg.get_path("quick.beep", True):
            return
        try:
            if sys.platform == "win32":
                import winsound
                for _ in range(3):
                    winsound.Beep(1568, 160)
                    time.sleep(0.07)
                return
            if self._wav and self._wav.is_file():
                r = subprocess.run(["aplay", "-q", str(self._wav)],
                                   timeout=5, capture_output=True)
                if r.returncode == 0:
                    return
        except Exception:
            pass
        with contextlib.suppress(Exception):
            print("\a", end="", flush=True)

    # ── notes ─────────────────────────────────────────────────────────────
    async def note_add(self, title: str | None, content: str | None) -> str:
        content = (content or "").strip()[:4000]
        title = (title or "").strip()[:80]
        if not content and not title:
            return "A note needs a title or some content."
        if not title:
            title = content.splitlines()[0][:40]
        nid = await self.app.db.execute(
            "INSERT INTO notes(title, content) VALUES (?,?)", (title, content))
        return f"Note #{nid} '{title}' saved."

    async def note_list(self, query: str | None) -> str:
        q = (query or "").strip()
        if q:
            like = f"%{q}%"
            rows = await self.app.db.fetchall(
                "SELECT * FROM notes WHERE title LIKE ? OR content LIKE ? ORDER BY id DESC LIMIT 15",
                (like, like))
            if not rows:
                return f"No notes matching '{q}'."
        else:
            rows = await self.app.db.fetchall("SELECT * FROM notes ORDER BY id DESC LIMIT 15")
            if not rows:
                return "No notes saved yet."
        return "\n".join(f"#{r['id']} '{r['title']}': {r['content'][:60]}" for r in rows)

    async def note_get(self, nid: int | None, title: str | None) -> str:
        row = None
        if nid:
            row = await self.app.db.fetchone("SELECT * FROM notes WHERE id=?", (nid,))
        elif title:
            rows = await self.app.db.fetchall(
                "SELECT * FROM notes WHERE title LIKE ? ORDER BY id DESC LIMIT 5",
                (f"%{title.strip()}%",))
            if len(rows) > 1:
                return ("Several notes match: " +
                        ", ".join(f"#{r['id']} '{r['title']}'" for r in rows) +
                        ". Which id?")
            row = rows[0] if rows else None
        if not row:
            return "No such note."
        return f"Note #{row['id']} '{row['title']}' ({row['created_at']} UTC):\n{row['content']}"

    async def note_delete(self, nid: int | None) -> str:
        if not nid:
            return "Deleting needs the note id (use list first)."
        row = await self.app.db.fetchone("SELECT * FROM notes WHERE id=?", (nid,))
        if not row:
            return f"No note #{nid}."
        await self.app.db.execute("DELETE FROM notes WHERE id=?", (nid,))
        return f"Deleted note #{nid} '{row['title']}'."

    # ── speaker volume (ALSA amixer — e.g. the Pi's USB speaker) ──────────
    # Card/control auto-detect: USB audio devices expose one playback control,
    # usually 'Speaker' or 'PCM', on their own card. quick.alsa_card /
    # quick.alsa_control in config.yaml pin it when detection guesses wrong.
    _MIXER_PREFERRED = ("Speaker", "PCM", "Master", "Headphone", "Digital")

    async def _amixer(self, *args) -> tuple[int, str]:
        """Run amixer without blocking the event loop. Returns (rc, output)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "amixer", *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return proc.returncode or 0, out.decode(errors="replace")
        except FileNotFoundError:
            return 127, "amixer not found (ALSA utils not installed?)"
        except (OSError, asyncio.TimeoutError) as e:
            return 1, f"amixer failed: {e}"

    async def _find_mixer(self) -> tuple[str, str]:
        """Return (card, control) with a playback volume; cached until an
        amixer call fails (USB replug can renumber cards)."""
        if self._mixer:
            return self._mixer
        card_cfg = self.app.cfg.get_path("quick.alsa_card")
        ctrl_cfg = str(self.app.cfg.get_path("quick.alsa_control") or "").strip()
        cards = ([str(card_cfg)] if card_cfg not in (None, "")
                 else [str(i) for i in range(8)])
        for card in cards:
            rc, out = await self._amixer("-c", card, "scontrols")
            if rc != 0:
                continue
            names = re.findall(r"Simple mixer control '([^']+)'", out)
            ordered = ([ctrl_cfg] if ctrl_cfg in names else
                       [n for n in self._MIXER_PREFERRED if n in names] +
                       [n for n in names if n not in self._MIXER_PREFERRED])
            for name in ordered:
                rc, out = await self._amixer("-c", card, "sget", name)
                if rc == 0 and re.search(r"\[\d{1,3}%\]", out):
                    self._mixer = (card, name)
                    return self._mixer
        raise ValueError(
            "No ALSA playback volume control found. Is the USB speaker plugged in? "
            "You can pin it via quick.alsa_card / quick.alsa_control in config.yaml "
            "(inspect with `amixer -c <n> scontrols`).")

    @staticmethod
    def _mixer_state(out: str) -> tuple[int | None, bool]:
        """Parse amixer output → (percent, muted)."""
        pct = re.search(r"\[(\d{1,3})%\]", out)
        return (int(pct.group(1)) if pct else None), "[off]" in out

    async def volume(self, action: str, level=None, step=None) -> str:
        if sys.platform in ("win32", "darwin"):
            return ("Volume control is implemented for Linux/ALSA (the Pi's speaker) "
                    "only — use the system volume controls on this machine.")
        action = str(action or "get").lower()
        card, ctrl = await self._find_mixer()

        if action in ("set",):
            if level is None:
                return "Set needs a level between 0 and 100."
            target = f"{max(0, min(100, int(level)))}%"
        elif action in ("up", "down"):
            pts = max(1, min(100, int(step or 10)))
            target = f"{pts}%{'+' if action == 'up' else '-'}"
        elif action in ("mute", "unmute"):
            target = action
        elif action in ("get", "status"):
            target = None
        else:
            return "Unknown action — use get, set, up, down, mute or unmute."

        if target is None:
            rc, out = await self._amixer("-c", card, "sget", ctrl)
        else:
            rc, out = await self._amixer("-c", card, "sset", ctrl, target)
        if rc != 0:
            self._mixer = None  # stale card number? re-detect next time
            if action in ("mute", "unmute") and "Invalid command" in out:
                # control has no mute switch — emulate with 0% / a sane level
                rc, out = await self._amixer(
                    "-c", card, "sset", ctrl, "0%" if action == "mute" else "40%")
            if rc != 0:
                return f"amixer error on card {card} '{ctrl}': {out.strip()[:200]}"

        pct, muted = self._mixer_state(out)
        state = f"{pct}%" if pct is not None else "unknown"
        if muted:
            state += " (muted)"
        return f"Speaker volume is {state}."

    # ── shopping lists ────────────────────────────────────────────────────
    async def _resolve_list(self, ref, create: bool = False):
        """ref = id or title (case-insensitive). Returns a row; may create."""
        ref = str(ref or "").strip() or "Shopping"
        if ref.isdigit():
            row = await self.app.db.fetchone("SELECT * FROM shopping_lists WHERE id=?", (int(ref),))
            if row:
                return row
            raise ValueError(f"No shopping list #{ref}.")
        rows = await self.app.db.fetchall(
            "SELECT * FROM shopping_lists WHERE lower(title)=lower(?)", (ref,))
        if not rows:
            rows = await self.app.db.fetchall(
                "SELECT * FROM shopping_lists WHERE title LIKE ?", (f"%{ref}%",))
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise ValueError("Several lists match: " +
                             ", ".join(f"#{r['id']} '{r['title']}'" for r in rows) + ". Which one?")
        if create:
            lid = await self.app.db.execute(
                "INSERT INTO shopping_lists(title) VALUES (?)", (ref[:60],))
            return await self.app.db.fetchone("SELECT * FROM shopping_lists WHERE id=?", (lid,))
        existing = await self.app.db.fetchall("SELECT id, title FROM shopping_lists")
        raise ValueError(f"No list called '{ref}'." +
                         (" Existing: " + ", ".join(f"#{r['id']} '{r['title']}'" for r in existing)
                          if existing else " No lists exist yet."))

    @staticmethod
    def _clean_items(items) -> list[str]:
        if isinstance(items, str):
            items = [p for chunk in items.split(",") for p in [chunk.strip()] if p]
        out = []
        for it in (items or [])[:50]:
            s = str(it).strip()[:80]
            if s:
                out.append(s)
        if not out:
            raise ValueError("No items given.")
        return out

    async def shop_create(self, title) -> str:
        title = str(title or "").strip()[:60] or "Shopping"
        rows = await self.app.db.fetchall(
            "SELECT id FROM shopping_lists WHERE lower(title)=lower(?)", (title,))
        if rows:
            return f"A list called '{title}' already exists (#{rows[0]['id']})."
        lid = await self.app.db.execute("INSERT INTO shopping_lists(title) VALUES (?)", (title,))
        return f"Shopping list #{lid} '{title}' created."

    async def shop_add(self, ref, items) -> str:
        items = self._clean_items(items)
        row = await self._resolve_list(ref, create=True)
        have = {r["item"].lower() for r in await self.app.db.fetchall(
            "SELECT item FROM shopping_items WHERE list_id=?", (row["id"],))}
        added, skipped = [], []
        for it in items:
            (skipped if it.lower() in have else added).append(it)
            have.add(it.lower())
        for it in added:
            await self.app.db.execute(
                "INSERT INTO shopping_items(list_id, item) VALUES (?,?)", (row["id"], it))
        out = f"Added to '{row['title']}' (#{row['id']}): " + ", ".join(added) if added \
            else f"Nothing new for '{row['title']}'"
        if skipped:
            out += f". Already on it: {', '.join(skipped)}"
        return out + "."

    async def shop_remove(self, ref, items) -> str:
        row = await self._resolve_list(ref)
        items = self._clean_items(items)
        removed, missing = [], []
        for it in items:
            n = await self.app.db.fetchone(
                "SELECT id FROM shopping_items WHERE list_id=? AND lower(item)=lower(?)",
                (row["id"], it))
            if n:
                await self.app.db.execute("DELETE FROM shopping_items WHERE id=?", (n["id"],))
                removed.append(it)
            else:
                missing.append(it)
        out = f"Removed from '{row['title']}': {', '.join(removed)}" if removed else "Nothing removed"
        if missing:
            out += f". Not on the list: {', '.join(missing)}"
        return out + "."

    async def shop_check(self, ref, items) -> str:
        row = await self._resolve_list(ref)
        items = self._clean_items(items)
        done, missing = [], []
        for it in items:
            n = await self.app.db.fetchone(
                "SELECT id FROM shopping_items WHERE list_id=? AND lower(item)=lower(?) AND done=0",
                (row["id"], it))
            if n:
                await self.app.db.execute("UPDATE shopping_items SET done=1 WHERE id=?", (n["id"],))
                done.append(it)
            else:
                missing.append(it)
        out = f"Checked off on '{row['title']}': {', '.join(done)}" if done else "Nothing checked off"
        if missing:
            out += f". Not open on the list: {', '.join(missing)}"
        return out + "."

    async def shop_view(self, ref) -> str:
        row = await self._resolve_list(ref)
        items = await self.app.db.fetchall(
            "SELECT * FROM shopping_items WHERE list_id=? ORDER BY done, id", (row["id"],))
        if not items:
            return f"'{row['title']}' (#{row['id']}) is empty."
        open_ = [i["item"] for i in items if not i["done"]]
        done = [i["item"] for i in items if i["done"]]
        out = f"'{row['title']}' (#{row['id']}): " + (", ".join(open_) if open_ else "all done")
        if done:
            out += f". Already got: {', '.join(done)}"
        return out + "."

    async def shop_lists(self) -> str:
        rows = await self.app.db.fetchall(
            "SELECT l.id, l.title, "
            "SUM(CASE WHEN i.done=0 THEN 1 ELSE 0 END) AS open "
            "FROM shopping_lists l LEFT JOIN shopping_items i ON i.list_id=l.id "
            "GROUP BY l.id ORDER BY l.id")
        if not rows:
            return "No shopping lists yet."
        return ". ".join(f"#{r['id']} '{r['title']}' ({int(r['open'] or 0)} open items)"
                         for r in rows) + "."

    async def shop_delete(self, ref) -> str:
        row = await self._resolve_list(ref)
        await self.app.db.execute("DELETE FROM shopping_items WHERE list_id=?", (row["id"],))
        await self.app.db.execute("DELETE FROM shopping_lists WHERE id=?", (row["id"],))
        return f"Deleted shopping list '{row['title']}' (#{row['id']}) and its items."

    # ── weather ───────────────────────────────────────────────────────────
    async def weather(self, city: str | None) -> str:
        city = (city or "").strip() or str(
            self.app.cfg.get_path("quick.default_city", "") or "").strip()
        if not city:
            return ("Which city? No default is set — the user can add one in config.yaml "
                    "under quick.default_city.")
        key = city.lower()
        hit = self._wcache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                g = (await c.get("https://geocoding-api.open-meteo.com/v1/search",
                                 params={"name": city, "count": 1})).json()
                places = g.get("results") or []
                if not places:
                    return f"I couldn't find a place called '{city}'."
                p = places[0]
                f = (await c.get("https://api.open-meteo.com/v1/forecast", params={
                    "latitude": p["latitude"], "longitude": p["longitude"],
                    "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min,"
                             "precipitation_probability_max,weather_code",
                    "timezone": "auto", "forecast_days": 2})).json()
        except Exception as e:
            return f"The weather service is unreachable right now ({type(e).__name__})."

        def n(x):
            try:
                return str(round(float(x)))
            except (TypeError, ValueError):
                return "?"

        def day(d, i):
            try:
                return (n(d["temperature_2m_min"][i]), n(d["temperature_2m_max"][i]),
                        n(d["precipitation_probability_max"][i]),
                        WMO.get(int(d["weather_code"][i]), ""))
            except (KeyError, IndexError, TypeError, ValueError):
                return None

        cur = f.get("current") or {}
        d = f.get("daily") or {}
        place = p["name"] + (f", {p['country']}" if p.get("country") else "")
        desc = WMO.get(int(cur.get("weather_code", -1) or -1), "unknown conditions")
        out = (f"Weather in {place}: {desc}, {n(cur.get('temperature_2m'))} degrees, feels like "
               f"{n(cur.get('apparent_temperature'))}, wind {n(cur.get('wind_speed_10m'))} km/h.")
        today = day(d, 0)
        if today:
            out += f" Today {today[0]} to {today[1]} degrees, rain chance {today[2]} percent."
        tom = day(d, 1)
        if tom:
            out += f" Tomorrow {tom[3] or 'similar'}, {tom[0]} to {tom[1]} degrees."
        self._wcache[key] = (time.monotonic() + 600, out)
        return out
