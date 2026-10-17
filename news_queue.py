#!/usr/bin/env python3
"""Scheduled queue for regular NEWS posts: spaces them 10-15 min apart (random). Live posts, match
announcements/results, polls, daily/weekly recaps do NOT use this queue.

  python3 news_queue.py --add ID --text-file post.txt [--image X] [--url U --source S --title T] [--no-source] [--text]
      Proofread first (README checklist). --add re-runs check_post.py (FAIL -> refused) and post.py --dry-run, freezes
      text + item data (url/source/image/title, so a later fetch.py rewriting items.json doesn't matter) into news_queue/,
      and schedules it at max(now, last news post, last queued) + random 10-15 min. If that slot is already due
      (no news in the last 10 min and empty queue) it is sent immediately. Then a detached sender is ensured.
  python3 news_queue.py --send-due [--dry-run] [--chat ID]   # publish due items (also run by live.py each run)
  python3 news_queue.py --daemon        # detached sender loop (started automatically; exits when queue empty)
  python3 news_queue.py --list | --cancel ID
Dedupe: refuses/skips items already in state.json seen (posted/skipped) or already queued; status goes
pending -> sending -> posted|failed under an flock, so nothing is double-posted; a crash mid-send leaves 'sending'
which is NOT retried automatically (check the channel, then --retry ID or --cancel ID).
Env: NEWS_QUEUE_FILE (tests), TELEGRAM_BOT_TOKEN (used by post.py, never printed).
"""
import argparse, fcntl, json, os, random, shutil, subprocess, sys, time
from datetime import datetime, timedelta
from common import BASE, ITEMS_FILE, load_json, save_json, load_state, now_msk

QFILE = os.environ.get("NEWS_QUEUE_FILE", os.path.join(BASE, "news_queue.json"))
QDIR = os.path.join(os.path.dirname(QFILE), "news_queue")
LOCK = QFILE + ".lock"
PIDF = QFILE + ".pid"
LOG = os.path.join(BASE, "logs", "news_queue.log")
GAP = (10, 15)


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{now_msk().isoformat(timespec='seconds')} {msg}\n")


class Locked:
    def __enter__(self):
        self.f = open(LOCK, "w"); fcntl.flock(self.f, fcntl.LOCK_EX); return self
    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN); self.f.close()


def load_q():
    return load_json(QFILE, [])


def last_news_time(q):
    """Latest of: news marked 'posted' in state.json (post.py marks only news; announcements use --no-mark),
    queue items posted/pending (scheduled)."""
    ts = []
    for v in load_state()["seen"].values():
        if v.get("status") == "posted" and v.get("marked_at"):
            ts.append(datetime.fromisoformat(v["marked_at"]))
    for it in q:
        if it["status"] in ("pending", "sending"):
            ts.append(datetime.fromisoformat(it["send_at"]))
        elif it["status"] == "posted" and it.get("posted_at"):
            ts.append(datetime.fromisoformat(it["posted_at"]))
    return max(ts) if ts else None


def post_cmd(it, dry, chat=None):
    c = [sys.executable, os.path.join(BASE, "post.py"), it["id"], "--text-file", it["text_file"],
         "--url", it["url"], "--source", it["source"], "--image", it.get("image") or "", "--title", it.get("title") or ""]
    if it.get("no_source"): c.append("--no-source")
    if dry: c.append("--dry-run")
    if chat: c += ["--chat", chat]
    return c


def ensure_daemon():
    try:
        pid = int(open(PIDF).read()); os.kill(pid, 0); return
    except Exception:
        pass
    out = open(os.path.join(BASE, "logs", "news_queue.out"), "a")
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--daemon"], cwd=BASE, stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, start_new_session=True, env=os.environ.copy())
    print(f"sender started (pid {p.pid})")


def send_due(dry=False, chat=None):
    sent = 0
    while True:
        with Locked():
            q = load_q(); now = now_msk(); seen = load_state()["seen"]
            due = [i for i in q if i["status"] == "pending" and datetime.fromisoformat(i["send_at"]) <= now]
            if not due:
                return sent
            it = min(due, key=lambda i: i["send_at"])
            if it["id"] in seen:
                it["status"] = "skipped"; it["note"] = f"already {seen[it['id']].get('status')} in state.json"
                save_json(QFILE, q); log(f"skip dup {it['id']}"); continue
            if not dry:
                it["status"] = "sending"; save_json(QFILE, q)
        p = subprocess.run(post_cmd(it, dry, chat), capture_output=True, text=True, cwd=BASE)
        with Locked():
            q = load_q(); cur = next(i for i in q if i["id"] == it["id"])
            if dry:
                print(f"[dry-run] would send {it['id']} (due {it['send_at']}):\n{p.stdout[-600:]}{p.stderr[-300:]}")
                if p.returncode == 0: cur["status"] = "dry-posted"; cur["posted_at"] = now_msk().isoformat(timespec="seconds")
            elif p.returncode == 0:
                cur["status"] = "posted"; cur["posted_at"] = now_msk().isoformat(timespec="seconds")
                cur["result"] = p.stdout.strip()[-200:]; log(f"posted {it['id']} {cur['result']}"); sent += 1
            else:
                err = (p.stderr or p.stdout).strip()[-300:]
                cur["attempts"] = cur.get("attempts", 0) + 1; cur["last_error"] = err
                # post.py exits non-zero before or on a Telegram error -> message not sent, safe to retry
                cur["status"] = "failed" if cur["attempts"] >= 3 else "pending"
                if cur["status"] == "pending":
                    cur["send_at"] = (now_msk() + timedelta(minutes=2)).isoformat(timespec="seconds")
                log(f"error {it['id']} attempt {cur['attempts']}: {err}")
            save_json(QFILE, q)
            if dry and p.returncode != 0:
                print(f"[dry-run] post.py failed for {it['id']}: {p.stderr[-300:]}"); cur["status"] = "failed"; save_json(QFILE, q)


def add(a):
    os.makedirs(QDIR, exist_ok=True)
    items = {i["id"]: i for i in load_json(ITEMS_FILE, {}).get("items", [])}
    item = dict(items.get(a.id, {"id": a.id}))
    for k in ("url", "source", "image", "title"):
        if getattr(a, k) is not None: item[k] = getattr(a, k)
    if not item.get("url") or not item.get("source"):
        sys.exit(f"{a.id} not in items.json; pass --url/--source")
    if a.id in load_state()["seen"]:
        sys.exit(f"{a.id} already in state.json seen ({load_state()['seen'][a.id]['status']}) - not queued")
    ck = [sys.executable, os.path.join(BASE, "check_post.py"), a.text_file] + (["--text"] if a.text or not item.get("image") else [])
    c = subprocess.run(ck, capture_output=True, text=True, cwd=BASE)
    print(c.stdout.strip())
    if c.returncode != 0:
        sys.exit("check_post.py FAIL -> fix the draft; not queued")
    tf = os.path.join(QDIR, f"{a.id.replace(':', '_').replace('/', '_')}.txt")
    shutil.copyfile(a.text_file, tf)
    it = {"id": a.id, "text_file": tf, "url": item["url"], "source": item["source"], "image": item.get("image") or "",
          "title": item.get("title") or "", "no_source": a.no_source}
    d = subprocess.run(post_cmd(it, True), capture_output=True, text=True, cwd=BASE)
    if d.returncode != 0:
        os.unlink(tf); sys.exit(f"post.py --dry-run failed: {d.stderr.strip()}")
    with Locked():
        q = load_q()
        if any(i["id"] == a.id and i["status"] in ("pending", "sending", "posted") for i in q):
            sys.exit(f"{a.id} already queued/posted via queue")
        now = now_msk(); last = last_news_time(q)
        at = now if last is None else max(now, last + timedelta(minutes=random.uniform(*GAP)))
        it.update(status="pending", send_at=at.isoformat(timespec="seconds"), added_at=now.isoformat(timespec="seconds"))
        q = [i for i in q if i["id"] != a.id] + [it]
        save_json(QFILE, q)
    print(json.dumps({"queued": a.id, "send_at": it["send_at"], "immediate": at <= now}, ensure_ascii=False))
    log(f"queued {a.id} at {it['send_at']}")
    if a.dry_run:
        return
    if at <= now:
        send_due()
    ensure_daemon()


def daemon():
    with open(PIDF, "w") as f: f.write(str(os.getpid()))
    try:
        while True:
            send_due()
            q = load_q()
            pend = [i for i in q if i["status"] == "pending"]
            if not pend:
                return
            nxt = min(datetime.fromisoformat(i["send_at"]) for i in pend)
            time.sleep(max(5, min(60, (nxt - now_msk()).total_seconds())))
    finally:
        try:
            if open(PIDF).read().strip() == str(os.getpid()): os.unlink(PIDF)
        except Exception: pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--add"); ap.add_argument("--text-file"); ap.add_argument("--url"); ap.add_argument("--source")
    ap.add_argument("--image"); ap.add_argument("--title"); ap.add_argument("--no-source", action="store_true")
    ap.add_argument("--text", action="store_true"); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--chat"); ap.add_argument("--send-due", action="store_true"); ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--list", action="store_true"); ap.add_argument("--cancel"); ap.add_argument("--retry")
    ap.add_argument("--ensure", action="store_true", help="start sender if items pending")
    a = ap.parse_args()
    if a.add:
        if not a.text_file: sys.exit("--text-file required")
        a.id = a.add; return add(a)
    if a.send_due:
        n = send_due(a.dry_run, a.chat); print(f"sent {n}")
        if any(i["status"] == "pending" for i in load_q()) and not a.dry_run: ensure_daemon()
        return
    if a.daemon: return daemon()
    if a.cancel or a.retry:
        with Locked():
            q = load_q()
            for i in q:
                if i["id"] == (a.cancel or a.retry):
                    i["status"] = "cancelled" if a.cancel else "pending"
                    if a.retry: i["send_at"] = now_msk().isoformat(timespec="seconds")
            save_json(QFILE, q)
        return
    if a.ensure:
        if any(i["status"] == "pending" for i in load_q()): ensure_daemon()
        return
    for i in load_q():
        print(i["status"].ljust(10), i["send_at"], i["id"], (i.get("title") or "")[:60], i.get("last_error", ""))


if __name__ == "__main__":
    main()
