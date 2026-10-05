#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"
PORT=${PORT:-8000}

if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'Python 3.11 or newer is required.' >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
fi

FFMPEG_EXECUTABLE=$(.venv/bin/python -c "from dotenv import load_dotenv; import os; load_dotenv(); print(os.getenv('FFMPEG_PATH') or 'ffmpeg')")
if ! "$FFMPEG_EXECUTABLE" -version >/dev/null 2>&1; then
  printf '%s\n' 'FFmpeg is required. Install it and add it to PATH, or set FFMPEG_PATH in .env. Docker includes FFmpeg.' >&2
  exit 1
fi

printf 'FrameForge is starting at http://127.0.0.1:%s\n' "$PORT"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
