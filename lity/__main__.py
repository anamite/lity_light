"""Entry point: `python -m lity [config.yaml] [--voice|--no-voice]`."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import uvicorn


def _load_dotenv():
    env = Path(".env")
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _parse_args():
    p = argparse.ArgumentParser(prog="lity")
    p.add_argument("config", nargs="?", default="config.yaml",
                   help="path to config.yaml (default: ./config.yaml)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--voice", action="store_true",
                   help="run the voice assistant (overrides voice.enabled)")
    g.add_argument("--no-voice", action="store_true",
                   help="disable the voice assistant (overrides voice.enabled)")
    p.add_argument("--list-devices", action="store_true",
                   help="list audio input/output devices and exit")
    return p.parse_args()


async def main():
    _load_dotenv()
    args = _parse_args()

    if args.list_devices:
        from .voicebot import missing_deps
        miss = missing_deps()
        if miss:
            print(f"voice deps missing ({miss}) — run ./install.sh with voice enabled.")
            sys.exit(1)
        from .voicebot.pipeline import list_audio_devices
        list_audio_devices()
        return

    from .app import App
    from .gateway.api import create_app

    core = App(args.config)
    if args.voice or args.no_voice:
        core.cfg.setdefault("voice", {})["enabled"] = bool(args.voice)
    await core.start()

    key_env = core.cfg.get_path("provider.api_key_env", "OPENROUTER_API_KEY")
    if not os.environ.get(key_env):
        print(f"WARNING: {key_env} is not set — the agent cannot reach the model provider.")

    host = core.cfg.get_path("server.host", "0.0.0.0")
    port = int(core.cfg.get_path("server.port", 8321))
    print(f"Lity is up -> http://{'localhost' if host == '0.0.0.0' else host}:{port}")

    server = uvicorn.Server(uvicorn.Config(create_app(core), host=host, port=port,
                                           log_level="warning"))
    try:
        await server.serve()
    finally:
        await core.stop()


if __name__ == "__main__":
    asyncio.run(main())
