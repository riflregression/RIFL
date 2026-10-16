#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
: "${OPENAI_API_KEY:?Set OPENAI_API_KEY in the environment}"
exec python "$ROOT/rifl/safemin.py" \
  --config "$ROOT/rifl/config/default.yaml" \
  --all-passed --output "$ROOT/results/featbench-all" "$@"
