#!/usr/bin/env python3
"""Live collector: pre-detects live events for notable CS2 matches every ~60 s and appends them to live_queue.json.
The live routine (every 5 min) reads the queue, verifies, proofreads, posts and marks posted. NOTHING is posted here,
and state.json is never written (read-only dedupe against live_events[*].seen / match_results).

Runs ONLY while needed: a notable match (matches.py --today) is live, or starts within 15 min. When nothing is
live/upcoming it exits by itself after a grace period (default 10 min after the last match finished). Never runs
outside 10:00-23:59 MSK.

  python3 collector.py --check            # exit 0 if a notable match is live / starts within 15 min (prints JSON)
  python3 collector.py --ensure [opts]    # start detached if needed & not running (restart if heartbeat > 3 min old)
  python3 collector.py --status | --stop
  python3 collector.py [opts]             # foreground loop (normally started by --ensure)
  test opts: --sim-schedule FILE (match list instead of matches.py), --queue FILE, --cstate FILE, --ignore-posted,
             --grace-min N, --tick-sec N, --once, --ignore-window
Sources per tick: bo3 REST match/games (+ websocket buffer live_buf/<id>.jsonl via a spawned `live.py --listen`
when bo3 has live coverage, + REST last_snapshot), Liquipedia wikitext (lp_live, action=query only, >= 2.1 s apart,
>= 55 s per page, cached, exponential backoff on 429/errors), Telegram previews of tg_channels.json channels with
live:true (moments/score flashes) or reliable:true (score confirmation). No X (only reachable from the routine).
"""
import argparse, fcntl, json, logging, os, re, signal, subprocess, sys, time, traceback, urllib.parse
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from common import UA, MSK, BASE, load_json, save_json, load_state, now_msk
import live, lp_live, tg_scan

QUEUE = os.path.join(BASE, "live_queue.json")
QLOCK = os.path.join(BASE, "live_queue.lock")
HB = os.path.join(BASE, "collector_heartbeat.json")
CSTATE = os.path.join(BASE, "collector_state.json")
PIDF = os.path.join(BASE, "collector.pid")
LOCKF = os.path.join(BASE, "collector.lock")
LOGDIR = os.path.join(BASE, "logs")
LOG = os.path.join(LOGDIR, "collector.log")
OUT = os.path.join(LOGDIR, "collector.out")
SOON_MIN = 15          # start heavy polling this many minutes before a scheduled start
STALE_SEC = 180        # heartbeat older than this -> restart
SCHED_EVERY = 300      # refresh schedule every 5 min
QUEUE_KEEP_DAYS = 3
WIN = (10, 0), (23, 59)
log = logging.getLogger("collector")


# ---------------- queue (shared with live_queue.py; flock-protected) ----------------
class QueueLock:
    def __enter__(self):
        self.f = open(QLOCK, "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def load_queue(path):
    q = load_json(path, [])
    return q if isinstance(q, list) else []


def prune_queue(q):
    cut = (now_msk() - timedelta(days=QUEUE_KEEP_DAYS)).isoformat()
    return [e for e in q if e.get("status") == "new" or (e.get("detected_at") or "") >= cut]


# ---------------- Liquipedia: polite wrapper replacing lp_live.lp_get ----------------
class LPBackoff(Exception):
    pass


LP = {"last": 0.0, "last_parse": 0.0, "cache": {}, "fail": 0, "until": 0.0}


def lp_get(params):
    now = time.time()
    if now < LP["until"]:
        raise LPBackoff(f"Liquipedia backoff for {int(LP['until'] - now)} s")
    key = json.dumps(params, sort_keys=True)
    ttl = 55 if params.get("prop") == "revisions" else 6 * 3600
    c = LP["cache"].get(key)
    if c and now - c[0] < ttl:
        return c[1]
    if params.get("action") == "parse":                       # not used, but keep the ToS limit if ever added
        w = 30.5 - (now - LP["last_parse"])
        if w > 0:
            time.sleep(w)
        LP["last_parse"] = time.time()
    w = 2.1 - (time.time() - LP["last"])
    if w > 0:
        time.sleep(w)
    LP["last"] = time.time()
    url = lp_live.LP_API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    p = subprocess.run(["curl", "-sL", "--compressed", "--max-time", "20", "-A", UA, "-w", "\n%{http_code}", url],
                       capture_output=True)
    body, _, code = p.stdout.rpartition(b"\n")
    code = int(code) if code.strip().isdigit() else 0
    if code != 200:
        LP["fail"] += 1
        back = min(60 * 2 ** (LP["fail"] - 1), 1800) if code != 429 else min(300 * 2 ** (LP["fail"] - 1), 3600)
        LP["until"] = time.time() + back
        raise RuntimeError(f"Liquipedia HTTP {code}; backing off {back} s")
    LP["fail"] = 0
    d = json.loads(body)
    LP["cache"][key] = (time.time(), d)
    if len(LP["cache"]) > 50:
        for k in sorted(LP["cache"], key=lambda k: LP["cache"][k][0])[:25]:
            LP["cache"].pop(k, None)
    return d


lp_live.lp_get = lp_get        # live.lp_events -> lp_live.live_maps -> lp_get (ours)


# ---------------- schedule ----------------
def fetch_schedule(sim=None):
    if sim:
        d = load_json(sim, {})
        return d.get("matches", d) if isinstance(d, dict) else d
    p = subprocess.run([sys.executable, os.path.join(BASE, "matches.py"), "--today", "--live"], capture_output=True,
                       text=True, timeout=120, cwd=BASE)
    d = json.loads(p.stdout)
    if not d.get("ok", True) and not d.get("matches"):
        raise RuntimeError(f"matches.py failed: {d.get('errors')}")
    return d.get("matches") or []


def prune_excluded(cs, sched):
    """Drop matches no longer live-eligible (live_tiers=top dropped media / demote_events)."""
    allowed = {m.get("key") for m in sched}
    dropped = []
    for k in list(cs.get("matches", {})):
        if k not in allowed and k not in cs.get("done", []):
            cs["matches"].pop(k, None)
            dropped.append(k)
    return dropped


def needed_keys(matches, done=(), now=None):
    """Notable matches that are live, or upcoming and starting within SOON_MIN (or overdue < 6 h, bo3 lags)."""
    now = now or datetime.now(timezone.utc)
    out = []
    for m in matches:
        k = m.get("key") or ""
        if not (k.startswith("bo3:") or k.startswith("cs:")) or k in done:
            continue
        st = m.get("status")
        start = datetime.fromisoformat(m["start_utc"]) if m.get("start_utc") else None
        if st == "current":
            out.append(k)
        elif st == "upcoming" and start and -6 * 3600 < (start - now).total_seconds() <= SOON_MIN * 60:
            out.append(k)
    return out


def in_window(t=None):
    t = t or now_msk()
    return WIN[0] <= (t.hour, t.minute) <= WIN[1]


# ---------------- Telegram ----------------
MOMENT = [("ace", re.compile(r"\bэйс|\bace\b|\b5к\b|\b5k\b|пент[аы]", re.I)),
          ("quad", re.compile(r"квадр|\b4к\b|\b4k\b|\bquad", re.I)),
          ("clutch", re.compile(r"клатч|clutch|\b1\s?(?:в|vs|на|v)\s?[2-5]\b", re.I)),
          ("outnumbered", re.compile(r"\b[1-3]\s?(?:в|vs|на|против|v)\s?[3-5]\b|в меньшинстве", re.I)),
          ("overtime", re.compile(r"овертайм|\bOT\b|овер\b", re.I)),
          ("pistol", re.compile(r"пистол", re.I)),
          ("comeback", re.compile(r"камб[еэ]к|comeback|отыгр", re.I)),
          ("streak", re.compile(r"раундов подряд|подряд|серия из", re.I)),
          ("map_point", re.compile(r"мап[- ]?поинт|map point|матч[- ]?поинт", re.I)),
          ("half_time", re.compile(r"перерыв|половин|half", re.I)),
          ("map_result", re.compile(r"забира(ют|ет)|выигрыва(ют|ет)|побе[дж]|wins?\b|takes?\b", re.I))]


def tg_posts(chans, since):
    rows = []
    for c in chans:
        try:
            posts = tg_scan.fetch(c["channel"])
        except Exception as ex:
            log.warning("tg %s: %r", c["channel"], ex)
            continue
        for p in posts:
            if p["time"] >= since:
                p["scores"] = [f"{x}:{y}" for x, y in tg_scan.SCORE.findall(p["text"]) if int(x) <= 30 and int(y) <= 30]
                p["reliable"], p["live"] = bool(c.get("reliable")), bool(c.get("live"))
                rows.append(p)
        time.sleep(0.5)
    return rows


def team_hit(text, t1, t2):
    low = text.lower()
    return any(a in low for t in (t1, t2) for a in tg_scan.aliases(t))


# ---------------- per-match detection ----------------
def merge(events):
    """Combine same-id events from different sources; detect score conflicts."""
    out = {}
    for e in events:
        i = e["id"]
        if i not in out:
            e = dict(e)
            e["_sources"] = {e.get("source"): e.get("score")}
            out[i] = e
            continue
        o = out[i]
        o["_sources"][e.get("source")] = e.get("score")
    return list(out.values())


def confirm_with_tg(score, posts):
    if not score:
        return [], False
    a, b = score.split(":")
    want = {f"{a}:{b}", f"{b}:{a}"}
    hits = [p for p in posts if want & set(p["scores"])]
    chs = {p["channel"] for p in hits}
    return hits, any(p["reliable"] for p in hits) or len(chs) >= 2


def to_queue_item(key, e, mt, tg_rel):
    t = e["type"]
    srcs = {k: v for k, v in e.pop("_sources", {e.get("source"): e.get("score")}).items() if k}
    scores = {k: v for k, v in srcs.items() if v}
    conflict = None
    if len(set(scores.values())) > 1:
        conflict = "sources disagree: " + ", ".join(f"{k} {v}" for k, v in scores.items())
    evidence = [{"source": s, "url": mt["url"] if s.startswith("bo3") else (mt.get("lp_url") if s == "Liquipedia" else None)}
                for s in srcs]
    confirmed = True
    if t in ("half_time", "map_finished"):
        if conflict:
            confirmed = False
        elif "Liquipedia" in srcs:
            confirmed = True                                  # LP = primary score source (README)
        else:
            hits, ok = confirm_with_tg(e.get("score"), tg_rel)
            evidence += [{"source": "Telegram @" + p["channel"], "url": p["url"]} for p in hits[:4]]
            confirmed = ok or len(scores) >= 2
    elif t == "match_finished":
        confirmed = bool(e.get("maps")) and all(mp.get("score") for mp in e["maps"])
    item = {"id": f"{key}|{e['id']}", "event_id": e["id"], "type": t,
            "match": {"key": key, "team1": e.get("team1"), "team2": e.get("team2"), "event": e.get("event")},
            "map": e.get("map"), "map_number": e.get("map_number"),
            "scores": {k: e[k] for k in ("score", "series") if e.get(k)} | ({"by_source": scores} if len(scores) > 1 else {}),
            "data": {k: v for k, v in e.items() if k not in ("id", "type", "key", "team1", "team2", "event")},
            "sources": sorted(srcs), "evidence": [x for x in evidence if x.get("url") or x["source"]],
            "confirmed": confirmed, "conflict": conflict}
    return item


def tg_moment_items(key, mt, posts):
    out, texts = [], []
    for p in posts:
        norm = re.sub(r"\W+", " ", p["text"].lower()).strip()
        if any(tg_scan_similar(norm, t) for t in texts):
            continue                                          # same flash posted twice / reposted
        texts.append(norm)
        if not p["live"] or not team_hit(p["text"], mt["team1"], mt["team2"]):
            continue
        kinds = [k for k, rx in MOMENT if rx.search(p["text"])]
        if not kinds and not p["scores"]:
            continue
        ch_post = p["url"].split("t.me/")[-1]
        out.append({"id": f"{key}|tg:{ch_post}", "event_id": f"tg:{ch_post}", "type": "tg_moment",
                    "match": {"key": key, "team1": mt["team1"], "team2": mt["team2"], "event": mt.get("event")},
                    "map": None, "map_number": None, "scores": {"mentioned": p["scores"]} if p["scores"] else {},
                    "data": {"kinds": kinds, "channel": p["channel"], "text": p["text"][:600],
                             "posted_msk": p["time"].astimezone(MSK).strftime("%H:%M")},
                    "sources": ["Telegram @" + p["channel"]], "evidence": [{"source": "Telegram @" + p["channel"], "url": p["url"]}],
                    "confirmed": False, "conflict": None})
    return out


def tg_scan_similar(a, b):
    import difflib
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.9


def ensure_listener(mid, minutes=300):
    pat = f"live.py bo3:{mid} --listen"
    if subprocess.run(["pgrep", "-f", pat], capture_output=True).returncode == 0:
        return False
    with open(os.path.join(LOGDIR, f"listen_{mid}.log"), "a") as f:
        subprocess.Popen([sys.executable, os.path.join(BASE, "live.py"), f"bo3:{mid}", "--listen", "--minutes", str(minutes)],
                         cwd=BASE, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    log.info("started bo3 websocket listener for %s", mid)
    return True


def stop_listener(mid):
    subprocess.run(["pkill", "-f", f"live.py bo3:{mid} --listen"], capture_output=True)


def poll_match(key, cs, tgrows, args):
    """-> (queue items, match status)"""
    mid = int(key.split(":")[1])
    m = live.fetch_match(mid)
    ctx = live.Ctx(m)
    rs = cs["matches"].setdefault(key, {})
    status = m.get("status")
    evs = live.rest_events(ctx)
    if status == "current" and m.get("parsed_status") not in (None, "waiting"):
        ensure_listener(mid)
    msgs = live.read_buffer(mid, rs)
    if status == "current":
        snap = live.snapshot(mid)
        if snap:
            msgs.append(snap)
    if msgs:
        evs += live.feed_events(ctx, msgs, rs)
    lp_url = None
    final_pass = status in ("finished", "defwin") and not rs.get("lp_final_done")
    if (status == "current" or final_pass) and not args.no_lp:
        try:
            lpe, lpi = live.lp_events(ctx, rs)
            evs += lpe
            if lpi:
                rs["live_score"], rs["lp_rev"] = lpi["live"], lpi["rev_ts"]
                lp_url = "https://liquipedia.net/counterstrike/" + urllib.parse.quote(lpi["page"].replace(" ", "_"))
                rs["lp_url"] = lp_url
            if final_pass:
                rs["lp_final_done"] = True
        except LPBackoff as ex:
            log.info("%s", ex)
        except Exception as ex:
            log.warning("Liquipedia error for %s: %r", key, ex)
            cs["last_error"] = f"Liquipedia: {ex!r}"[:300]
    mt = {"url": f"https://bo3.gg/matches/{m.get('slug')}", "lp_url": rs.get("lp_url"),
          "team1": ctx.t1, "team2": ctx.t2, "event": (m.get("tournament") or {}).get("name")}
    start = datetime.fromisoformat(m["start_date"].replace("Z", "+00:00")) - timedelta(minutes=10)
    end = datetime.fromisoformat(m["end_date"].replace("Z", "+00:00")) + timedelta(minutes=20) if m.get("end_date") else None
    posts = [p for p in tgrows if p["time"] >= start and (not end or p["time"] <= end)]
    rel = [p for p in posts if team_hit(p["text"], ctx.t1, ctx.t2) or p["reliable"]]
    items = [to_queue_item(key, e, mt, rel) for e in merge(evs)]
    bad = [i["map"] or i["event_id"] for i in items if i["type"] == "map_finished" and not i["confirmed"]]
    for i in items:
        if i["type"] == "match_finished" and bad:
            i["confirmed"], i["conflict"] = False, "map score(s) unconfirmed: " + ", ".join(map(str, bad))
    mstart = start + timedelta(minutes=10)
    items += tg_moment_items(key, mt, [p for p in posts if p["time"] >= mstart])
    rs["status"], rs["updated"] = status, now_msk().isoformat(timespec="seconds")
    return items, status, m


def write_items(items, args):
    st = load_state()
    le = st.get("live_events", {})
    posted_results = st.get("match_results", {})
    added, updated = [], []
    with QueueLock():
        q = load_queue(args.queue)
        byid = {e["id"]: e for e in q}
        for it in items:
            key, eid = it["match"]["key"], it["event_id"]
            if not args.ignore_posted:
                if eid in set(le.get(key, {}).get("seen", [])):
                    continue
                if it["type"] in ("match_finished", "tg_moment") and key in posted_results:
                    continue
            old = byid.get(it["id"])
            if old:
                if old.get("status") != "new":
                    continue
                ch = False
                for f in ("confirmed", "conflict", "scores", "sources"):
                    if it[f] != old.get(f) and not (f == "confirmed" and old.get("confirmed") and not it["confirmed"]):
                        old[f] = it[f]; ch = True
                urls = {x.get("url") for x in old.get("evidence", [])}
                for x in it["evidence"]:
                    if x.get("url") not in urls:
                        old.setdefault("evidence", []).append(x); ch = True
                if ch:
                    old["updated_at"] = now_msk().isoformat(timespec="seconds")
                    updated.append(old["id"])
                continue
            it.update({"detected_at": now_msk().isoformat(timespec="seconds"), "updated_at": None,
                       "status": "new", "posted_at": None})
            q.append(it)
            byid[it["id"]] = it
            added.append(it["id"])
        if added or updated:
            save_json(args.queue, prune_queue(q))
        pending = sum(1 for e in q if e.get("status") == "new")
    return added, updated, pending


# ---------------- loop ----------------
def heartbeat(**kw):
    hb = load_json(HB, {})
    hb.update(kw, pid=os.getpid(), last_tick_msk=now_msk().isoformat(timespec="seconds"), last_tick_ts=time.time())
    save_json(HB, hb)


def run(args):
    os.makedirs(LOGDIR, exist_ok=True)
    lockf = open(LOCKF, "a")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("collector already running", file=sys.stderr)
        return 3
    with open(PIDF, "w") as f:
        f.write(str(os.getpid()))
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(flag=True))
    log.info("collector start pid=%s args=%s", os.getpid(), vars(args))
    save_json(HB, {"started_msk": now_msk().isoformat(timespec="seconds"), "ticks": 0, "status": "starting",
                   "last_error": None, "pid": os.getpid(), "last_tick_ts": time.time(),
                   "last_tick_msk": now_msk().isoformat(timespec="seconds")})
    cs = load_json(args.cstate, {})
    if cs.get("day") != now_msk().date().isoformat():
        cs = {"day": now_msk().date().isoformat()}
    cs.setdefault("matches", {}); cs.setdefault("done", [])
    chans = [c for c in load_json(os.path.join(BASE, "tg_channels.json"), []) if c.get("live") or c.get("reliable")]
    sched, sched_at, last_need, ticks, last_err = [], 0.0, time.time(), 0, None
    reason = "?"
    try:
        while not stop["flag"]:
            t0, keys = time.time(), []
            if not args.ignore_window and not in_window():
                reason = "outside 10:00-23:59 MSK"; break
            try:
                if time.time() - sched_at >= SCHED_EVERY or not sched_at:
                    sched, sched_at = fetch_schedule(args.sim_schedule), time.time()
                    dropped = prune_excluded(cs, sched)
                    if dropped:
                        log.info("live_tiers drop (no longer live-eligible): %s", dropped)
                        save_json(args.cstate, cs)
                keys = needed_keys(sched, set(cs["done"]))
                live_keys = [k for k in keys if any(m.get("key") == k and (m.get("status") == "current" or
                             datetime.fromisoformat(m["start_utc"]) <= datetime.now(timezone.utc)) for m in sched)]
                allowed = {m.get("key") for m in sched}
                # poll matches that are (or should be) running; upcoming-soon ones: just wait for them
                # never keep polling keys that --live schedule excluded (media / Stake Pulse when live_tiers=top)
                todo = list(dict.fromkeys(live_keys + [k for k in cs["matches"] if k not in cs["done"] and k in allowed]))
                added, updated, pend, polled = [], [], None, []
                if todo:
                    since = datetime.now(timezone.utc) - timedelta(hours=8)
                    tgrows = tg_posts(chans, since)
                    for k in todo:
                        try:
                            if k.startswith("cs:"):   # media/streamer league via cybersport.ru (events.json)
                                import media_league
                                items, status, m = media_league.poll(k)
                                cs["matches"].setdefault(k, {})
                            else:
                                items, status, m = poll_match(k, cs, tgrows, args)
                            polled.append(f"{k}={status}")
                            a, u, pend = write_items(items, args)
                            added += a; updated += u
                            if status in ("finished", "defwin", "canceled", "cancelled"):
                                cs["done"].append(k)
                                if k.startswith("bo3:"): stop_listener(int(k.split(":")[1]))
                                log.info("%s finished -> done", k)
                        except Exception as ex:
                            last_err = f"{k}: {ex!r}"[:300]
                            log.warning("poll %s failed: %r", k, ex)
                if keys or todo:
                    last_need = time.time()
                ticks += 1
                for i in added:
                    log.info("queued %s", i)
                for i in updated:
                    log.info("updated %s", i)
                st = "polling" if todo else ("waiting_for_start" if keys else "grace")
                log.info("tick %d: %s needed=%s polled=%s +%d ~%d", ticks, st, keys, polled, len(added), len(updated))
                heartbeat(status=st, ticks=ticks, needed=keys, polled=polled, last_added=added, queue_pending=pend,
                          last_error=last_err or cs.get("last_error"), lp_backoff_until=LP["until"] or None)
                save_json(args.cstate, cs)
            except Exception as ex:
                last_err = f"{ex!r}"[:300]
                log.error("tick error: %s", traceback.format_exc()[-1500:])
                heartbeat(status="error", ticks=ticks, last_error=last_err, last_error_msk=now_msk().isoformat(timespec="seconds"))
            if not keys and not [k for k in cs["matches"] if k not in cs["done"]] \
                    and time.time() - last_need >= args.grace_min * 60:
                reason = f"no notable match live/starting within {SOON_MIN} min (grace {args.grace_min} min over)"
                break
            if args.once:
                reason = "--once"; break
            while not stop["flag"] and time.time() - t0 < args.tick_sec:
                time.sleep(1)
        else:
            reason = "SIGTERM"
    finally:
        for k in cs.get("matches", {}):
            if k.startswith("bo3:"):
                stop_listener(int(k.split(":")[1]))
        save_json(args.cstate, cs)
        heartbeat(status="stopped", stop_reason=reason)
        log.info("collector exit: %s", reason)
        try:
            os.remove(PIDF)
        except FileNotFoundError:
            pass
    return 0


def running_pid():
    try:
        pid = int(open(PIDF).read().strip())
        os.kill(pid, 0)
        if "collector.py" in open(f"/proc/{pid}/cmdline").read():
            return pid
    except Exception:
        return None
    return None


def check(args):
    if not args.ignore_window and not in_window():
        return {"needed": [], "reason": "outside 10:00-23:59 MSK"}
    sched = fetch_schedule(args.sim_schedule)
    keys = needed_keys(sched)
    # A match that flips to finished between ticks must keep the gate ACTIVE, otherwise
    # the 10-minute runner goes idle and the final score is never posted.
    try:
        from live_auto import pending_result_keys
        for k in pending_result_keys(sched):
            if k not in keys:
                keys.append(k)
    except Exception as ex:
        print(f"# pending results skipped: {ex!r}", file=sys.stderr)
    return {"needed": keys, "reason": "notable match live, starting soon, or result not yet posted" if keys else
            f"no notable match live or starting within {SOON_MIN} min"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--ensure", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--sim-schedule")
    ap.add_argument("--queue", default=QUEUE)
    ap.add_argument("--cstate", default=CSTATE)
    ap.add_argument("--ignore-posted", action="store_true", help="test: don't dedupe against state.json")
    ap.add_argument("--ignore-window", action="store_true")
    ap.add_argument("--no-lp", action="store_true")
    ap.add_argument("--grace-min", type=float, default=10)
    ap.add_argument("--tick-sec", type=float, default=60)
    a = ap.parse_args()
    a.queue, a.cstate = os.path.abspath(a.queue), os.path.abspath(a.cstate)
    if a.sim_schedule:
        a.sim_schedule = os.path.abspath(a.sim_schedule)
    os.makedirs(LOGDIR, exist_ok=True)
    h = RotatingFileHandler(LOG, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(h); log.setLevel(logging.INFO)
    hb = load_json(HB, {})
    pid = running_pid()
    if a.status:
        age = time.time() - hb.get("last_tick_ts", 0)
        print(json.dumps({"running": bool(pid), "pid": pid, "heartbeat_age_s": round(age), **{k: v for k, v in hb.items()
                          if k != "last_tick_ts"}}, ensure_ascii=False, indent=1))
        return
    if a.stop:
        if pid:
            os.kill(pid, signal.SIGTERM)
        print("stopped" if pid else "not running")
        return
    if a.check:
        r = check(a)
        print(json.dumps(r, ensure_ascii=False))
        sys.exit(0 if r["needed"] else 1)
    if a.ensure:
        if pid:
            age = time.time() - hb.get("last_tick_ts", 0)
            if age <= STALE_SEC or hb.get("status") == "starting" and age < 600:
                print(f"running pid={pid} heartbeat {round(age)} s ago, status={hb.get('status')}")
                return
            log.warning("heartbeat stale (%d s) -> restarting pid %s", age, pid)
            os.kill(pid, signal.SIGTERM)
            for _ in range(15):
                if not running_pid():
                    break
                time.sleep(1)
            if running_pid():
                os.kill(pid, signal.SIGKILL); time.sleep(1)
        try:
            r = check(a)
        except Exception as ex:
            print(f"schedule check failed ({ex!r}); not started"); return
        if not r["needed"]:
            print(f"not started: {r['reason']}")
            return
        cmd = [sys.executable, os.path.abspath(__file__)] + [x for x in sys.argv[1:] if x != "--ensure"]
        with open(OUT, "a") as f:
            p = subprocess.Popen(cmd, cwd=BASE, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        print(f"started pid={p.pid} for {r['needed']}")
        return
    sys.exit(run(a))


if __name__ == "__main__":
    main()
