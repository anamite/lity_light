"""In-process pub/sub bus. Every state change emits an event; the SSE endpoint
and any future channels (Telegram, Discord) are just subscribers."""

import asyncio
import time


class EventBus:
    def __init__(self):
        self._subs: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=500)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subs:
            self._subs.remove(q)

    def emit(self, type_: str, **payload):
        event = {"type": type_, "ts": time.time(), **payload}
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer: drop rather than block the agent
