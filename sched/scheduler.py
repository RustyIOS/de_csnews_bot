#!/usr/bin/env python3
"""Box job scheduler for @de_csnews (cron replacement; cron is not reliable on this box).

Runs as user box, started by watchdog.sh (supervised loop), which is started by ensure.sh.
Jobs: jobs.json (re-read every tick). Times are Europe/Moscow.
  {"name": .., "cmd": [..], "minutes": "*/10" | "4" | "34",  "hours": "*" | "10" | "10-23",
   "catchup_min": N   # if the scheduler was down at the slot (box restart / freeze), still run it
                      # up to N minutes late, same day only. Default 0 (no catch-up).
   "timeout": sec, "enabled": true}
State: state.json (last slot run per job) -> each slot runs at most once, even across restarts.
Log: ../logs/scheduler.log. Heartbeat file: ../logs/scheduler_tick.json (last tick time, pid).
Secrets: jobs get the env of run_auto.sh / .cron.env only when they call it; the scheduler never reads or logs tokens.
"""
import fcntl, json, os, subprocess, sys, time, datetime as dt
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
LOGS = os.path.join(BASE, "logs")
JOBS = os.path.join(HERE, "jobs.json")
STATE = os.path.join(HERE, "state.json")
TICK = os.path.join(LOGS, "scheduler_tick.json")
LOG = os.path.join(LOGS, "scheduler.log")
os.makedirs(LOGS, exist_ok=True)


def log(msg):
    line = f"[{dt.datetime.now(MSK):%F %T}] {msg}\n"
    with open(LOG, "a") as f:
        f.write(line)
    try:
        if os.path.getsize(LOG) > 2_000_000:
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass


def field_match(spec, v):
    for part in str(spec).split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, s = part.split("/"); step = int(s)
        if part in ("*", ""):
            lo, hi = 0, 59
        elif "-" in part:
            lo, hi = map(int, part.split("-"))
        else:
            lo = hi = int(part)
            if step == 1 and v == lo:
                return True
            continue
        if lo <= v <= hi and (v - lo) % step == 0:
            return True
    return False


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


running = {}  # name -> (Popen, started_monotonic, timeout)


def start(job, slot):
    name = job["name"]
    p = running.get(name)
    if p and p[0].poll() is None:
        log(f"SKIP {name} slot {slot}: previous run still active (pid {p[0].pid})")
        return False
    out = open(os.path.join(LOGS, f"sched_{name}.out"), "a")
    out.write(f"===== {dt.datetime.now(MSK):%F %T} slot {slot}\n"); out.flush()
    env = dict(os.environ, TZ="Europe/Moscow", PATH="/usr/local/bin:/usr/bin:/bin", HOME="/home/box")
    proc = subprocess.Popen(job["cmd"], cwd=BASE, stdout=out, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    out.close()
    running[name] = (proc, time.monotonic(), job.get("timeout", 1800))
    log(f"RUN {name} slot {slot} pid {proc.pid}: {' '.join(job['cmd'])}")
    return True


def reap():
    for name, (proc, t0, to) in list(running.items()):
        rc = proc.poll()
        if rc is not None:
            log(f"DONE {name} pid {proc.pid} rc={rc} in {time.monotonic()-t0:.0f}s")
            del running[name]
        elif time.monotonic() - t0 > to:
            log(f"TIMEOUT {name} pid {proc.pid} after {to}s -> kill")
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass


def due_slot(job, now):
    """Latest slot (minute-aligned datetime) for this job that is <= now, within catchup window, same day."""
    win = int(job.get("catchup_min", 0))
    for back in range(0, win + 1):
        t = (now - dt.timedelta(minutes=back)).replace(second=0, microsecond=0)
        if t.date() != now.date():
            break
        if field_match(job.get("hours", "*"), t.hour) and field_match(job.get("minutes", "*"), t.minute):
            return t
    return None


def main():
    lockf = open(os.path.join(HERE, ".scheduler.lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("scheduler already running"); return 0
    lockf.write(str(os.getpid())); lockf.flush()
    log(f"START scheduler pid {os.getpid()}")
    state = load(STATE, {})
    while True:
        now = dt.datetime.now(MSK)
        jobs = load(JOBS, [])
        for job in jobs:
            if not job.get("enabled", True):
                continue
            slot = due_slot(job, now)
            if slot is None:
                continue
            key = slot.strftime("%F %H:%M")
            if state.get(job["name"]) == key:
                continue
            late = int((now - slot).total_seconds() // 60)
            if late > 0 and state.get(job["name"], "") < key:
                log(f"CATCHUP {job['name']} slot {key} ({late} min late)")
            if start(job, key):
                state[job["name"]] = key
                save(STATE, state)
        reap()
        save(TICK, {"tick_msk": now.strftime("%F %T"), "pid": os.getpid(),
                    "running": {k: v[0].pid for k, v in running.items()}})
        time.sleep(max(1, 20 - (time.time() % 20)))


if __name__ == "__main__":
    sys.exit(main())
