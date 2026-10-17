#!/usr/bin/env python3
"""Mark items as seen/skipped (so fetch.py stops returning them), or undo.

Usage:
  python3 mark_seen.py ID [ID ...]            # mark as skipped
  python3 mark_seen.py --all [--except ID ..]  # mark every item in items.json (seed backlog)
  python3 mark_seen.py --unmark ID [ID ...]    # remove from state (will be fetched again)
  python3 mark_seen.py --list                  # show state
"""
import argparse, json
from common import ITEMS_FILE, load_json, load_state, save_state, mark

ap = argparse.ArgumentParser()
ap.add_argument("ids", nargs="*")
ap.add_argument("--all", action="store_true")
ap.add_argument("--except", dest="exc", nargs="*", default=[])
ap.add_argument("--unmark", action="store_true")
ap.add_argument("--status", default="skipped")
ap.add_argument("--list", action="store_true")
a = ap.parse_args()

st = load_state()
if a.list:
    for k, v in st["seen"].items():
        print(f"{v['status']:8} {v['marked_at']} {k} | {v['title'][:80]}")
    raise SystemExit
items = {i["id"]: i for i in load_json(ITEMS_FILE, {}).get("items", [])}
ids = [i for i in items if i not in a.exc] if a.all else a.ids
for i in ids:
    if a.unmark:
        print(("unmarked " if st["seen"].pop(i, None) else "not in state ") + i)
    else:
        mark(st, items.get(i, {"id": i}), a.status); print(f"{a.status}: {i}")
save_state(st)
