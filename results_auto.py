#!/usr/bin/env python3
"""Daily results recap, no AI (23:34 MSK). day_results card + caption via post.py (all guards).
   python3 results_auto.py [--dry-run] [--date YYYY-MM-DD] [--hours 36] [--force]
One post per Moscow day: every notable match played that day, plus every media/streamer
league match (BetBoom Streamers Battle and the rest of events.json notable_events).
Live stays top-tier (matches.py --live is not used here). Streamer map names from
cybersport.ru are omitted; only the map scores are shown.
None finished -> posts nothing."""
import argparse, datetime, html, json, os, sys
from datetime import timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_lib import *
from common import MSK, now_msk

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--date", help="Moscow day to recap (default: today)")
ap.add_argument("--hours", type=int, default=36, help="lookback when merging matches that finished this day but started earlier")
ap.add_argument("--force", action="store_true")
a = ap.parse_args()
lock = single_instance("results_auto")
day = datetime.date.fromisoformat(a.date) if a.date else now_msk().date()
st = state()
if str(day) in st.get("results_dates", []) and not (a.force or a.dry_run):
    log(f"results for {day} already posted"); sys.exit(0)

cfg = load_events_cfg()


def _ts(s):
    if not s:
        return None
    dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def on_day(m):
    """Played this Moscow day: scheduled then, or the series ended then."""
    if (m.get("date_msk") or "")[:10] == str(day):
        return True
    end = _ts(m.get("end_utc"))
    return bool(end) and end.astimezone(MSK).date() == day


def score_ok(m):
    if m.get("status") not in ("finished", "defwin"):
        return False
    sc = (m.get("score") or "").strip()
    try:
        x, y = sc.split(":")
        int(x); int(y)
    except ValueError:
        return False
    return True


def keep(m):
    """Streamer/media leagues are never dropped. Stake Pulse still follows demote_events."""
    if not score_ok(m) or not on_day(m):
        return False
    if m.get("tier") == "MEDIA" or m.get("media"):
        return True
    return not demoted(m, cfg)


def load_day():
    """Calendar day, not a rolling 24 h slice, so a morning series is still in the 23:34 post.
    --today carries that day's streamer rows; --results adds a series that started the evening
    before and ended after midnight."""
    today = run_json(["matches.py", "--today", "--date", str(day)])
    recent = run_json(["matches.py", "--results", "--hours", str(a.hours), "--date", str(day)])
    by = {}
    for src in (today, recent):
        for m in src.get("matches") or []:
            k = m.get("key")
            if not k:
                continue
            prev = by.get(k)
            if prev is None or len(m.get("maps") or []) >= len(prev.get("maps") or []):
                by[k] = m
    errors = (today.get("errors") or []) + (recent.get("errors") or [])
    return list(by.values()), errors


rows, errors = load_day()
ms = [m for m in rows if keep(m)]
if not ms:
    log(f"no finished matches to recap for {day} (errors: {errors}) -> nothing posted"); sys.exit(0)
ms.sort(key=lambda m: m.get("start_utc") or "")
n_media = sum(1 for m in ms if m.get("tier") == "MEDIA" or m.get("media"))
log(f"{day}: {len(ms)} matches, {n_media} streamer/media")


def oriented(m):
    """winner first: (w, l, 'w:l', [(map, 'w:l')])"""
    s1, s2 = (int(x) for x in m["score"].split(":"))
    flip = s2 > s1
    t1, t2 = team(m["team1"]), team(m["team2"])
    media = m.get("tier") == "MEDIA"
    maps = []
    for mp in m.get("maps") or []:
        sc = mp.get("score") or ""
        if ":" not in sc: continue
        a1, a2 = sc.split(":")
        maps.append((None if media else mp.get("map"), f"{a2}:{a1}" if flip else sc))
    return (t2, t1, f"{s2}:{s1}", maps) if flip else (t1, t2, f"{s1}:{s2}", maps)


groups = {}
for m in ms:
    groups.setdefault(event_name(m["event"]), []).append(m)
order = sorted(groups, key=lambda e: min({"S": 0, "A": 1, "B": 2}.get(x.get("tier"), 3) for x in groups[e]))


def build(with_maps):
    lines = [f"🏁 <b>Итоги дня, {date_ru(day)}</b>"]
    for ev in order:
        lines += ["", f"🏆 {html.escape(ev)}"]
        for m in groups[ev]:
            w, l, sc, maps = oriented(m)
            ln = f"{w} {sc} {l}"
            if with_maps and maps:
                ln += " (" + ", ".join(f"{n} {s}" if n else s for n, s in maps) + ")"
            lines.append(html.escape(ln))
    lines += ["", hashtags([m["event"] for ev in order for m in groups[ev]])]
    return "\n".join(lines)


text = build(True)
if len(text) > 950:
    text = build(False)
card = {"date": str(day), "groups": []}
for ev in order:
    g = {"event": ev, "matches": []}
    for m in groups[ev]:
        w, l, sc, maps = oriented(m)
        g["matches"].append({"team1": w, "team2": l, "score": sc, "maps": [{"map": n or f"Карта {i+1}", "score": s}
                             for i, (n, s) in enumerate(maps)] if all(n for n, _ in maps) else []})
    card["groups"].append(g)

os.makedirs("drafts", exist_ok=True); os.makedirs("cards/out", exist_ok=True)
sfx = "-dry" if a.dry_run else ""
tp, jp, cp = f"drafts/results-auto-{day}{sfx}.txt", f"drafts/results-auto-{day}{sfx}.json", f"cards/out/results-auto-{day}{sfx}.jpg"
open(tp, "w").write(text + "\n"); json.dump(card, open(jp, "w"), ensure_ascii=False, indent=1)
rc, out, err = run(["card.py", "day_results", "--json", jp, "-o", cp])
if rc != 0 or not os.path.isfile(cp):
    log(f"card failed ({err[-300:]}) -> text-only post"); cp = ""
log(f"text:\n{text}\ncard: {cp or '-'}")
ok = check_and_post(f"res-{day}", tp, cp, a.dry_run, ms[-1].get("url") or "https://bo3.gg/matches",
                    check_args=["--since-hours", str(max(a.hours, 30))])  # rematch-safe score check (17.10)
if ok and not a.dry_run:
    st = state()
    st["recapped_keys"] = (st.get("recapped_keys", []) + [m["key"] for m in ms])[-500:]
    st["results_dates"] = (st.get("results_dates", []) + [str(day)])[-60:]
    save_state(st)
log("DONE" if ok else "FAILED"); sys.exit(0 if ok else 1)
