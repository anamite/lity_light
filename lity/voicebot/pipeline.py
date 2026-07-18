"""Audio-side processors, ported verbatim-in-logic from pipy_catty
bot_simple.py: the wake-word gate, the STT frame-hygiene bridge, and the
tone player. Configuration comes from config.yaml (voice:) instead of .env.

Gate states (see the original state diagram):

    LOCKED   openWakeWord watches the mic locally. No audio reaches
             Speechmatics (zero STT cost while idle).
       │ wake word detected
       ▼
    OPEN     All mic audio streams to Speechmatics. ADAPTIVE turn detection
             (with a generous silence trigger) decides when you finished a
             sentence. If you never start speaking, a relock timer fires.
       │ sentence completion detected (UserStoppedSpeaking)
       ▼
    MUTED    The kernel thinks and the bot speaks. Mic audio is replaced with
             SILENCE toward Speechmatics (keeps its audio timeline continuous
             so half-heard words flush out now and get dropped). openWakeWord
             still watches the real mic: the wake word interrupts (barge-in).
       │ TTS finished (BotStoppedSpeaking)
       ▼
    OPEN     Mic reopens for followup_seconds, then LOCKED.

Frame-hygiene rules (SttGateBridge) learned the hard way:
1. Interim transcriptions are NEVER forwarded (a dropped final would
   deadlock the aggregator's turn-stop strategy).
2. A turn's final transcript can arrive AFTER its UserStoppedSpeaking; a
   0.5s grace window admits exactly ONE late final — only if the turn has
   no text yet.
3. Any other transcript arriving while the gate isn't OPEN is stale
   buffered audio; forwarding it would become a phantom LLM turn.
4. Turn signals caused by that stale audio are dropped too.
5. If the kernel/TTS never responds, a watchdog relocks the gate."""

import asyncio
import io
import logging
import math
import os
import queue
import struct
import threading
import time
import wave
from dataclasses import dataclass

logging.getLogger("openwakeword").setLevel(logging.ERROR)

import numpy as np
import soxr
from loguru import logger
from openwakeword.model import Model as WakeWordModel

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    SystemFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService

# openWakeWord expects 16 kHz mono int16 audio, processed in 80 ms chunks.
OWW_SAMPLE_RATE = 16000
OWW_CHUNK = 1280  # samples (80 ms @ 16 kHz)


@dataclass
class VoiceSettings:
    """config.yaml `voice:` section, with pipy_catty's proven defaults."""

    wake_word: str = "hey_jarvis"
    wake_threshold: float = 0.5
    wake_listen_seconds: float = 8.0
    followup_seconds: float = 3.0
    processing_timeout_seconds: float = 30.0
    eou_silence_trigger: float = 1.5
    language: str = "en"
    tts_engine: str = "kokoro"  # kokoro (local) | resemble (cloud) | openai (cloud)
    tts_voice: str = "af_heart"
    resemble_voice: str = ""            # Resemble voice UUID
    openai_tts_voice: str = "nova"
    openai_tts_model: str = "gpt-4o-mini-tts"
    input_device_index: int | None = None
    output_device_index: int | None = None
    kokoro_model_path: str | None = None
    kokoro_voices_path: str | None = None
    speechmatics_api_key_env: str = "SPEECHMATICS_API_KEY"

    @classmethod
    def from_cfg(cls, cfg) -> "VoiceSettings":
        s = cls()
        for f in ("wake_word", "language", "tts_engine", "tts_voice", "resemble_voice",
                  "openai_tts_voice", "openai_tts_model", "speechmatics_api_key_env"):
            v = cfg.get_path(f"voice.{f}")
            if v:
                setattr(s, f, str(v))
        for f in ("wake_threshold", "wake_listen_seconds", "followup_seconds",
                  "processing_timeout_seconds", "eou_silence_trigger"):
            v = cfg.get_path(f"voice.{f}")
            if v is not None:
                setattr(s, f, float(v))
        for f in ("input_device_index", "output_device_index"):
            v = cfg.get_path(f"voice.{f}")
            if v is not None and str(v).strip() != "":
                setattr(s, f, int(v))
        for f in ("kokoro_model_path", "kokoro_voices_path"):
            v = cfg.get_path(f"voice.{f}") or os.getenv(f.upper())
            if v:
                setattr(s, f, str(v))
        return s


@dataclass
class MicStateFrame(SystemFrame):
    """Marker frame: the gate's mic just opened or closed. Exists purely so
    TonePlayerObserver can react; SystemFrame means it's processed ahead of
    queued data frames, so beep timing matches the actual state change."""

    listening: bool


def _ensure_wakeword_models(wakeword: str):
    """Download openWakeWord's base + wakeword models once, if not present."""
    import openwakeword
    from openwakeword.utils import download_models

    models_dir = os.path.join(
        os.path.dirname(openwakeword.__file__), "resources", "models"
    )
    needed = ["melspectrogram.onnx", "embedding_model.onnx", f"{wakeword}_v0.1.onnx"]
    if not all(os.path.exists(os.path.join(models_dir, n)) for n in needed):
        logger.info("Downloading openWakeWord models (one-time)…")
        download_models()


class WakeWordGate(FrameProcessor):
    """LOCKED / OPEN / MUTED state machine sitting between mic and STT.

    Owns all audio-level decisions: wake word detection, forwarding audio,
    silence injection, barge-in, relock and watchdog timers. Turn signals
    arrive via SttGateBridge (which sits downstream of the STT)."""

    def __init__(self, settings: VoiceSettings):
        super().__init__()
        self._wakeword = settings.wake_word
        self._threshold = settings.wake_threshold
        self._wake_listen_seconds = settings.wake_listen_seconds
        self._followup_seconds = settings.followup_seconds
        self._mute_timeout = settings.processing_timeout_seconds

        _ensure_wakeword_models(self._wakeword)
        self._oww = WakeWordModel(
            wakeword_models=[self._wakeword], inference_framework="onnx"
        )
        self._pending = np.zeros(0, dtype=np.int16)  # buffer for 80 ms chunking

        self._state = "LOCKED"
        self._user_speaking = False
        self._relock_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None

    @property
    def is_open(self) -> bool:
        """True when we're accepting user speech (mic → Speechmatics)."""
        return self._state == "OPEN"

    @property
    def is_idle(self) -> bool:
        """True only when LOCKED — safe for the announcer to speak pushes."""
        return self._state == "LOCKED"

    # ---- pipeline entry point ------------------------------------------------
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            await self._handle_audio(frame)
            return

        # Bot speaking frames also travel upstream, so we see them here.
        if isinstance(frame, BotStartedSpeakingFrame):
            self._cancel_watchdog()  # the bot responded; the turn wasn't lost
            self._mute("bot speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # Only on a natural end of bot speech. After a barge-in we're
            # already OPEN with the full wake window; the interrupted bot's
            # trailing BotStopped must not shrink it.
            if self._state == "MUTED":
                self._open(self._followup_seconds, "bot finished")

        await self.push_frame(frame, direction)

    # ---- audio routing (the heart of the gate) -------------------------------
    async def _handle_audio(self, frame: InputAudioRawFrame):
        if self._state == "OPEN":
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)  # -> STT

        elif self._state == "LOCKED":
            if self._detect_wakeword(frame):
                self._open(self._wake_listen_seconds, "wake word")

        elif self._state == "MUTED":
            # Barge-in: the wake word cuts the bot off and reopens the mic.
            if self._detect_wakeword(frame):
                logger.info("🙋 Wake word during bot response — interrupting")
                await self.broadcast_interruption()
                self._open(self._wake_listen_seconds, "barge-in")
                return
            # Otherwise feed SILENCE to Speechmatics: the mic stays deaf, but
            # the audio timeline stays continuous, so words that were in
            # flight when we muted finalize now (and the bridge drops them)
            # instead of leaking out as a phantom turn when the mic reopens.
            silence = InputAudioRawFrame(
                audio=b"\x00" * len(frame.audio),
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
            )
            await self.push_frame(silence, FrameDirection.DOWNSTREAM)

    def _detect_wakeword(self, frame: InputAudioRawFrame) -> bool:
        samples = self._to_16k_mono(frame)
        self._pending = np.concatenate([self._pending, samples])
        while len(self._pending) >= OWW_CHUNK:
            chunk = self._pending[:OWW_CHUNK]
            self._pending = self._pending[OWW_CHUNK:]
            scores = self._oww.predict(chunk)
            if scores.get(self._wakeword, 0.0) >= self._threshold:
                self._pending = np.zeros(0, dtype=np.int16)
                self._oww.reset()
                return True
        return False

    @staticmethod
    def _to_16k_mono(frame: InputAudioRawFrame) -> np.ndarray:
        samples = np.frombuffer(frame.audio, dtype=np.int16)
        if frame.num_channels > 1:  # downmix to mono
            samples = samples.reshape(-1, frame.num_channels).mean(axis=1).astype(np.int16)
        if frame.sample_rate != OWW_SAMPLE_RATE:  # resample to 16 kHz
            resampled = soxr.resample(
                samples.astype(np.float32), frame.sample_rate, OWW_SAMPLE_RATE
            )
            samples = np.clip(resampled, -32768, 32767).astype(np.int16)
        return samples

    # ---- state transitions ---------------------------------------------------
    def _open(self, relock_after: float, reason: str):
        self._state = "OPEN"
        self._user_speaking = False
        self._cancel_relock()
        self._cancel_watchdog()
        self._relock_task = asyncio.create_task(self._relock_timer(relock_after))
        logger.info(f"🟢 Open ({reason}) — mic → Speechmatics")
        self._notify_mic_state(listening=True)

    def _mute(self, reason: str):
        if self._state == "MUTED":
            return
        self._state = "MUTED"
        self._cancel_relock()
        logger.info(f"🔇 Muted ({reason}) — bot's turn")
        self._notify_mic_state(listening=False)

    def _lock(self, reason: str):
        self._state = "LOCKED"
        self._cancel_relock()
        self._cancel_watchdog()
        self._pending = np.zeros(0, dtype=np.int16)
        self._oww.reset()
        spoken = self._wakeword.replace("_", " ")
        logger.info(f"🔒 Locked ({reason}) — say “{spoken}” to wake")
        self._notify_mic_state(listening=False)

    def _notify_mic_state(self, listening: bool):
        """Tell TonePlayerObserver the mic just flipped — fire and forget, so
        this never adds latency to the gate's own state handling."""
        asyncio.create_task(
            self.push_frame(MicStateFrame(listening=listening), FrameDirection.DOWNSTREAM)
        )

    # ---- called by SttGateBridge ---------------------------------------------
    def notify_user_started(self):
        """User began speaking — keep the mic open, cancel the relock timer."""
        self._user_speaking = True
        self._cancel_relock()

    def notify_user_stopped(self):
        """User finished a sentence — bot's turn now."""
        self._user_speaking = False
        self._mute("turn ended")
        # If no bot speech follows (turn lost, backend down), don't stay
        # muted forever — relock so the wake word works again.
        self._start_watchdog()

    # ---- timers --------------------------------------------------------------
    async def _relock_timer(self, timeout: float):
        try:
            await asyncio.sleep(timeout)
            if not self._user_speaking:
                self._lock("no speech")
        except asyncio.CancelledError:
            pass

    def _cancel_relock(self):
        if self._relock_task and not self._relock_task.done():
            self._relock_task.cancel()
        self._relock_task = None

    def _start_watchdog(self):
        self._cancel_watchdog()
        self._watchdog_task = asyncio.create_task(self._watchdog_timer())

    async def _watchdog_timer(self):
        try:
            await asyncio.sleep(self._mute_timeout)
            if self._state == "MUTED":
                logger.warning(
                    f"⚠️  No bot response after {self._mute_timeout:.0f}s — relocking"
                )
                self._lock("no response")
        except asyncio.CancelledError:
            pass

    def _cancel_watchdog(self):
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None


class SttGateBridge(FrameProcessor):
    """Sits right after the STT: filters its output and drives the gate.

    Implements the frame-hygiene rules from the module docstring — exactly
    one LLM inference per spoken turn, no phantom turns from stale audio."""

    # The aggregator triggers inference 0.5s after the last final transcript;
    # a late final admitted within this window merges into the current turn.
    # Keep this <= that strategy timeout (0.5s in pipecat 1.5.0).
    GRACE_SECONDS = 0.5

    def __init__(self, gate: WakeWordGate):
        super().__init__()
        self._gate = gate
        self._turn_stopped_at: float | None = None
        self._turn_has_text = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InterimTranscriptionFrame):
            return  # rule 1: never forward interims

        if isinstance(frame, TranscriptionFrame):
            if self._gate.is_open:
                self._turn_has_text = True
            else:
                in_grace = (
                    not self._turn_has_text  # rule 2: only if turn is empty
                    and self._turn_stopped_at is not None
                    and time.monotonic() - self._turn_stopped_at <= self.GRACE_SECONDS
                )
                if not in_grace:
                    logger.info(f"🗑️ Dropped stale transcript: {frame.text!r}")
                    return  # rule 3: stale audio, not a new turn
                self._turn_has_text = True  # rule 2: admit exactly one

        elif isinstance(frame, UserStartedSpeakingFrame):
            if not self._gate.is_open:
                return  # rule 4: turn-start from stale audio
            self._turn_has_text = False  # a new turn begins, no text yet
            self._gate.notify_user_started()

        elif isinstance(frame, UserStoppedSpeakingFrame):
            if not self._gate.is_open:
                return  # rule 4: turn-end from stale audio
            self._turn_stopped_at = time.monotonic()
            self._gate.notify_user_stopped()

        await self.push_frame(frame, direction)


def _make_beep_wav(frequency: float, duration: float, sample_rate: int = 44100,
                   pulses: int = 1, gap: float = 0.08) -> bytes:
    """Synthesize `pulses` short sine beeps as an in-memory mono 16-bit WAV.
    A few ms of fade in/out per pulse avoid the audible click a sine makes
    when it starts/stops at a nonzero amplitude."""
    n_samples = int(sample_rate * duration)
    n_gap = int(sample_rate * gap)
    fade_samples = int(sample_rate * 0.005)  # 5 ms fade
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        for p in range(pulses):
            for i in range(n_samples):
                fade = min(1.0, i / fade_samples, (n_samples - i) / fade_samples)
                value = 0.3 * fade * math.sin(2 * math.pi * frequency * i / sample_rate)
                wav.writeframes(struct.pack("<h", int(value * 32767)))
            if p < pulses - 1:
                wav.writeframes(b"\x00\x00" * n_gap)
    return buf.getvalue()


class TonePlayerObserver(BaseObserver):
    """Plays status beeps: high = mic listening, low = mic muted/locked,
    double-high = a new approval wants attention (ask "hey jarvis, what's
    pending?"). For headless setups where you can't watch the log lines.

    Runs entirely on its OWN PyAudio stream and background thread. It only
    reacts to MicStateFrame markers (or explicit play_approval() calls) —
    it never touches transport.output() or any frame in the real pipeline,
    so beeps can't add latency to the actual mic/speaker audio path."""

    LISTENING_BEEP = _make_beep_wav(frequency=880, duration=0.12)  # high: mic on
    MUTED_BEEP = _make_beep_wav(frequency=440, duration=0.12)  # low: mic off
    APPROVAL_BEEP = _make_beep_wav(frequency=990, duration=0.1, pulses=2)

    def __init__(self, output_device_index: int | None = None):
        super().__init__()
        self._device_index = output_device_index
        self._queue: queue.Queue[bytes] = queue.Queue()
        self._pa = None  # created lazily, on the worker thread
        threading.Thread(target=self._worker, daemon=True).start()

    async def on_push_frame(self, data: FramePushed):
        # Observers see every frame at every pipeline hop. Only react to the
        # gate's original MicStateFrame emission, otherwise one state change
        # can beep multiple times as the marker propagates downstream.
        if isinstance(data.frame, MicStateFrame) and isinstance(data.source, WakeWordGate):
            beep = self.LISTENING_BEEP if data.frame.listening else self.MUTED_BEEP
            self._queue.put_nowait(beep)  # non-blocking hand-off to the worker

    def play_approval(self):
        """Called by the Announcer when a new approval is pending."""
        self._queue.put_nowait(self.APPROVAL_BEEP)

    def _worker(self):
        import pyaudio

        self._pa = pyaudio.PyAudio()
        while True:
            wav_bytes = self._queue.get()
            try:
                self._play(wav_bytes)
            except Exception as e:
                logger.debug(f"Tone playback failed (non-fatal): {e}")

    def _play(self, wav_bytes: bytes):
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            stream = self._pa.open(
                format=self._pa.get_format_from_width(wav.getsampwidth()),
                channels=wav.getnchannels(),
                rate=wav.getframerate(),
                output=True,
                output_device_index=self._device_index,
            )
            try:
                stream.write(wav.readframes(wav.getnframes()))
            finally:
                stream.stop_stream()
                stream.close()


class TranscriptLogObserver(BaseObserver):
    """Prints 'You:' / 'Bot:' lines plus per-turn LLM/error diagnostics."""

    def __init__(self):
        super().__init__()
        self._reply = ""

    async def on_push_frame(self, data: FramePushed):
        src, frame = data.source, data.frame
        if isinstance(frame, TranscriptionFrame) and isinstance(src, STTService):
            logger.info(f"🧑 You: {frame.text}")
        elif isinstance(frame, LLMContextFrame) and "User" in type(src).__name__:
            # Exactly one per spoken turn is healthy; two = duplicate
            # trigger; zero = the turn was lost before the LLM.
            logger.info("🧠 Kernel turn triggered")
        elif isinstance(frame, ErrorFrame):
            logger.error(f"❌ Pipeline error from {src}: {frame.error}")
        elif isinstance(frame, LLMTextFrame) and isinstance(src, LLMService):
            self._reply += frame.text
        elif isinstance(frame, LLMFullResponseEndFrame) and isinstance(src, LLMService):
            if self._reply.strip():
                logger.info(f"🤖 Bot: {self._reply.strip()}")
            self._reply = ""


def list_audio_devices():
    """Print PyAudio device indices/names, e.g. to configure a Pi's mic/speaker."""
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        print(f"{'idx':>4}  {'in':>3} {'out':>3}  name")
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            print(
                f"{i:>4}  {info['maxInputChannels']:>3} {info['maxOutputChannels']:>3}  "
                f"{info['name']}"
            )
        try:
            print(f"\nDefault input:  {pa.get_default_input_device_info()['name']}")
        except OSError:
            print("\nDefault input:  (none)")
        try:
            print(f"Default output: {pa.get_default_output_device_info()['name']}")
        except OSError:
            print("Default output: (none)")
        print(
            "\nSet voice.input_device_index / voice.output_device_index in "
            "config.yaml to override the default."
        )
    finally:
        pa.terminate()
