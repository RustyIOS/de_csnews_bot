#!/usr/bin/env python3
"""Morning match announce, no AI (cron 10:04 MSK). Text + announce card via post.py (dup/image/lock/gap guards).
   python3 announce_auto.py [--dry-run] [--date YYYY-MM-DD] [--force]
No notable matches -> posts nothing. Already announced today (auto_state.json) -> nothing unless --force."""
import argparse, datetime, html, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_lib import *
from common import now_msk

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--date"); ap.add_argument("--force", action="store_true")
a = ap.parse_args()
lock = single_instance("announce_auto")
day = datetime.date.fromisoformat(a.date) if a.date else now_msk().date()
st = state()
if str(day) in st["announced_dates"] and not (a.force or a.dry_run):
    log(f"already announced {day}"); sys.exit(0)

cfg = load_events_cfg()
data = run_json(["matches.py", "--today", "--date", str(day)])
ms = [m for m in data.get("matches", []) if m.get("status") not in ("finished",) and not demoted(m, cfg)]
if not ms:
    log(f"no notable matches on {day} (errors: {data.get('errors')}) -> nothing posted"); sys.exit(0)
ms.sort(key=lambda m: m.get("start_msk") or "")

def is_tbd(n):
    return (n or "").strip().upper() in ("", "TBD", "TBA")

groups = {}
for m in ms:
    groups.setdefault(event_name(m["event"]), []).append(m)
# bigger tiers first (S/A before media)
order = sorted(groups, key=lambda e: min({"S": 0, "A": 1, "B": 2}.get(x.get("tier"), 3) for x in groups[e]))
lines = [f"📅 <b>Матчи дня, {date_ru(day)}</b>"]
card = {"date": str(day), "matches": []}
for ev in order:
    lines += ["", f"🏆 {html.escape(ev)}"]
    tbd = [m for m in groups[ev] if is_tbd(m["team1"]) and is_tbd(m["team2"])]
    for m in [x for x in groups[ev] if x not in tbd]:
        t1, t2, st_ = team(m["team1"]), team(m["team2"]), stage_short(m.get("stage"))
        parts = [f"{m.get('start_msk')} {t1} vs {t2}"] + [x for x in (m.get("format"), st_) if x]
        lines.append(html.escape(", ".join(parts)))
        card["matches"].append({"key": m["key"], "start_msk": m.get("start_msk"), "team1": t1, "team2": t2,
                                "format": m.get("format"), "event": ev, "stage_short": st_,
                                "team1_logo": m.get("team1_logo"), "team2_logo": m.get("team2_logo"),
                                "event_logo": m.get("event_logo")})
    if tbd:
        lines.append(html.escape(", ".join(m.get("start_msk") or "?" for m in tbd)
                                 + (" — пары определятся позже" if len(tbd) > 1 else " — пара определится позже")))
lines += ["", hashtags([m["event"] for ev in order for m in groups[ev]])]
text = "\n".join(lines)

os.makedirs("drafts", exist_ok=True); os.makedirs("cards/out", exist_ok=True)
sfx = "-dry" if a.dry_run else ""
tp, jp, cp = f"drafts/announce-auto-{day}{sfx}.txt", f"drafts/announce-auto-{day}{sfx}.json", f"cards/out/announce-auto-{day}{sfx}.jpg"
open(tp, "w").write(text + "\n"); json.dump(card, open(jp, "w"), ensure_ascii=False, indent=1)
rc, out, err = run(["card.py", "announce", "--json", jp, "-o", cp])
if rc != 0 or not os.path.isfile(cp):
    log(f"card failed ({err[-300:]}) -> text-only post"); cp = ""
log(f"text:\n{text}\ncard: {cp or '-'}")
url = (ms[0].get("url") or "https://bo3.gg/matches")
ok = check_and_post(f"ann-{day}", tp, cp, a.dry_run, url)
if ok and not a.dry_run:
    st = state(); st["announced_dates"] = (st["announced_dates"] + [str(day)])[-60:]; save_state(st)
    run(["matches.py", "--mark", "announced"] + [m["key"] for m in ms])
log("DONE" if ok else "FAILED"); sys.exit(0 if ok else 1)
