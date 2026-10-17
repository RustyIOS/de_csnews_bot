#!/usr/bin/env python3
"""Media / streamer leagues (events.json "notable_events") via the public cybersport.ru JSON API. Never posts.

  python3 media_league.py --today [--date YYYY-MM-DD]   # rows in matches.py format (key "cs:<id>")
  python3 media_league.py --poll cs:ID                   # queue-style items: match_started / map_started (map 2+) / map_finished / match_finished

When events.json live_tiers="top", matches.py --live (collector/gate) does NOT pull these rows — live coverage
is top-tier only. Daily announce/results still call media_league / matches.py without --live.
Times on cybersport.ru are MSK. Map NAMES there were once swapped vs. their own article (12.10 Ct0m-SCADE):
scores per game are reliable, map names - double-check before posting (LESSONS.md).
"""
import argparse, json, os, sys, time, urllib.request
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "events.json")
CACHE = os.path.join(BASE, "media_cache.json")
API = "https://www.cybersport.ru/api/matches/"
MSK = timezone(timedelta(hours=3))


def load(p, d):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return d


def cfg():
    return load(CFG, {})


def fetch(mid):
    req = urllib.request.Request(API + str(mid), headers={"User-Agent": "Mozilla/5.0 (de_csnews bot)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def parse(d, ev):
    m = d["data"]; a = m["attributes"]; r = m["relationships"]
    inc = {(x["type"], x["id"]): x for x in d.get("included", [])}
    def side(k):
        mp = inc.get(("match_participant", (r[k]["data"] or {}).get("id")), {})
        t = ((mp.get("relationships") or {}).get("team") or {}).get("data")
        name = inc.get(("team", t["id"]), {}).get("attributes", {}).get("title") if t else None
        return name or "TBD", (mp.get("attributes") or {}).get("matchScore")
    (t1, s1), (t2, s2) = side("participant1"), side("participant2")
    maps = []
    for g in (r.get("games") or {}).get("data") or []:
        gx = inc.get((g["type"], g["id"]), {})
        ga = gx.get("attributes", {})
        mid_ = (((gx.get("relationships") or {}).get("map") or {}).get("data") or {}).get("id")
        mn = inc.get(("discipline_map", mid_), {}).get("attributes", {}).get("name")
        if not ga.get("participant1Score") and not ga.get("participant2Score") and a.get("isFinished"):
            continue   # unplayed decider listed as 0:0
        maps.append({"map": mn, "score": f"{ga.get('participant1Score')}:{ga.get('participant2Score')}",
                     "s1": ga.get("participant1Score"), "s2": ga.get("participant2Score")})
    bo = {"bestOf1": 1, "bestOf3": 3, "bestOf5": 5}.get(a.get("format"), 3)
    start = None
    if a.get("date") and a.get("time"):
        start = datetime.fromisoformat(f"{a['date']}T{a['time']}:00").replace(tzinfo=MSK)
    fin = bool(a.get("isFinished"))
    now = datetime.now(MSK)
    status = "finished" if fin else ("current" if (maps or (start and start <= now)) else "upcoming")
    row = {"key": f"cs:{m['id']}", "source": "cybersport.ru", "url": f"https://www.cybersport.ru/matches/cs2/{m['id']}",
           "status": status, "team1": t1, "team2": t2, "team1_rank": None, "team2_rank": None,
           "start_msk": a.get("time"), "date_msk": a.get("date"),
           "start_utc": start.astimezone(timezone.utc).isoformat() if start else None, "end_utc": None,
           "event": ev["name"], "stage": None, "format": f"BO{bo}", "tier": "MEDIA",
           "importance": ev.get("importance", "medium"), "media": True, "hashtags": ev.get("hashtags", [])}
    if maps or fin or s1 or s2:
        winner = None
        if fin and s1 is not None and s2 is not None and s1 != s2:
            winner = t1 if s1 > s2 else t2
        row.update({"score": f"{s1 or 0}:{s2 or 0}", "winner": winner, "maps": maps})
    return row, m["relationships"]["tournament"]["data"]["id"]


def ids_for(ev, c):
    lo, hi = ev.get("match_ids", [0, -1])
    known = set(c.get(str(ev["tournament_id"]), {}).get("extra", []))
    return list(range(lo, hi + 1)) + sorted(known)


def discover(ev, c):
    """Look up to 15 ids above the configured range for new (playoff) matches; at most once per 30 min."""
    key = str(ev["tournament_id"]); ent = c.setdefault(key, {"extra": [], "checked": 0})
    if time.time() - ent["checked"] < 1800:
        return
    ent["checked"] = time.time()
    top = max([ev["match_ids"][1]] + ent["extra"])
    def one(i):
        try:
            d = fetch(i); return i, d["data"]["relationships"]["tournament"]["data"]["id"]
        except Exception: return i, None
    with ThreadPoolExecutor(8) as ex:
        for i, t in ex.map(one, range(top + 1, top + 16)):
            if t == key and i not in ent["extra"]:
                ent["extra"].append(i)


def rows(day=None, only_day=True):
    c = load(CACHE, {}); out, errors = [], []
    day = day or datetime.now(MSK).date().isoformat()
    for ev in cfg().get("notable_events", []):
        if ev.get("source") != "cybersport.ru":
            continue
        if ev.get("start") and ev.get("end") and not (ev["start"] <= day <= ev["end"]):
            continue
        try:
            discover(ev, c)
        except Exception as e:
            errors.append(f"cybersport discover: {e}")
        def one(i):
            try:
                return parse(fetch(i), ev)
            except Exception as e:
                errors.append(f"cybersport {i}: {e}"); return None, None
        with ThreadPoolExecutor(8) as ex:
            for r, tid in ex.map(one, ids_for(ev, c)):
                if r and tid == str(ev["tournament_id"]) and (not only_day or r["date_msk"] == day):
                    out.append(r)
    try:
        with open(CACHE, "w") as f: json.dump(c, f)
    except Exception: pass
    out.sort(key=lambda r: (r["date_msk"] or "", r["start_msk"] or ""))
    return out, errors


def poll(key):
    """(items, status, row). Items mirror collector.to_queue_item shape."""
    mid = int(key.split(":")[1])
    ev = next((e for e in cfg().get("notable_events", []) if e.get("source") == "cybersport.ru"), {"name": "?"})
    d = fetch(mid)
    tid = d["data"]["relationships"]["tournament"]["data"]["id"]
    ev = next((e for e in cfg().get("notable_events", []) if str(e.get("tournament_id")) == tid), ev)
    r, _ = parse(d, ev)
    items = []
    def it(eid, t, mapn=None, scores=None, data=None):
        items.append({"id": f"{key}|{eid}", "event_id": eid, "type": t, "map": mapn,
                      "match": {"key": key, "team1": r["team1"], "team2": r["team2"], "event": r["event"]},
                      "confirmed": True, "conflict": None, "scores": scores or {}, "data": (data or {}) | {"media": True},
                      "sources": ["cybersport.ru"], "evidence": [{"source": "cybersport.ru", "url": r["url"]}]})
    if r["status"] != "upcoming":
        it("start", "match_started", data={"first_map": (r.get("maps") or [{}])[0].get("map")})
    w1 = w2 = 0
    current_start_emitted = False
    for n, mp in enumerate(r.get("maps") or [], 1):
        s1, s2 = mp["s1"] or 0, mp["s2"] or 0
        done = max(s1, s2) >= 13 and abs(s1 - s2) >= 2 or (max(s1, s2) > 13 and r["status"] == "finished")
        if not done and r["status"] != "finished":
            # first unfinished map = live (or listed decider); map 1 is covered by match_started
            if n >= 2 and not current_start_emitted:
                it(f"map{n}:start", "map_started", mp["map"], {"series": f"{w1}:{w2}"},
                   {"map_number": n, "map_name_unverified": True})
            # always mark the first unfinished map, including map 1, so later 0:0 maps
            # don't emit a bogus map2+:start while map 1 is still live
            current_start_emitted = True
            continue
        if s1 > s2: w1 += 1
        else: w2 += 1
        win, los = (r["team1"], r["team2"]) if s1 > s2 else (r["team2"], r["team1"])
        it(f"map{n}:end", "map_finished", mp["map"], {"score": f"{max(s1,s2)}:{min(s1,s2)}", "series": f"{w1}:{w2}"},
           {"winner": win, "loser": los, "map_name_unverified": True})
    if r["status"] == "finished" and r.get("winner"):
        los = r["team2"] if r["winner"] == r["team1"] else r["team1"]
        a, b = (int(x) for x in r["score"].split(":"))
        it("end", "match_finished", scores={"series": f"{max(a,b)}:{min(a,b)}"},
           data={"winner": r["winner"], "loser": los, "maps": r.get("maps")})
    return items, r["status"], r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--today", action="store_true"); ap.add_argument("--date")
    ap.add_argument("--poll")
    a = ap.parse_args()
    if a.poll:
        items, st, r = poll(a.poll)
        print(json.dumps({"status": st, "match": r, "items": items}, ensure_ascii=False, indent=2)); return
    rs, errs = rows(a.date)
    print(json.dumps({"ok": True, "source": "cybersport.ru", "matches": rs, "errors": errs}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
