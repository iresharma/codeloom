#!/usr/bin/env bash
# One command: kicks off run_all.py for every target in targets.json, kept
# awake with caffeinate, detached with nohup so closing the terminal doesn't
# kill it. Check progress with `cat pr_trials/overnight_status.json | jq` or
# `tail -f pr_trials/overnight.log`; final results land in
# pr_trials/overnight_summary.md and pr_trials/reports/.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f pr_trials/overnight_status.json ]; then
  echo "pr_trials/overnight_status.json already exists from a previous run." >&2
  echo "Move it aside (and pr_trials/runs/*) before starting a new batch." >&2
  exit 1
fi

caffeinate -dims nohup python3 pr_trials/run_all.py \
  > pr_trials/overnight.log 2>&1 &
disown

echo "started, pid $!"
echo "watch:  tail -f pr_trials/overnight.log"
echo "status: cat pr_trials/overnight_status.json"
echo "when done: pr_trials/overnight_summary.md and pr_trials/reports/"
