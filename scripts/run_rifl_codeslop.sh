#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
: "${OPENAI_API_KEY:?Set OPENAI_API_KEY in the environment}"
PROBLEM=${1:-execution_server}
OUT=${2:-"$ROOT/results/slopcode-$PROBLEM"}
shift 2 2>/dev/null || true
exec python "$ROOT/rifl/codeslop_smoke.py" \
  --problem "$PROBLEM" --output "$OUT" "$@"
