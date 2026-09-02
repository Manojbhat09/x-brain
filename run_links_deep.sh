#!/bin/bash
# link resolution -> deep curation chain (runs alongside text/vision passes)
set -e
HARNESS_DIR="$(cd "$(dirname "$0")/harness" && pwd)"
BRAIN_DIR="${XBRAIN_DIR:-${X_BRAIN_DIR:-$HOME/.xbrain}}"
if [[ -f "$(dirname "$0")/.env" ]]; then set -a; source "$(dirname "$0")/.env" 2>/dev/null || true; set +a; BRAIN_DIR="${XBRAIN_DIR:-$BRAIN_DIR}"; fi
EXTRA=""; [[ -n "$XBRAIN_DIR" || -n "$X_BRAIN_DIR" ]] && EXTRA="--brain-dir $BRAIN_DIR"
cd "$HARNESS_DIR"
echo "=== links pass: $(date) ==="
python3 -u xb.py $EXTRA links-run
echo "=== deep pass: $(date) ==="
python3 -u xb.py $EXTRA deep-run
echo "=== deep retry pass: $(date) ==="
python3 -u xb.py $EXTRA deep-run
echo "=== links+deep done: $(date) ==="
