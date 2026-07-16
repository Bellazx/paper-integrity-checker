#!/usr/bin/env bash
set -u

cd /home/sjtu/paper-integrity-checker || exit 1

export PAPER_PREPROCESS=0

if [[ "${ENABLE_4050_SUPERVISOR:-0}" != "1" ]]; then
  echo "screen_4050 supervisor disabled; set ENABLE_4050_SUPERVISOR=1 to run"
  exit 2
fi

while true; do
  echo "===== $(date -Is) start screen_4050 single worker ====="
  python3 screen_4050.py --workers 1 --skip-done
  code=$?
  echo "===== $(date -Is) screen_4050 exited code=${code}; restart in 60s ====="
  sleep 60
done
