#!/usr/bin/env python3
"""GitHub Actions entry for the no-AI channel jobs.

  python3 scripts/gha_run.py announce --dry-run
  python3 scripts/gha_run.py results --dry-run
  python3 scripts/gha_run.py live --dry-run
  python3 scripts/gha_run.py tick                 # same decision as cron / job=auto
  python3 scripts/gha_run.py --preflight

Schedule (UTC, Moscow is UTC+3 year-round) and workflow_dispatch with no inputs
or job=auto share one decision: Moscow time picks announce, results or live.
A real send happens only when POSTING_ENABLED is true and dry_run is not set.
Calling that path often is safe: one announce and one results post per day, live
posts nothing when there is nothing new, and a second overlapping run waits.
"""
import os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("TZ", "Europe/Moscow")
try:
    time.tzset()
except Exception:
    pass
os.environ.setdefault("NO_SCHED", "1")
os.environ.setdefault("NO_COLLECTOR", "1")
os.environ.setdefault("NO_NEWS_QUEUE", "1")

from common import now_msk  # noqa: E402
from auto_lib import state  # noqa: E402


def minutes(t):
    return t.hour * 60 + t.minute


def in_announce(t):
    m = minutes(t)
    return 10 * 60 + 4 <= m <= 12 * 60


def in_results(t):
    m = minutes(t)
    return 23 * 60 + 34 <= m <= 23 * 60 + 59


def in_live(t):
    m = minutes(t)
    return 10 * 60 <= m <= 23 * 60 + 59


def tick_choice(now, st):
    day = now.date().isoformat()
    if in_announce(now) and day not in (st.get("announced_dates") or []):
        return "announce"
    if in_results(now) and day not in (st.get("results_dates") or []):
        return "results"
    if in_live(now):
        return "live"
    return None


def posting_enabled():
    return os.environ.get("POSTING_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def dispatch_job():
    """job input, or the legacy mode input. Empty / auto / tick follow the clock."""
    job = (os.environ.get("GHA_JOB") or os.environ.get("GHA_MODE") or "auto").strip().lower()
    if job in ("", "auto", "tick", "schedule"):
        return "tick", "schedule"
    if job in ("announce", "results", "live"):
        return job, "dispatch"
    print(f"unknown job {job!r}; using auto", flush=True)
    return "tick", "schedule"


def resolve(cli_mode):
    if cli_mode:
        # CLI `tick` is the same clock decision as cron and job=auto.
        if cli_mode == "tick":
            return "tick", "schedule"
        return cli_mode, "cli"
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event == "workflow_dispatch":
        return dispatch_job()
    if event == "schedule":
        sched = (os.environ.get("GITHUB_EVENT_SCHEDULE") or "").strip()
        if sched == "4 7 * * *":
            return "announce", "schedule"
        if sched == "34 20 * * *":
            return "results", "schedule"
        return "tick", "schedule"
    return "tick", "cli"


def want_dry(cli_dry):
    if cli_dry or os.environ.get("GHA_DRY_RUN", "").strip().lower() == "true":
        return True
    if os.environ.get("GITHUB_ACTIONS") == "true" and not posting_enabled():
        print("POSTING_ENABLED is not true — nothing will be sent (dry-run)", flush=True)
        return True
    return False


def plan(mode, source, dry):
    """Return (action, concrete_mode). action is 'run' or 'skip'."""
    now = now_msk()
    st = state()
    day = now.date().isoformat()
    concrete = mode
    if source == "schedule" and mode == "announce" and not in_announce(now):
        print(f"skip: announce outside 10:04–12:00 MSK (now {now:%H:%M})", flush=True)
        return "skip", mode
    if source == "schedule" and mode == "results" and not in_results(now):
        print(f"skip: results outside 23:34–23:59 MSK (now {now:%H:%M})", flush=True)
        return "skip", mode
    if mode == "tick":
        concrete = tick_choice(now, st)
        if not concrete:
            print(f"skip: outside announce/results/live windows (now {now:%H:%M} MSK)", flush=True)
            return "skip", "tick"
        print(f"tick -> {concrete} ({now:%H:%M} MSK)", flush=True)
    if concrete == "announce" and not dry and day in (st.get("announced_dates") or []):
        print(f"skip: announce already posted for {day}", flush=True)
        return "skip", concrete
    if concrete == "results" and not dry and day in (st.get("results_dates") or []):
        print(f"skip: results already posted for {day}", flush=True)
        return "skip", concrete
    if concrete == "live":
        import live_auto
        try:
            info = live_auto.assess()
        except Exception as ex:
            print(f"skip: live schedule check failed ({ex}); next tick will retry", flush=True)
            return "skip", concrete
        print(info["reason"], flush=True)
        if not info["posts"]:
            return "skip", concrete
    return "run", concrete


def emit(action):
    print(f"preflight: action={action}", flush=True)
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"action={action}\n")


def call_gate():
    p = subprocess.run([sys.executable, "live.py", "--gate"], cwd=ROOT, capture_output=True, text=True, timeout=180)
    sys.stdout.write(p.stdout or "")
    sys.stderr.write(p.stderr or "")
    if p.returncode != 0:
        return "FAIL"
    line = next((ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()), "")
    word = line.split(":", 1)[0].strip().upper()
    return word if word in ("IDLE", "BUSY", "ACTIVE") else "FAIL"


def run_live(dry):
    kind = call_gate()
    if kind != "ACTIVE":
        print(f"live gate {kind}; not posting", flush=True)
        return 0 if kind in ("IDLE", "BUSY") else 1
    try:
        args = [sys.executable, "live_auto.py"] + (["--dry-run"] if dry else [])
        return subprocess.run(args, cwd=ROOT).returncode
    finally:
        subprocess.run([sys.executable, "live.py", "--release"], cwd=ROOT)


def execute(mode, dry):
    flag = ["--dry-run"] if dry else []
    if mode == "announce":
        return subprocess.run([sys.executable, "announce_auto.py", *flag], cwd=ROOT).returncode
    if mode == "results":
        return subprocess.run([sys.executable, "results_auto.py", *flag], cwd=ROOT).returncode
    if mode == "live":
        return run_live(dry)
    print(f"unknown mode {mode}", flush=True)
    return 1


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", nargs="?", choices=["announce", "results", "live", "tick"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--preflight", action="store_true", help="decide run/skip and write GITHUB_OUTPUT")
    a = ap.parse_args(argv)
    mode, source = resolve(a.mode)
    dry = want_dry(a.dry_run)
    print(f"mode={mode} source={source} dry_run={dry}", flush=True)
    action, concrete = plan(mode, source, dry)
    if a.preflight:
        emit(action)
        return 0
    if action == "skip":
        return 0
    return execute(concrete, dry)


if __name__ == "__main__":
    sys.exit(main())
