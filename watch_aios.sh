#!/usr/bin/env bash
clear
echo "=== AI-OS LIVE MONITOR ==="
echo

while true; do
  echo "TIME: $(date)"
  echo "-----------------------------"
  ./bin/aiosctl status
  echo
  echo "LAST AUTOPILOT LOG:"
  tail -n 5 runtime/autopilot.log 2>/dev/null
  echo
  echo "LAST JOB:"
  ls -t runtime/jobs 2>/dev/null | head -n 1
  echo "============================="
  sleep 5
  clear
done
