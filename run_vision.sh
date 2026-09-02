#!/bin/bash
# Resilient vision worker: auto-restarts on crash. Logs to $XBRAIN_DIR/vision.log
set -e
HARNESS_DIR="$(cd "$(dirname "$0")/harness" && pwd)"
BRAIN_DIR="${XBRAIN_DIR:-${X_BRAIN_DIR:-$HOME/.xbrain}}"
if [[ -f "$(dirname "$0")/.env" ]]; then set -a; source "$(dirname "$0")/.env" 2>/dev/null || true; set +a; BRAIN_DIR="${XBRAIN_DIR:-$BRAIN_DIR}"; fi
EXTRA=""; [[ -n "$XBRAIN_DIR" || -n "$X_BRAIN_DIR" ]] && EXTRA="--brain-dir $BRAIN_DIR"
mkdir -p "$BRAIN_DIR"
LOG="$BRAIN_DIR/vision.log"
cd "$HARNESS_DIR"
while true; do
  python3 -u xb.py $EXTRA vision-run --workers 2 2>&1 | while IFS= read -r line; do echo "$(date '+%Y-%m-%d %H:%M:%S %Z') $line"; done >> "$LOG" 2>&1
  echo "$(date '+%Y-%m-%d %H:%M:%S %Z') === worker exited, restarting in 10s ===" >> "$LOG"
  sleep 10
done
