#!/usr/bin/env bash
# Lity plug-and-play installer (Linux / Raspberry Pi OS 64-bit / macOS / WSL2)
# One script sets up everything: core Lity + (optionally) the in-process
# voice assistant (wake word, Speechmatics STT, local Kokoro TTS).
set -euo pipefail
cd "$(dirname "$0")"

echo "── Lity installer ──────────────────────────────────────"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

# The voice assistant needs pipecat-ai (Python >=3.11) and audio system libs.
WITH_VOICE=n
read -rp "• set up the voice assistant too (mic + speaker + wake word)? [Y/n] " YN
[ "${YN,,}" != "n" ] && WITH_VOICE=y

if [ "$WITH_VOICE" = "y" ] && [ "$(uname)" = "Linux" ]; then
  echo "• installing system packages for audio (requires sudo)"
  # portaudio19-dev: PyAudio needs PortAudio headers to build.
  # build-essential: compilers for PyAudio and any other sdist builds.
  sudo apt-get update
  sudo apt-get install -y portaudio19-dev build-essential
fi

if [ ! -d .venv ]; then
  echo "• creating virtualenv"
  python3 -m venv .venv
fi
source .venv/bin/activate

if [ "$WITH_VOICE" = "y" ]; then
  python - <<'PYEOF'
import sys
if sys.version_info < (3, 11):
    print(f"ERROR: Python {sys.version.split()[0]} in .venv, but the voice "
          "assistant (pipecat-ai) needs >=3.11.", file=sys.stderr)
    sys.exit(1)
PYEOF
fi

echo "• installing dependencies"
pip install --quiet --upgrade pip setuptools wheel
pip install --quiet -r requirements.txt

if [ "$WITH_VOICE" = "y" ]; then
  echo "• installing voice dependencies (pipecat, Kokoro TTS — this can take a while)"
  pip install --quiet -r requirements-voice.txt
  # openwakeword hard-requires tflite-runtime on Linux, which has no wheel
  # for many Pi Python builds, even though only the ONNX backend is used.
  # Install its real runtime deps ourselves, then openwakeword with
  # --no-deps to skip the unavailable tflite-runtime requirement.
  pip install --quiet \
      "onnxruntime<2,>=1.10.0" "tqdm<5.0,>=4.0" "scipy<2,>=1.3" \
      "scikit-learn<2,>=1" "requests<3,>=2.0"
  pip install --quiet --no-deps openwakeword==0.6.0
fi

# Provider, models and API keys: one guided wizard, stored in config.yaml/.env.
# Re-run anytime with ./lityctl setup; change single values with ./lityctl set.
if [ -t 0 ]; then
  echo "• settings wizard (Enter keeps current values)"
  python -m lity.setup
else
  echo "• non-interactive shell — run ./lityctl setup afterwards to add keys"
fi

if command -v systemctl >/dev/null && [ "$(uname)" = "Linux" ]; then
  read -rp "• install systemd service so Lity starts on boot? [y/N] " YN
  if [ "${YN,,}" = "y" ]; then
    sudo tee /etc/systemd/system/lity.service >/dev/null <<EOF
[Unit]
Description=Lity personal agent
After=network-online.target sound.target

[Service]
WorkingDirectory=$(pwd)
Environment=PYTHONUNBUFFERED=1
ExecStart=$(pwd)/.venv/bin/python -m lity
Restart=on-failure
User=$USER

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now lity
    echo "• service installed and started"
    echo "  (voice on boot: set voice.enabled: true in config.yaml, then"
    echo "   sudo systemctl restart lity)"
  fi
fi

PORT=$(grep -A2 '^server:' config.yaml | grep port | awk '{print $2}')
echo "── done."
echo "   Start:        ./lityctl start          (web UI only)"
if [ "$WITH_VOICE" = "y" ]; then
  echo "   With voice:   ./lityctl start --voice  (or set voice.enabled: true)"
  echo "   Audio devices: ./lityctl devices       (indices for config.yaml)"
fi
echo "   Then open http://localhost:${PORT:-8321}"
