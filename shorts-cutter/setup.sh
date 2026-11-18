#!/bin/bash
# One-time setup: FFmpeg (via Homebrew) + Python packages in a local .venv.
set -e
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null; then
  if command -v brew >/dev/null; then brew install ffmpeg; else
    echo "Install FFmpeg first: https://ffmpeg.org/download.html"; exit 1; fi
fi
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
mkdir -p data "$HOME/shorts-output"
echo "✅ Setup done. Start with: ./run.sh"
