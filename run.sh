#!/usr/bin/env bash
# Controller entry point. Contract: ./run.sh <SIM_URL> runs until the scenario finishes.
# Optional environment:
#   CONTROLLER_STATE_DIR  durable state directory (default: state)
#   CONTROLLER_STEP_MINUTES  simulated minutes per loop tick (default: 60)
#   CONTROLLER_DB / CONTROLLER_LOG  explicit store and JSONL log paths
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python
fi
exec "$PYTHON" -m controller run --url "$SIM_URL" --state "${CONTROLLER_STATE_DIR:-state}"
