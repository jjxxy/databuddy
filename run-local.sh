#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 bootstrap.py
if [ ! -d .venv ]; then python3 -m venv .venv; fi
.venv/bin/pip install -r requirements.lock
set -a
source .env
set +a
export DATA_DIR="$PWD/data"
export SCHEDULER_MODE=worker
# Legacy developer launcher uses the Atlas loop explicitly. The self-contained
# desktop bundle selects OpenCode separately; this script is not its entrypoint.
export ATLAS_OPENCODE_RUNTIME=0
.venv/bin/python -m agent_service.worker &
worker_pid=$!
trap 'kill "$worker_pid" 2>/dev/null || true' EXIT INT TERM
.venv/bin/uvicorn agent_service.api:app --host 127.0.0.1 --port 8000
