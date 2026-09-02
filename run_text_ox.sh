#!/bin/bash
# Resilient text worker: auto-restarts on crash. Logs to $XBRAIN_DIR/llm_ox.log
set -e
HARNESS_DIR="$(cd "$(dirname "$0")/harness" && pwd)"
BRAIN_DIR="${XBRAIN_DIR:-${X_BRAIN_DIR:-$HOME/.xbrain}}"
# honour .env if present
if [[ -f "$(dirname "$0")/.env" ]]; then set -a; source "$(dirname "$0")/.env" 2>/dev/null || true; set +a; BRAIN_DIR="${XBRAIN_DIR:-$BRAIN_DIR}"; fi
EXTRA=""; [[ -n "$XBRAIN_DIR" || -n "$X_BRAIN_DIR" ]] && EXTRA="--brain-dir $BRAIN_DIR"
mkdir -p "$BRAIN_DIR"
LOG="$BRAIN_DIR/llm_ox.log"
cd "$HARNESS_DIR"
while true; do
  python3 -u xb.py $EXTRA llm-run --workers 4 2>&1 | while IFS= read -r line; do echo "$(date '+%Y-%m-%d %H:%M:%S %Z') $line"; done >> "$LOG" 2>&1
  echo "$(date '+%Y-%m-%d %H:%M:%S %Z') === text worker exited, restarting in 10s ===" >> "$LOG"
  sleep 10
done
