"""Entry point: `python -m lity [config.yaml]`."""

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


async def main():
    _load_dotenv()
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"

    from .app import App
    from .gateway.api import create_app

    core = App(config_path)
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
