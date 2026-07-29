#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
python3 scripts/harness_entrypoint.py \
  --input smoke/input.json \
  --output smoke/output_codex \
  --backend codex \
  --model "${CODEX_SWARM_MODEL:-}" \
  --max-parallel "${SWARM_MAX_PARALLEL:-2}"

