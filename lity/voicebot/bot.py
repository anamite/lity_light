"""Builds and runs the voice pipeline inside the Lity process.

    mic → WakeWordGate → Speechmatics STT → SttGateBridge
        → context aggregator → LityLLMService (the kernel, in-process)
        → Kokoro TTS (local) → speaker

Runs as an asyncio task next to uvicorn (started by App.start when
voice.enabled). Cancellation from App.stop shuts the pipeline down cleanly."""

import asyncio
import os
import sys
import warnings

from loguru import logger

from .pipeline import (
    SttGateBridge,
    TonePlayerObserver,
    TranscriptLogObserver,
    VoiceSettings,
    WakeWordGate,
)
from .service import Announcer, LityLLMService


def _language(code: str):
    from pipecat.transcriptions.language import Language

    try:
        return Language(code)
    except ValueError:
        logger.warning(f"voice.language {code!r} unknown — falling back to en")
        return Language.EN


async def run_bot(core):
    # Pipecat 1.5.0 soft-deprecates PipelineTask/PipelineRunner for the newer
    # Worker API, but the task/runner pattern is still the documented
    # mainstream one and works through 2.0. Keep the log readable.
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    settings = VoiceSettings.from_cfg(core.cfg)

    key = os.environ.get(settings.speechmatics_api_key_env, "")
    if not key:
        print(f"[voice] disabled — {settings.speechmatics_api_key_env} is not set "
              "in .env (needed for Speechmatics STT).")
        return

    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
    )
    from pipecat.services.kokoro.tts import KokoroTTSService
    from pipecat.services.speechmatics.stt import SpeechmaticsSTTService
    from pipecat.transports.local.audio import (
        LocalAudioTransport,
        LocalAudioTransportParams,
    )

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            input_device_index=settings.input_device_index,
            output_device_index=settings.output_device_index,
        )
    )

    gate = WakeWordGate(settings)
    bridge = SttGateBridge(gate)

    stt = SpeechmaticsSTTService(
        api_key=key,
        settings=SpeechmaticsSTTService.Settings(
            language=_language(settings.language),
            # The default (EXTERNAL) waits for a separate VAD to end turns,
            # which this pipeline doesn't have — use the server's built-in
            # ADAPTIVE endpointing instead.
            turn_detection_mode=SpeechmaticsSTTService.TurnDetectionMode.ADAPTIVE,
            # Max pause that still counts as "thinking" rather than "done".
            end_of_utterance_silence_trigger=settings.eou_silence_trigger,
        ),
    )

    llm = LityLLMService(core)  # the kernel, in-process — no HTTP loopback

    # Local Kokoro TTS (ONNX, no API key). Model files auto-download to
    # ~/.cache/pipecat/kokoro-onnx/ unless the paths point at an install.
    tts = KokoroTTSService(
        model_path=settings.kokoro_model_path,
        voices_path=settings.kokoro_voices_path,
        settings=KokoroTTSService.Settings(
            voice=settings.tts_voice,
            language=_language(settings.language),
        ),
    )

    # Lity keeps conversation memory server-side; each turn just carries the
    # latest user utterance, so the context starts empty.
    context_aggregator = LLMContextAggregatorPair(LLMContext())

    tones = TonePlayerObserver(output_device_index=settings.output_device_index)

    pipeline = Pipeline(
        [
            transport.input(),
            gate,                       # wake word / mute / barge-in
            stt,
            bridge,                     # frame hygiene + turn signals to gate
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True),
        observers=[TranscriptLogObserver(), tones],
        # We can sit LOCKED (no speaking frames) for long stretches waiting
        # for the wake word — that's normal, not a stuck pipeline.
        idle_timeout_secs=None,
    )

    announcer = Announcer(core, gate, tones, task)
    announcer_task = asyncio.create_task(announcer.run())

    # uvicorn owns signal handling in this process.
    runner = PipelineRunner(handle_sigint=False)

    spoken = settings.wake_word.replace("_", " ")
    print(f"[voice] assistant ready — say “{spoken}” to wake it.")
    logger.info(f"🔒 Locked — say “{spoken}” to wake")

    try:
        await runner.run(task)
    except asyncio.CancelledError:
        await task.cancel()
        raise
    finally:
        announcer_task.cancel()
