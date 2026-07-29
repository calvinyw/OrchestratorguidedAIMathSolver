#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
python3 scripts/harness_entrypoint.py \
  --input smoke/input.json \
  --output smoke/output_local \
  --backend "${SWARM_BACKEND:-mock}"

