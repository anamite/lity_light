"""The in-process replacements for pipy_catty's two HTTP paths:

  LityLLMService — the pipeline's "LLM" slot, but instead of POSTing to a
      localhost OpenAI-compatible endpoint it awaits core.kernel directly
      (via core.voice.turn, which owns the shared spoken-message cursor).

  Announcer — subscribes to core.bus and (a) speaks Home-thread assistant
      messages that arrive while the mic is idle (task results, schedules,
      heartbeat findings), (b) beeps once per new pending approval. This
      replaces the old GET /v1/voice/pending 4-second polling with push."""

import asyncio

from loguru import logger

from pipecat.frames.frames import (
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService

from .. import voice as voicemod
from ..voice import VoiceChannel


def _last_user_text(context) -> str:
    """Latest user utterance from a pipecat LLMContext (the only part Lity
    reads — the Home thread, summary and memory are the real history)."""
    for m in reversed(context.get_messages()):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):  # multimodal parts → text parts only
                c = " ".join(p.get("text", "") for p in c
                             if isinstance(p, dict) and p.get("type") == "text")
            return (c or "").strip()
    return ""


class LityLLMService(LLMService):
    """Kernel-backed 'LLM': one LLMContextFrame in, speakable sentences out.

    Mirrors OpenAILLMService's frame contract (FullResponseStart → text
    frames → FullResponseEnd) so the TTS and the gate's state machine see
    exactly what they saw with the HTTP backend."""

    def __init__(self, core, **kwargs):
        super().__init__(**kwargs)
        self._core = core

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            try:
                await self.push_frame(LLMFullResponseStartFrame())
                await self.start_processing_metrics()
                await self._respond(frame.context)
            except Exception as e:
                await self.push_error(error_msg=f"Lity turn failed: {e}", exception=e)
            finally:
                await self.stop_processing_metrics()
                await self.push_frame(LLMFullResponseEndFrame())
        else:
            await self.push_frame(frame, direction)

    async def _respond(self, context):
        text = _last_user_text(context)
        if not text:
            return
        # turn() = kernel reply + any not-yet-spoken task results, sanitized.
        reply = await self._core.voice.turn(text)
        for sentence in voicemod.sentences(reply):
            await self.push_frame(LLMTextFrame(sentence))


class Announcer:
    """EventBus subscriber: proactive speech and approval beeps, pushed."""

    def __init__(self, core, gate, tones, task):
        self._core = core
        self._gate = gate       # WakeWordGate — only speak while idle
        self._tones = tones     # TonePlayerObserver — owns the beep stream
        self._task = task       # PipelineTask — queue_frames target

    async def run(self):
        q = self._core.bus.subscribe()
        try:
            while True:
                ev = await q.get()
                try:
                    await self._handle(ev)
                except Exception as e:
                    logger.warning(f"announcer: event handling failed: {e}")
        finally:
            self._core.bus.unsubscribe(q)

    async def _handle(self, ev: dict):
        etype = ev.get("type")

        if (etype == "message.created"
                and ev.get("thread_id") == VoiceChannel.THREAD
                and ev.get("role") == "assistant"):
            # Mid-conversation the reply rides in on the current turn (the
            # shared cursor makes sure of it); only announce while idle.
            if not self._gate.is_idle:
                return
            texts = await self._core.voice.unheard()
            if texts:
                logger.info(f"📨 announcing {len(texts)} message(s) from Lity")
                # append_to_context=False: standalone announcements; Lity owns
                # conversation memory, not the pipeline's local context.
                await self._task.queue_frames(
                    [TTSSpeakFrame(t, append_to_context=False) for t in texts])

        elif etype == "approval.requested":
            if self._core.voice.should_beep(int(ev.get("id", 0))):
                logger.info("🔔 new approval pending — beeping")
                self._tones.play_approval()

        elif etype == "approval.resolved":
            self._core.voice.forget_approval(int(ev.get("id", 0)))
