#!/usr/bin/env bash
# Launch the pipeline web app on localhost.
#
# MVP: single FastAPI process; worker thread runs in-process.
# To swap to Celery + Redis later, install with: pip install -e ".[queue]"
# and use bin/run-api.sh + bin/run-worker.sh (separate processes).
set -euo pipefail

cd "$(dirname "$0")/.."

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8888}"

PY="$(command -v python3 || command -v python)"

# Make `pipeline_app` importable from the project root.
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

exec "$PY" -m uvicorn pipeline_app.api:app --host "$HOST" --port "$PORT" --reload --log-level info