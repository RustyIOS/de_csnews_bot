#!/usr/bin/env bash
# Called by the live routine every run. Starts collector.py detached (setsid, survives the caller) ONLY if a notable
# match is live or starts within 15 min (per matches.py --today); restarts it if the heartbeat is > 3 min old;
# otherwise does nothing. Extra args are passed to collector.py (tests: --sim-schedule F --queue F ...).
cd "$(dirname "$0")" || exit 1
mkdir -p logs
exec python3 collector.py --ensure "$@"
