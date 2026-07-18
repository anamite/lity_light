"""Telegram module — Lity on your phone, strictly opt-in.

What it does once configured (enabled + chat_id in config.yaml, bot token in
.env as TELEGRAM_BOT_TOKEN):

  outbound  the `telegram` kernel tool sends messages/workspace files ON
            PURPOSE — only when the user asks or a turn genuinely needs it.
  approvals when telegram.forward_approvals is true, every approval request
            (Hermes-bridged or local) also appears in Telegram with inline
            buttons; a tap runs the same approvals.resolve() path as the web
            UI and voice, and the card updates with the outcome wherever the
            decision was made.
  inbound   text messages from the owner's chat run a normal kernel turn in
            the Home thread, stored as "[via Telegram] …"; the reply goes
            back to Telegram (and the web UI) but is NOT spoken (same
            channel philosophy as typed input). Messages from any other
            chat id are ignored.

A supervisor task starts at boot and checks the live config — enabling the
module needs no restart; polling begins within ~5 s of setup."""

import asyncio
import json
import logging
import mimetypes
import os

import httpx

from . import modules_cfg

log = logging.getLogger("lity.telegram")

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
HOME = 1  # the shared Home thread (same as VoiceChannel.THREAD)
MAX_CHUNK = 4000  # Telegram hard limit is 4096 chars/message


class Telegram:
    def __init__(self, app):
        self.app = app
        self._cards: dict[int, tuple[int, str]] = {}  # approval id -> (msg id, tool)

    # ── config / status ─────────────────────────────────────────────────────
    def cfg(self) -> dict:
        return modules_cfg(self.app, "telegram")

    @property
    def token(self) -> str:
        return os.environ.get(TOKEN_ENV, "").strip()

    @property
    def configured(self) -> bool:
        c = self.cfg()
        return bool(c.get("enabled")) and bool(str(c.get("chat_id") or "").strip()) \
            and bool(self.token)

    def status(self) -> str:
        c = self.cfg()
        checks = [
            ("telegram.enabled is true in config.yaml", bool(c.get("enabled"))),
            (f"bot token set in .env as {TOKEN_ENV}", bool(self.token)),
            ("telegram.chat_id set (the owner's chat)", bool(str(c.get("chat_id") or "").strip())),
        ]
        missing = [n for n, ok in checks if not ok]
        if not missing:
            fwd = c.get("forward_approvals", True)
            return (f"Telegram: READY (chat {c.get('chat_id')}, approval forwarding "
                    f"{'on' if fwd else 'off'}). Messages from the user there arrive "
                    "tagged [via Telegram]; replies go back silently.")
        return ("Telegram: NOT ready — missing: " + "; ".join(missing)
                + ". The telegram tool's setup action has the manual.")

    def setup_manual(self) -> str:
        return f"""TELEGRAM MODULE — SETUP MANUAL (internal: read this, then guide the user ONE step at a time in your own words.)

How it works: the user creates their own Telegram bot (takes a minute, free), Lity polls it. Only the paired chat id is ever answered. Config is live — no restart.

Step 1 — create the bot (user, in the Telegram app):
  Open a chat with @BotFather → send /newbot → pick any display name, then a unique username ending in 'bot'. BotFather replies with an HTTP API TOKEN (looks like 123456789:AA…).

Step 2 — give Lity the token (on the Lity machine):
  ./lityctl key {TOKEN_ENV}    (pastes hidden)

Step 3 — pair the chat. EASIEST: run  ./lityctl setup  → step 6 → enable Telegram: the wizard waits while the user sends any message to their new bot and captures the chat id automatically. Manual alternative: message @userinfobot for their id, then ./lityctl set telegram.chat_id THE_ID
  Finally: ./lityctl set telegram.enabled true

Step 4 — verify: within ~5 seconds Lity starts polling. The user sends the bot "hello" — it should get a reply. Approval requests now also appear there with decision buttons (turn off with ./lityctl set telegram.forward_approvals false).

CURRENT STATUS: {self.status()}"""

    # ── Telegram Bot API ────────────────────────────────────────────────────
    async def _api(self, method: str, payload: dict | None = None,
                   files: dict | None = None, timeout: float = 15) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        async with httpx.AsyncClient(timeout=timeout) as cli:
            if files:
                r = await cli.post(url, data=payload or {}, files=files)
            else:
                r = await cli.post(url, json=payload or {})
        data = r.json()
        if not data.get("ok"):
            raise ValueError(f"Telegram {method} failed: {data.get('description', r.text[:120])}")
        return data.get("result") or {}

    async def send_message(self, text: str, reply_markup: dict | None = None) -> int | None:
        """Plain-text message to the owner chat, chunked to the API limit.
        Returns the (last) message id."""
        chat = str(self.cfg().get("chat_id") or "").strip()
        text = (text or "").strip()
        if not chat or not text:
            return None
        mid = None
        chunks = [text[i:i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)]
        for i, chunk in enumerate(chunks):
            payload = {"chat_id": chat, "text": chunk}
            if reply_markup and i == len(chunks) - 1:  # buttons on the last chunk
                payload["reply_markup"] = reply_markup
            mid = (await self._api("sendMessage", payload)).get("message_id")
        return mid

    async def send_file(self, path, caption: str = "") -> str:
        """Send a workspace file as photo/document. Returns a speakable result."""
        ws = self.app.cfg.workspace
        p = (ws / str(path)).resolve()
        if not p.is_relative_to(ws):
            raise ValueError("Path escapes the workspace.")
        if not p.is_file():
            raise ValueError(f"No such file '{path}' in the workspace.")
        chat = str(self.cfg().get("chat_id") or "").strip()
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        field, method = (("photo", "sendPhoto") if mime.startswith("image/")
                         else ("document", "sendDocument"))
        await self._api(method, {"chat_id": chat, "caption": (caption or "")[:1000]},
                        files={field: (p.name, p.read_bytes(), mime)}, timeout=60)
        return f"Sent '{p.name}' to Telegram."

    def reply_bg(self, text: str):
        """Fire-and-forget reply back to Telegram (kernel calls this for turns
        that came in via Telegram). Failures log, never break the turn."""
        async def _go():
            try:
                await self.send_message(text)
            except Exception as e:
                log.warning(f"telegram reply failed: {e}")
        asyncio.create_task(_go())

    # ── approval cards ──────────────────────────────────────────────────────
    async def _approval_choices(self, row) -> list[str]:
        if row["run_id"]:
            try:
                return (json.loads(row["args_json"]).get("_hermes_choices")
                        or ["once", "session", "always", "deny"])
            except (json.JSONDecodeError, TypeError):
                return ["once", "session", "always", "deny"]
        return ["approve", "deny"]

    async def _send_approval(self, approval_id: int):
        row = await self.app.db.fetchone(
            "SELECT * FROM approvals WHERE id=? AND status='pending'", (approval_id,))
        if not row:
            return
        try:
            args = {k: v for k, v in json.loads(row["args_json"]).items()
                    if not str(k).startswith("_")}
        except (json.JSONDecodeError, TypeError):
            args = {}
        text = (f"🔐 Approval #{row['id']} — tool `{row['tool']}`"
                + (f" (task #{row['task_id']})" if row["task_id"] else "")
                + (f"\n{json.dumps(args)[:300]}" if args else ""))
        choices = await self._approval_choices(row)
        markup = {"inline_keyboard": [[
            {"text": c, "callback_data": f"apr:{row['id']}:{c}"} for c in choices]]}
        mid = await self.send_message(text, reply_markup=markup)
        if mid:
            self._cards[approval_id] = (mid, row["tool"])

    async def _mark_resolved(self, approval_id: int, status: str):
        card = self._cards.pop(approval_id, None)
        if not card:
            return
        mid, tool = card
        icon = "✅" if status in ("approved", "always") else "🚫"
        await self._api("editMessageText", {
            "chat_id": str(self.cfg().get("chat_id") or ""), "message_id": mid,
            "text": f"{icon} Approval #{approval_id} (`{tool}`) — {status}."})

    # ── inbound ─────────────────────────────────────────────────────────────
    async def _handle_update(self, u: dict):
        chat_ok = lambda c: str((c or {}).get("id", "")) == str(self.cfg().get("chat_id") or "")

        msg = u.get("message")
        if msg:
            if not chat_ok(msg.get("chat")):
                log.info(f"telegram: ignoring message from foreign chat "
                         f"{(msg.get('chat') or {}).get('id')}")
                return
            text = (msg.get("text") or "").strip()
            if not text:
                await self.send_message("I can only read text messages here for now.")
                return
            # normal kernel turn; reply routes back here via kernel._deliver
            asyncio.create_task(self.app.kernel.on_user_message(
                HOME, f"[via Telegram] {text}", source="telegram"))
            return

        cq = u.get("callback_query")
        if cq:
            if not chat_ok((cq.get("message") or {}).get("chat")):
                return
            parts = str(cq.get("data") or "").split(":")
            note = "Not a decision I recognize."
            if len(parts) == 3 and parts[0] == "apr":
                ok = await self.app.approvals.resolve(int(parts[1]), parts[2])
                note = f"Registered: {parts[2]}" if ok else "No longer pending."
            await self._api("answerCallbackQuery", {
                "callback_query_id": cq.get("id"), "text": note})

    # ── the long-running loops ──────────────────────────────────────────────
    async def run(self):
        """Started at boot regardless of config; idles until configured."""
        poller = asyncio.create_task(self._poll_loop())
        events = asyncio.create_task(self._event_loop())
        try:
            await asyncio.gather(poller, events)
        finally:
            poller.cancel()
            events.cancel()

    async def _poll_loop(self):
        offset = None
        while True:
            if not self.configured:
                await asyncio.sleep(5)
                continue
            try:
                payload = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    payload["offset"] = offset
                result = await self._api("getUpdates", payload, timeout=35)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                wait = 60 if "Unauthorized" in str(e) else 10
                log.warning(f"telegram poll failed ({e}) — retrying in {wait}s")
                await asyncio.sleep(wait)
                continue
            for u in result if isinstance(result, list) else []:
                offset = u.get("update_id", 0) + 1
                try:
                    await self._handle_update(u)
                except Exception as e:
                    log.warning(f"telegram update handling failed: {e}")

    async def _event_loop(self):
        q = self.app.bus.subscribe()
        try:
            while True:
                ev = await q.get()
                try:
                    if not self.configured:
                        continue
                    if (ev.get("type") == "approval.requested"
                            and self.cfg().get("forward_approvals", True)):
                        await self._send_approval(int(ev["id"]))
                    elif ev.get("type") == "approval.resolved":
                        await self._mark_resolved(int(ev.get("id", 0)),
                                                  str(ev.get("status", "")))
                except Exception as e:
                    log.warning(f"telegram event handling failed: {e}")
        finally:
            self.app.bus.unsubscribe(q)
