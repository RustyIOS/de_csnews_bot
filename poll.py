#!/usr/bin/env python3
"""Send an anonymous regular poll (sendPoll) to the channel, and record it in state.json["polls"].

Usage:
  python3 poll.py --question "Кто выиграет гранд-финал EPL S24?" --option Vitality --option MOUZ \
                  [--key bo3:130754] [--dry-run] [--chat @test_or_id] [--force]
  python3 poll.py --stop KEY [--winner Vitality]     # close the poll (stopPoll), store + print votes and % as JSON
  python3 poll.py --results KEY [--winner Vitality]  # stored results; for a still-open poll, a live peek via
                                                     # getUpdates (read-only, never confirms/consumes updates)
  python3 poll.py --list            # show recorded polls
--winner: the option that turned out correct -> adds "guessed_percent" (share of voters who picked it).
Results JSON: {key, question, closed, total_voters, options:[{text, votes, percent}], results:{text: votes},
               winner?, guessed_percent?}  - can be fed straight to `card.py poll_result`.

--key: dedupe id (e.g. the match key from matches.py). If a poll with this key was already
sent, the script refuses unless --force. Default key = normalized question text.
Limits (Telegram): question 1-300 chars, 2-12 options of 1-100 chars each.
Token: env TELEGRAM_BOT_TOKEN (never printed). Dry-run sends nothing and records nothing.
"""
import argparse, json, os, re, sys
import requests
from common import POLLS_ENABLED, CHANNEL_ID, load_state, save_state, now_msk


def tg(method, token, data):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=data, timeout=60)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "description": f"HTTP {r.status_code}"}


def token_or_exit():
    t = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not t:
        sys.exit("TELEGRAM_BOT_TOKEN not set")
    return t


def summarize(key, p, winner=None):
    res = p.get("results") or {}
    total = p.get("total_voters")
    if total is None:
        total = sum(res.values())
    opts = []
    for text in p.get("options") or list(res):
        v = int(res.get(text, 0))
        opts.append({"text": text, "votes": v, "percent": round(100 * v / total, 1) if total else 0.0})
    out = {"key": key, "question": p.get("question"), "closed": bool(p.get("stopped_at") or p.get("is_closed")),
           "total_voters": total, "options": opts, "results": res}
    if p.get("stopped_at"):
        out["stopped_at"] = p["stopped_at"]
    if p.get("peek_at"):
        out["peek_at"] = p["peek_at"]
    winner = winner or p.get("winner")
    if winner:
        if winner not in res and winner not in (p.get("options") or []):
            sys.exit(f"--winner {winner!r} is not one of the options {p.get('options')}")
        out["winner"] = out["correct"] = winner
        out["guessed_percent"] = round(100 * int(res.get(winner, 0)) / total) if total else 0
    return out


def store_poll_obj(p, poll):
    p["results"] = {o["text"]: o["voter_count"] for o in poll.get("options", [])}
    p["total_voters"] = poll.get("total_voter_count")
    if poll.get("id"):
        p["poll_id"] = poll["id"]


def peek_updates(token, p):
    """Latest state of this poll from pending bot updates (no offset -> nothing is confirmed)."""
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"timeout": 0}, timeout=60).json()
    if not r.get("ok"):
        return None, r.get("description")
    found = None
    for u in r.get("result", []):
        poll = u.get("poll") or (u.get("channel_post") or {}).get("poll")
        if not poll:
            continue
        if (p.get("poll_id") and poll.get("id") == p["poll_id"]) or \
           (not p.get("poll_id") and poll.get("question") == p.get("question")):
            found = poll   # later updates overwrite earlier ones
    return found, None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--question", "-q")
    ap.add_argument("--option", "-o", action="append", default=[])
    ap.add_argument("--key")
    ap.add_argument("--chat", default=CHANNEL_ID)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="send even if this key was already polled")
    ap.add_argument("--stop", metavar="KEY")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--results", metavar="KEY")
    ap.add_argument("--winner", help="correct option (for guessed_percent)")
    a = ap.parse_args()

    st = load_state()
    polls = st.setdefault("polls", {})

    if a.list:
        print(json.dumps(polls, ensure_ascii=False, indent=2)); return
    if a.stop:
        p = polls.get(a.stop)
        if not p:
            sys.exit(f"no poll recorded under key {a.stop}")
        if a.dry_run:
            print(json.dumps({"method": "stopPoll", "chat_id": p["chat"], "message_id": p["message_id"]})); return
        res = tg("stopPoll", token_or_exit(), {"chat_id": p["chat"], "message_id": p["message_id"]})
        if not res.get("ok"):
            desc = res.get("description") or ""
            if "already been closed" in desc and p.get("results") is not None:
                print(json.dumps(dict(summarize(a.stop, p, a.winner), ok=True, note="poll was already closed; stored results"),
                                 ensure_ascii=False, indent=2)); return
            sys.exit(f"Telegram error: {desc}")
        store_poll_obj(p, res["result"])
        p["stopped_at"] = now_msk().isoformat(timespec="seconds")
        if a.winner:
            p["winner"] = a.winner
        out = summarize(a.stop, p, a.winner)
        save_state(st)
        print(json.dumps(dict(out, ok=True), ensure_ascii=False, indent=2)); return
    if a.results:
        p = polls.get(a.results)
        if not p:
            sys.exit(f"no poll recorded under key {a.results}")
        note = None
        if not p.get("stopped_at"):
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            poll, err = peek_updates(token, p) if token else (None, "TELEGRAM_BOT_TOKEN not set")
            if poll:
                store_poll_obj(p, poll)
                p["peek_at"] = now_msk().isoformat(timespec="seconds")
                p["is_closed"] = poll.get("is_closed", False)
                save_state(st)
                note = "open poll: live counts from pending bot updates (getUpdates, not confirmed)"
            else:
                note = ("open poll: no fresh vote update available" + (f" ({err})" if err else "") +
                        ("; showing last peek" if p.get("results") is not None else "; no counts yet - use --stop after the match"))
        out = dict(summarize(a.results, p, a.winner), ok=True)
        if note:
            out["note"] = note
        print(json.dumps(out, ensure_ascii=False, indent=2)); return

    q = (a.question or "").strip()
    opts = [o.strip() for o in a.option if o.strip()]
    if not q or not (1 <= len(q) <= 300):
        sys.exit("--question required, 1-300 chars")
    if not (2 <= len(opts) <= 12) or any(len(o) > 100 for o in opts):
        sys.exit("need 2-12 --option values, each <= 100 chars")
    if len(set(opts)) != len(opts):
        sys.exit("duplicate options")
    key = a.key or "q:" + re.sub(r"\W+", " ", q.lower()).strip()
    if key in polls and not a.force:
        sys.exit(f"poll with key {key!r} already sent at {polls[key].get('sent_at')} "
                 f"(message {polls[key].get('message_id')}); use --force to send again")

    if not POLLS_ENABLED:
        sys.exit("polls are disabled (common.POLLS_ENABLED=False): no poll sent")
    payload = {"chat_id": a.chat, "question": q, "options": [{"text": o} for o in opts],
               "is_anonymous": True, "type": "regular", "allows_multiple_answers": False}
    if a.dry_run:
        print(json.dumps({"method": "sendPoll", "key": key, "payload": payload}, ensure_ascii=False, indent=2))
        return
    res = tg("sendPoll", token_or_exit(), payload)
    if not res.get("ok"):
        sys.exit(f"Telegram error: {res.get('description')}")
    msg = res["result"]
    polls[key] = {"chat": a.chat, "message_id": msg.get("message_id"), "question": q, "options": opts,
                  "poll_id": (msg.get("poll") or {}).get("id"),
                  "sent_at": now_msk().isoformat(timespec="seconds")}
    st = st  # same object
    if len(polls) > 1000:
        st["polls"] = dict(sorted(polls.items(), key=lambda kv: kv[1].get("sent_at", ""))[-800:])
    save_state(st)
    print(json.dumps({"ok": True, "key": key, "message_id": msg.get("message_id"),
                      "chat": msg.get("chat", {}).get("username") or a.chat}, ensure_ascii=False))


if __name__ == "__main__":
    main()
