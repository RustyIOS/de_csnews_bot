#!/usr/bin/env bash
# Supervised loop: keeps scheduler.py alive (restart 5 s after any exit). Single instance via flock.
cd "$(dirname "$0")" || exit 1
exec 9>.watchdog.lock
flock -n 9 || { echo "watchdog already running"; exit 0; }
echo $$ > .watchdog.pid
while true; do
  python3 scheduler.py >> ../logs/scheduler.out 2>&1
  rc=$?
  echo "[$(date '+%F %T')] scheduler exited rc=$rc, restarting in 5 s" >> ../logs/scheduler.log
  sleep 5
done
