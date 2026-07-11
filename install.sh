#!/usr/bin/env bash
# Lity plug-and-play installer (Linux / Raspberry Pi OS 64-bit / macOS / WSL2)
set -euo pipefail
cd "$(dirname "$0")"

echo "── Lity installer ──────────────────────────────────────"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

if [ ! -d .venv ]; then
  echo "• creating virtualenv"
  python3 -m venv .venv
fi
source .venv/bin/activate

echo "• installing dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  read -rp "• OpenRouter API key (sk-or-...): " KEY
  echo "OPENROUTER_API_KEY=$KEY" > .env
  echo "  (add HERMES_API_KEY=<your Hermes gateway API_SERVER_KEY> to .env too)"
fi

if command -v systemctl >/dev/null && [ "$(uname)" = "Linux" ]; then
  read -rp "• install systemd service so Lity starts on boot? [y/N] " YN
  if [ "${YN,,}" = "y" ]; then
    sudo tee /etc/systemd/system/lity.service >/dev/null <<EOF
[Unit]
Description=Lity personal agent
After=network-online.target

[Service]
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/python -m lity
Restart=on-failure
User=$USER

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now lity
    echo "• service installed and started"
  fi
fi

PORT=$(grep -A2 '^server:' config.yaml | grep port | awk '{print $2}')
echo "── done. Start with:  .venv/bin/python -m lity"
echo "   then open http://localhost:${PORT:-8321}"
