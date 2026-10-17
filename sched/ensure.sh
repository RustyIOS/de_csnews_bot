#!/usr/bin/env bash
# Idempotent: start the box scheduler (watchdog + scheduler.py) if not running; also restart it if its tick is stale.
# Safe to call from any agent / routine / shell at any time. Prints one status line. Never prints secrets.
cd "$(dirname "$0")" || exit 1
TICK=../logs/scheduler_tick.json
alive_wd() { [ -f .watchdog.pid ] && kill -0 "$(cat .watchdog.pid)" 2>/dev/null && ! flock -n .watchdog.lock true; }
stale() { [ ! -f "$TICK" ] || [ $(( $(date +%s) - $(stat -c %Y "$TICK") )) -gt 180 ]; }
if alive_wd && ! stale; then echo "scheduler OK (watchdog $(cat .watchdog.pid), tick $(date -r $TICK '+%T'))"; exit 0; fi
if alive_wd && stale; then
  echo "scheduler tick stale -> restarting"; pkill -f "python3 scheduler.py" ; sleep 2
  stale || { echo "scheduler restarted by watchdog"; exit 0; }
  kill "$(cat .watchdog.pid)" 2>/dev/null; pkill -f "python3 scheduler.py"; sleep 1
fi
setsid nohup ./watchdog.sh >/dev/null 2>&1 < /dev/null &
sleep 3
echo "scheduler STARTED (watchdog $(cat .watchdog.pid 2>/dev/null))"
