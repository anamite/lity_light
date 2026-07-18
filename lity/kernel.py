"""The kernel — the main-thread agent loop. Small model, small context,
small tool set. Everything heavy leaves through `delegate`."""

import asyncio
import json
from collections import defaultdict

from . import context
from .db import dumps
from .tools import INTERNAL, ToolContext, openai_schema, run_tool
from .tools.core import KERNEL_TOOLS


class Kernel:
    def __init__(self, app):
        self.app = app
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def on_user_message(self, thread_id: int, text: str, source: str = "text"):
        """source: 'text' (web UI) or 'voice'. Replies to TYPED input are marked
        UI-delivered so the voicebot doesn't read them out (see VoiceChannel)."""
        await self.app.db.add_message(thread_id, "user", text)
        self.app.bus.emit("message.created", thread_id=thread_id, role="user", content=text)
        # armed approval options: a 1:1 option match executes the decision
        # deterministically — the LLM is bypassed entirely for this message
        confirm = await self.app.approvals.try_option_match(thread_id, text)
        if confirm is not None:
            mid = await self.app.db.add_message(thread_id, "assistant", confirm)
            if source == "text":
                self.app.voice.note_text_reply(mid)
            self.app.bus.emit("message.created", thread_id=thread_id,
                              role="assistant", content=confirm)
            return
        await self._run_turn(thread_id, latest_user_text=text, source=source)

    async def system_event(self, thread_id: int, text: str):
        """Task results, fired schedules, heartbeat findings enter here.
        source='event': these are proactive — the voicebot DOES announce them."""
        await self.app.db.add_message(thread_id, "event", text)
        self.app.bus.emit("message.created", thread_id=thread_id, role="event", content=text)
        await self._run_turn(thread_id, latest_user_text=text, source="event")

    async def _run_turn(self, thread_id: int, latest_user_text: str, source: str = "text"):
        async with self._locks[thread_id]:
            cfg = self.app.cfg
            system = await context.build_system(self.app, thread_id, latest_user_text)
            messages = [{"role": "system", "content": system}]
            history, has_images = await context.build_messages(self.app, thread_id)
            messages += history
            # images in the window → route the turn to the vision model
            model = (cfg.get_path("models.vision") if has_images else None) \
                or cfg.get_path("models.main")
            tools = openai_schema(KERNEL_TOOLS)
            ctx = ToolContext(app=self.app, thread_id=thread_id)

            final_text = ""
            direct_sent = False
            for _ in range(int(cfg.get_path("kernel.max_tool_iterations", 8))):
                try:
                    msg, usage = await self.app.llm.chat(
                        model, messages, tools=tools, max_tokens=1024)
                except Exception as e:
                    final_text = f"(I hit an error talking to the model: {e})"
                    break

                calls = msg.get("tool_calls") or []
                if not calls:
                    final_text = (msg.get("content") or "").strip()
                    break

                messages.append(msg)
                for call in calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    direct = bool(args.pop("direct_to_user", False))
                    result = await run_tool(ctx, name, args)
                    if result.startswith(INTERNAL):
                        # model-only content (manuals, capability sheets) — never
                        # deliverable raw, whatever direct_to_user said
                        result = result[len(INTERNAL):]
                        direct = False
                    if direct and not result.startswith(("Denied", "Error")):
                        # tool output goes straight to the user — no extra model step
                        mid = await self.app.db.add_message(thread_id, "assistant", result)
                        if source == "text":
                            self.app.voice.note_text_reply(mid)
                        self.app.bus.emit("message.created", thread_id=thread_id,
                                          role="assistant", content=result)
                        final_text = result
                        direct_sent = True
                        messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                         "content": "(output delivered directly to the user)"})
                        continue
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                     "content": result})
                    # Persist the resolved pair as ONE collapsed event line —
                    # this is what future turns see instead of the raw pair.
                    line = f"{name}({dumps(args)[:120]}) → {result[:200]}"
                    await self.app.db.add_message(thread_id, "event", line, tool_name=name)
                    self.app.bus.emit("message.created", thread_id=thread_id,
                                      role="event", content=line)
                if direct_sent:
                    break  # turn is over; the user already has the answer
            else:
                final_text = "(I stopped — too many tool steps for one turn.)"

            # NO_REPLY = the model judged this event needs no user-facing message
            if final_text.strip() == "NO_REPLY":
                final_text = ""
            # guard against repeating the previous reply (e.g. two near-identical
            # system events for the same task arriving back-to-back)
            if final_text and not direct_sent:
                prev = await self.app.db.fetchone(
                    "SELECT content FROM messages WHERE thread_id=? AND role='assistant' "
                    "ORDER BY id DESC LIMIT 1", (thread_id,))
                if prev and prev["content"].strip() == final_text.strip():
                    final_text = ""
            if final_text and not direct_sent:
                mid = await self.app.db.add_message(thread_id, "assistant", final_text)
                if source == "text":
                    self.app.voice.note_text_reply(mid)
                self.app.bus.emit("message.created", thread_id=thread_id,
                                  role="assistant", content=final_text)

        # Post-turn housekeeping, off the reply path:
        asyncio.create_task(self.app.memory.extract(thread_id, latest_user_text, final_text))
        asyncio.create_task(self.app.compactor.maybe_compact(thread_id))
