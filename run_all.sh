#!/bin/bash
# x-brain production supervisor: enrich everything, then LLM everything, then retry pass
# Usage: ./run_all.sh [--brain-dir ./data]  (or set XBRAIN_DIR in .env)
set -e
HARNESS_DIR="$(cd "$(dirname "$0")/harness" && pwd)"
BRAIN_DIR="${XBRAIN_DIR:-${X_BRAIN_DIR:-}}"
EXTRA_ARGS=""
if [[ -n "$BRAIN_DIR" ]]; then EXTRA_ARGS="--brain-dir $BRAIN_DIR"; fi
# allow explicit --brain-dir passthrough
if [[ "$1" == "--brain-dir" ]]; then EXTRA_ARGS="--brain-dir $2"; shift 2; fi
cd "$HARNESS_DIR"
echo "=== enrich pass 1: $(date) ==="
python3 -u xb.py $EXTRA_ARGS enrich --batch 200
echo "=== llm pass 1: $(date) ==="
python3 -u xb.py $EXTRA_ARGS llm-run
echo "=== enrich pass 2 (retries): $(date) ==="
python3 -u xb.py $EXTRA_ARGS enrich --batch 200
echo "=== llm pass 2: $(date) ==="
python3 -u xb.py $EXTRA_ARGS llm-run
echo "=== supervisor done: $(date) ==="
python3 xb.py $EXTRA_ARGS stats
