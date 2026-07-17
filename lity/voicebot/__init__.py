"""In-process voice assistant: mic → wake word → Speechmatics STT → kernel →
Kokoro TTS → speaker, all inside the Lity process (no HTTP loopback).

Ported from pipy_catty's bot_simple.py; the wake-word gate and the STT
frame-hygiene rules are unchanged. What's different in-process:
  - the pipeline's "LLM" calls core.kernel directly (service.LityLLMService);
  - proactive task results / approval beeps arrive by EventBus push
    (service.Announcer) instead of polling GET /v1/voice/pending.

Voice deps (requirements-voice.txt) are OPTIONAL: without them Lity runs
normally and this module just logs why voice is off."""

VOICE_DEPS = {"pipecat": "pipecat-ai", "openwakeword": "openwakeword",
              "soxr": "soxr", "pyaudio": "pyaudio (portaudio19-dev)",
              "numpy": "numpy"}


def missing_deps() -> str | None:
    """Comma-separated missing packages, or None when all importable."""
    import importlib.util
    missing = [pkg for mod, pkg in VOICE_DEPS.items()
               if importlib.util.find_spec(mod) is None]
    return ", ".join(missing) or None


async def run(core):
    """Entry point owned by App.start — never raises into the server."""
    import asyncio
    miss = missing_deps()
    if miss:
        print(f"[voice] disabled — missing deps: {miss}. "
              "Run ./install.sh and answer yes to the voice assistant step.")
        return
    from .bot import run_bot
    try:
        await run_bot(core)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[voice] crashed: {e!r} — Lity keeps running without voice.")
