#!/usr/bin/env python3
"""Read / mark the live event queue written by collector.py (live_queue.json). Never posts.

  python3 live_queue.py --pending [--brief] [--key bo3:ID]     # unposted events (status "new"), oldest first
  python3 live_queue.py --mark posted ID [ID ...]              # after a successful send: queue status=posted AND
                                                               #   event_id appended to state.json live_events[key].seen
  python3 live_queue.py --mark skipped ID [ID ...] [--note T]  # not worth posting / covered by another post (also -> seen)
  python3 live_queue.py --show ID | --all [--brief]
IDs are "<match key>|<event id>", e.g. "bo3:130754|map2:half" (quote them in the shell). Queue writes are flock-protected.
"""
import argparse, json, sys
from collector import QueueLock, QUEUE, load_queue, prune_queue
from common import load_state, save_state, save_json, now_msk


def brief(e):
    d, s = e.get("data") or {}, e.get("scores") or {}
    m = e["match"]
    flag = "OK " if e.get("confirmed") else "UNCONFIRMED "
    extra = ""
    t = e["type"]
    if t == "tg_moment":
        extra = f"[{','.join(d.get('kinds') or [])}] @{d.get('channel')} {d.get('posted_msk')}: {d.get('text', '')[:160]!r}"
    elif t == "map_finished":
        extra = f"{e.get('map')}: {d.get('winner')} {s.get('score')} {d.get('loser')}, серия {s.get('series')}" + \
                (f", OT x{d.get('overtimes')}" if d.get("overtime") else "")
    elif t == "half_time":
        extra = f"{e.get('map')}: {m['team1']} {s.get('score')} {m['team2']}, sides {d.get('sides')}"
    elif t == "match_finished":
        extra = f"{d.get('winner')} def. {d.get('loser')} {s.get('series')}"
    elif t in ("pistol", "ace"):
        extra = f"{e.get('map')} r{d.get('round')}: {d.get('winner') or d.get('player')}"
    elif t in ("map_started", "match_started"):
        extra = f"{e.get('map') or d.get('first_map') or ''}"
    c = f" CONFLICT: {e['conflict']}" if e.get("conflict") else ""
    return f"{e['id']}  {t}  {flag}{m['team1']} vs {m['team2']}  {extra}  src={','.join(e.get('sources') or [])}{c}  ({e.get('detected_at', '')[11:19]})"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pending", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--show")
    ap.add_argument("--mark", nargs="+", metavar=("STATUS", "ID"))
    ap.add_argument("--note")
    ap.add_argument("--key")
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--no-collapse", action="store_true", help="--pending: show stale events too (debug)")
    ap.add_argument("--queue", default=QUEUE)
    ap.add_argument("--no-state", action="store_true", help="--mark: don't touch state.json (tests)")
    a = ap.parse_args()
    if a.mark:
        status, ids = a.mark[0], a.mark[1:]
        if status not in ("posted", "skipped") or not ids:
            sys.exit("usage: --mark posted|skipped ID ...")
        with QueueLock():
            q = load_queue(a.queue)
            byid = {e["id"]: e for e in q}
            st = None if a.no_state else load_state()
            for i in ids:
                e = byid.get(i)
                if not e:
                    print("not in queue:", i); continue
                e["status"], e["posted_at"] = status, now_msk().isoformat(timespec="seconds")
                if a.note:
                    e["note"] = a.note
                if st is not None:
                    rs = st.setdefault("live_events", {}).setdefault(e["match"]["key"], {"seen": []})
                    if e["event_id"] not in rs.setdefault("seen", []):
                        rs["seen"].append(e["event_id"])
                    rs["updated"] = now_msk().isoformat(timespec="seconds")
                print(status, i)
            save_json(a.queue, prune_queue(q))
            if st is not None:
                save_state(st)
        return
    q = load_queue(a.queue)
    if a.show:
        print(json.dumps([e for e in q if e["id"] == a.show], ensure_ascii=False, indent=1)); return
    rows = [e for e in q if (a.all or e.get("status") == "new") and (not a.key or e["match"]["key"] == a.key)]
    if a.pending and not a.all and not a.no_collapse:
        # lesson 23: catch-up -> only the newest state per match; older pending events auto-skipped
        from live import collapse_stale
        rows, sup = collapse_stale(rows)
        if sup:
            with QueueLock():
                q2 = load_queue(a.queue); byid = {e["id"]: e for e in q2}
                st = None if a.no_state else load_state()
                for e in sup:
                    x = byid.get(e["id"])
                    if not x or x.get("status") != "new":
                        continue
                    x["status"], x["note"] = "skipped", "superseded (catch-up collapse)"
                    x["posted_at"] = now_msk().isoformat(timespec="seconds")
                    if st is not None:
                        rs = st.setdefault("live_events", {}).setdefault(x["match"]["key"], {"seen": []})
                        if x["event_id"] not in rs.setdefault("seen", []):
                            rs["seen"].append(x["event_id"])
                save_json(a.queue, prune_queue(q2))
                if st is not None:
                    save_state(st)
            print(f"# collapsed {len(sup)} superseded event(s): " + ", ".join(e["id"] for e in sup) +
                  " -> mention their scores inside the kept post; space posts >= 75 s (python3 live.py --wait-gap)")
    if a.brief:
        for e in rows:
            print(("" if e.get("status") == "new" else f"[{e['status']}] ") + brief(e))
        if not rows:
            print("(queue empty)")
    else:
        print(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
