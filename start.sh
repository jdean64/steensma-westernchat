#!/usr/bin/env bash
# West Chat — startup script
# Usage: ./start.sh [--dev]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/venv"
PORT=8088
WORKERS=2
TIMEOUT=120

# Create/activate venv
if [[ ! -d "$VENV" ]]; then
    python3 -m venv "$VENV"
fi
source "$VENV/bin/activate"
pip install -q --upgrade pip
pip install -q -r requirements.txt

if [[ "${1:-}" == "--dev" ]]; then
    export FLASK_ENV=development
    exec python app.py
else
    exec gunicorn \
        --bind "127.0.0.1:$PORT" \
        --workers "$WORKERS" \
        --timeout "$TIMEOUT" \
        --access-logfile - \
        --error-logfile - \
        "app:app"
fi
