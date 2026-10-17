#!/usr/bin/env python3
"""Daily results recap, no AI (cron 23:34 MSK). day_results card + caption via post.py (all guards).
   python3 results_auto.py [--dry-run] [--hours 24] [--force]
Takes notable matches finished in the last N hours that were not in a previous recap (auto_state.json).
None -> posts nothing. Streamer-league map names (cybersport.ru) are omitted, only map scores are shown."""
import argparse, html, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_lib import *
from common import now_msk

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--hours", type=int, default=24)
ap.add_argument("--force", action="store_true")
a = ap.parse_args()
lock = single_instance("results_auto")
day = now_msk().date()
st = state()
if str(day) in st.get("results_dates", []) and not (a.force or a.dry_run):
    log(f"results for {day} already posted"); sys.exit(0)

cfg = load_events_cfg()
data = run_json(["matches.py", "--results", "--hours", str(a.hours)])
done = set(st.get("recapped_keys", []))
ms = [m for m in data.get("matches", []) if m.get("status") == "finished" and m.get("score")
      and m["key"] not in done and not demoted(m, cfg)]
if not ms:
    log(f"no finished notable matches to recap (errors: {data.get('errors')}) -> nothing posted"); sys.exit(0)
ms.sort(key=lambda m: m.get("start_utc") or "")


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
                    check_args=["--since-hours", str(a.hours + 6)])  # rematch-safe score check (17.10)
if ok and not a.dry_run:
    st = state()
    st["recapped_keys"] = (st.get("recapped_keys", []) + [m["key"] for m in ms])[-500:]
    st["results_dates"] = (st.get("results_dates", []) + [str(day)])[-60:]
    save_state(st)
log("DONE" if ok else "FAILED"); sys.exit(0 if ok else 1)
