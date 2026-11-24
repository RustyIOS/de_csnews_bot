#!/usr/bin/env python3
"""Top-tier live posts without an always-on machine. Every 10 min, 10:00–23:59 MSK.

Posts two things, and only those:
  * match start — bo3 status is current, the start was not posted, and the match began
    within LIVE_START_FRESH_MIN (default 45). Older starts are dropped, not sent late.
  * match end — finished or technical win, with a score, not yet posted, and either the
    start was posted or the match ended within LIVE_RESULT_HOURS (default 3).

A catch-up uses live.collapse_stale: if the start and the final are both still unposted,
only the final goes out. At most LIVE_MAX_POSTS (default 2) per run; the rest wait for
the next tick, with the usual 75 s gap inside post.py.

A finished map whose score bo3 has not filled in yet is not posted and is not
marked sent. The tick fetches that match again, then asks Liquipedia. Still
empty, and the match ended under LIVE_SCORE_WAIT_MIN (default 60): the result
stays pending. After that the series score goes out without the empty map line.
A held result does not fail the run and does not take a slot from other posts.

Map start, half-time and map-result cards are not posted. On a 10-minute poll those
signals (especially Liquipedia halves and OT series) have been wrong often enough that
a late run must not emit a chain of them. The final uses the same result card as the
evening recap, from bo3's finished score.

Schedule filter is `matches.py --today --live` (events.json live_tiers=top): no streamer
league, no Stake Pulse. Polls stay off. --dry-run prints the text and writes the card,
and does not touch state.json.
"""
import argparse, datetime, html, json, os, re, sys
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_lib import (check_and_post, event_name, hashtags, log, run_json, single_instance,
                      stage_short, team)
from common import load_state, now_msk, save_state, valid_score
from live import collapse_stale

FRESH_MIN = float(os.environ.get("LIVE_START_FRESH_MIN", "45"))
RESULT_HOURS = float(os.environ.get("LIVE_RESULT_HOURS", "3"))
MAX_POSTS = int(os.environ.get("LIVE_MAX_POSTS", "2"))
SCORE_WAIT_MIN = float(os.environ.get("LIVE_SCORE_WAIT_MIN", "60"))
SOON_MIN = 15


def _ts(s):
    if not s:
        return None
    dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _score_ok(m):
    return bool(valid_score(m.get("score")))


def _real_teams(m):
    bad = {"", "TBD", "TBA"}
    return (m.get("team1") or "").strip().upper() not in bad and (m.get("team2") or "").strip().upper() not in bad


def start_due(m, seen, now):
    if "match:start" in seen or m.get("status") != "current" or not _real_teams(m):
        return False
    start = _ts(m.get("start_utc"))
    if not start:
        return False
    age = (now - start).total_seconds() / 60
    return -10 <= age <= FRESH_MIN


def end_due(m, seen, now):
    if "match:finished" in seen or not _real_teams(m) or not _score_ok(m):
        return False
    if m.get("status") not in ("finished", "defwin"):
        return False
    note = m.get("note") or ""
    if "incomplete" in note:
        return False
    if "match:start" in seen:
        return True
    end = _ts(m.get("end_utc")) or _ts(m.get("start_utc"))
    if not end:
        return False
    return (now - end).total_seconds() <= RESULT_HOURS * 3600


def _evt(m, typ, eid):
    return {"type": typ, "event_id": eid, "id": eid, "key": m["key"],
            "match": {"key": m["key"]}, "_m": m}


def plan_posts(m, seen, now):
    evs = []
    if start_due(m, seen, now):
        evs.append(_evt(m, "match_started", "match:start"))
    if end_due(m, seen, now):
        evs.append(_evt(m, "match_finished", "match:finished"))
    if not evs:
        return []
    keep, _sup = collapse_stale(evs)
    return keep


def is_watching(m, now):
    """Live, starting within 15 min, or overdue-but-not-yet-flipped (bo3 lag, < 6 h)."""
    k = str(m.get("key") or "")
    if not k.startswith("bo3:"):
        return False
    st = m.get("status")
    start = _ts(m.get("start_utc"))
    if st == "current":
        return True
    if st == "upcoming" and start:
        delta = (start - now).total_seconds()
        return -6 * 3600 < delta <= SOON_MIN * 60
    return False


def pending_result_keys(matches, now=None):
    """Finished top-tier matches whose final has not been posted. Used by collector --check
    so live.py --gate stays ACTIVE after bo3 flips the match to finished."""
    now = now or datetime.datetime.now(timezone.utc)
    le = (load_state().get("live_events") or {})
    out = []
    for m in matches:
        k = str(m.get("key") or "")
        if not k.startswith("bo3:"):
            continue
        seen = set((le.get(k) or {}).get("seen") or [])
        if end_due(m, seen, now):
            out.append(k)
    return out


def _tags(event):
    tags = hashtags([event] if event else []).split()
    if "#CS2" not in tags:
        tags.insert(0, "#CS2")
    if len(tags) < 2:
        tags.append("#матч")
    return " ".join(tags[:4])


def _winner_view(m):
    s1, s2 = (int(x) for x in m["score"].split(":"))
    flip = s2 > s1
    t1, t2 = team(m["team1"]), team(m["team2"])
    maps = []
    for mp in m.get("maps") or []:
        sc = valid_score(mp.get("score"))
        if not sc:
            continue
        a, b = sc.split(":")
        name = (mp.get("map") or "").strip()
        if re.match(r"(?i)^карта\s*\d+$", name):
            name = ""
        maps.append((name, f"{b}:{a}" if flip else sc))
    if flip:
        return t2, t1, f"{s2}:{s1}", maps, True
    return t1, t2, f"{s1}:{s2}", maps, False


def _copy_match(m):
    out = dict(m)
    out["maps"] = [dict(mp) for mp in (m.get("maps") or [])]
    return out


def missing_maps(m):
    """Finished maps whose score is still empty (None, 'None:None', blank)."""
    return [mp for mp in (m.get("maps") or []) if not valid_score(mp.get("score"))]


def _map_token(name):
    n = re.sub(r"[^a-z0-9]", "", str(name or "").lower().replace("de_", ""))
    return "dust2" if n == "dustii" else n


def _donor_score(mp, donors):
    name = _map_token(mp.get("map"))
    number = mp.get("number")
    by_num = None
    for donor in donors or []:
        sc = valid_score(donor.get("score"))
        if not sc:
            continue
        if name and _map_token(donor.get("map")) == name:
            return sc
        if number is not None and donor.get("number") == number and by_num is None:
            by_num = sc
    return by_num


def merge_map_scores(m, donors):
    """Fill empty map scores from donor rows. Real scores already on `m` stay."""
    m = _copy_match(m)
    for mp in m["maps"]:
        have = valid_score(mp.get("score"))
        if have:
            mp["score"] = have
            continue
        got = _donor_score(mp, donors)
        if got:
            mp["score"] = got
    return m


def maps_from_bo3(raw):
    if not raw:
        return []
    import matches as matches_mod
    return (matches_mod.bo3_norm(raw).get("maps") or [])


def maps_from_lp(raw):
    """Liquipedia finals oriented team1:team2, the same way bo3_norm stores them."""
    if not raw:
        return []
    import lp_live
    info = lp_live.live_maps(raw, {})
    if not info:
        return []
    rows = []
    for mi in info.get("maps") or []:
        final = mi.get("final")
        if not final or len(final) != 2:
            continue
        sc = valid_score(f"{final[0]}:{final[1]}")
        if not sc:
            continue
        rows.append({"map": mi.get("map"), "number": mi.get("n"), "score": sc})
    return rows


def _fetch_match(mid):
    import live
    return live.fetch_match(mid)


def _fetch_lp(raw):
    return maps_from_lp(raw)


def _lp_raw_from_match(m):
    """Enough of a bo3 match for lp_live when the fresh fetch itself failed."""
    return {
        "tournament": {"name": m.get("event")},
        "team1": {"name": m.get("team1")},
        "team2": {"name": m.get("team2")},
        "start_date": m.get("start_utc"),
    }


def refill_map_scores(m, fetch_match=None, fetch_lp=None):
    """Try a fresh bo3 match, then Liquipedia, for maps that still have no score."""
    m = _copy_match(m)
    if not missing_maps(m):
        return m
    key = str(m.get("key") or "")
    mid = None
    if key.startswith("bo3:"):
        try:
            mid = int(key.split(":", 1)[1])
        except ValueError:
            mid = None
    raw = None
    if mid is not None:
        fetcher = _fetch_match if fetch_match is None else fetch_match
        try:
            raw = fetcher(mid)
            if raw:
                m = merge_map_scores(m, maps_from_bo3(raw))
        except (Exception, SystemExit) as ex:
            log(f"{key}: fresh bo3 fetch failed ({ex})")
            raw = None
    if missing_maps(m):
        lp_fetch = _fetch_lp if fetch_lp is None else fetch_lp
        try:
            donors = lp_fetch(raw if raw is not None else _lp_raw_from_match(m))
        except (Exception, SystemExit) as ex:
            log(f"{key}: liquipedia score fetch failed ({ex})")
            donors = []
        if donors:
            m = merge_map_scores(m, donors)
    return m


def _as_dt(value):
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        dt = _ts(value)
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def score_wait_expired(m, now, hold_since=None):
    """True once the empty map score has had about SCORE_WAIT_MIN to show up.

    The clock is the match end when bo3 has one. Otherwise it is the first
    tick that noticed the hole (score_hold_since on the live_events row).
    """
    now = _as_dt(now)
    anchor = _as_dt(m.get("end_utc")) or _as_dt(hold_since)
    if anchor is None or now is None:
        return False
    return (now - anchor).total_seconds() >= SCORE_WAIT_MIN * 60


def drop_unscored_maps(m):
    m = _copy_match(m)
    kept = []
    for mp in m["maps"]:
        sc = valid_score(mp.get("score"))
        if not sc:
            continue
        mp["score"] = sc
        kept.append(mp)
    m["maps"] = kept
    return m


def prepare_finished(m, now, fetch_match=None, fetch_lp=None, hold_since=None):
    """('post', match) or ('hold', match).

    'hold' means the result stays pending: not sent, not marked seen.
    After the wait, 'post' keeps the series score and drops map lines that
    are still empty, so the caption never contains None.
    """
    m = refill_map_scores(m, fetch_match=fetch_match, fetch_lp=fetch_lp)
    pending = missing_maps(m)
    if not pending:
        return "post", m
    names = ", ".join((mp.get("map") or "map") for mp in pending)
    if score_wait_expired(m, now, hold_since):
        log(f"{m.get('key')}: posting without {names} "
            f"(score still missing after {SCORE_WAIT_MIN:g} min)")
        return "post", drop_unscored_maps(m)
    log(f"{m.get('key')}: {names} score not in yet")
    return "hold", m


def note_score_hold(key):
    """Remember when we first waited on a map score. Does not mark the result sent."""
    st = load_state()
    le = st.setdefault("live_events", {})
    rs = le.setdefault(key, {"seen": []})
    rs.setdefault("seen", [])
    if not rs.get("score_hold_since"):
        rs["score_hold_since"] = now_msk().isoformat(timespec="seconds")
    rs["updated"] = now_msk().isoformat(timespec="seconds")
    save_state(st)


def render_post(ev):
    """Return (text, card_template_or_None, card_dict)."""
    m = ev["_m"]
    ev_name = event_name(m.get("event"))
    stg = stage_short(m.get("stage"))
    if ev["type"] == "match_started":
        t1, t2 = team(m["team1"]), team(m["team2"])
        lines = [f"🟢 <b>Матч начался: {html.escape(t1)} — {html.escape(t2)}</b>", "",
                 f"🏆 {html.escape(ev_name)}"]
        bits = [x for x in (m.get("format"), stg) if x]
        if bits:
            lines.append(html.escape(", ".join(bits)))
        lines += ["", _tags(m.get("event"))]
        card = {"team1": t1, "team2": t2, "event": ev_name, "stage": stg or None,
                "format": m.get("format"), "team1_logo": m.get("team1_logo"),
                "team2_logo": m.get("team2_logo"), "team1_rank": m.get("team1_rank"),
                "team2_rank": m.get("team2_rank")}
        return "\n".join(lines), "match_start", card
    w, l, sc, maps, flip = _winner_view(m)
    lines = [f"🏁 <b>{html.escape(w)} {html.escape(sc)} {html.escape(l)}</b>", "",
             f"🏆 {html.escape(ev_name)}"]
    if maps:
        lines.append(html.escape(", ".join(f"{n} {s}" if n else s for n, s in maps)))
    if m.get("status") == "defwin" or "forfeit" in (m.get("note") or ""):
        lines.append("Техническая победа.")
    lines += ["", _tags(m.get("event"))]
    named = bool(maps) and all(n for n, _ in maps)
    card = {"event": ev_name, "stage": stg or None, "format": m.get("format"),
            "team1": w, "team2": l, "score": sc, "date_msk": m.get("date_msk"),
            "team1_logo": m.get("team2_logo") if flip else m.get("team1_logo"),
            "team2_logo": m.get("team1_logo") if flip else m.get("team2_logo"),
            "maps": [{"map": n, "score": s} for n, s in maps] if named else []}
    return "\n".join(lines), "result", card


def load_live_matches():
    data = run_json(["matches.py", "--today", "--live"])
    ms = [m for m in data.get("matches") or [] if str(m.get("key") or "").startswith("bo3:")]
    return data, ms


def assess(now=None):
    now = now or datetime.datetime.now(timezone.utc)
    data, ms = load_live_matches()
    le = (load_state().get("live_events") or {})
    posts, watching = [], []
    for m in ms:
        if is_watching(m, now):
            watching.append(m["key"])
        seen = set((le.get(m["key"]) or {}).get("seen") or [])
        posts.extend(plan_posts(m, seen, now))
    posts.sort(key=_sort_key)
    if posts:
        brief = ", ".join(f"{p['type']} {p['key']}" for p in posts[:6])
        reason = f"{len(posts)} live post(s) due: {brief}"
    elif watching:
        reason = f"idle: {len(watching)} top-tier match(es) live or soon, nothing new to post"
    else:
        reason = "idle: no top-tier match live or starting within 15 min, no pending result"
    if data.get("errors"):
        reason += f" (errors: {data.get('errors')})"
    return {"posts": posts, "watching": watching, "reason": reason, "count": len(ms)}


def _sort_key(ev):
    m = ev["_m"]
    end = _ts(m.get("end_utc")) or _ts(m.get("start_utc"))
    stamp = end.timestamp() if end else 0
    return (0 if ev["type"] == "match_finished" else 1, -stamp)


def remember(key, event_id, status):
    st = load_state()
    le = st.setdefault("live_events", {})
    rs = le.setdefault(key, {"seen": []})
    seen = list(rs.get("seen") or [])
    if event_id not in seen:
        seen.append(event_id)
    rs["seen"] = seen[-400:]
    rs["updated"] = now_msk().isoformat(timespec="seconds")
    rs["match_status"] = status
    cutoff = (now_msk() - datetime.timedelta(days=3)).isoformat()
    for k in [k for k, v in le.items() if (v.get("updated") or "") < cutoff]:
        le.pop(k, None)
    save_state(st)


def send_one(ev, dry, now=None):
    now = now or datetime.datetime.now(timezone.utc)
    m = ev["_m"]
    if ev["type"] == "match_finished":
        hold_since = ((load_state().get("live_events") or {}).get(m.get("key")) or {}).get("score_hold_since")
        status, m = prepare_finished(m, now, hold_since=hold_since)
        if status == "hold":
            log(f"HOLD {m.get('key')}: map score missing, still pending")
            if not dry:
                note_score_hold(m["key"])
            return "hold"
        ev = dict(ev, _m=m)
    text, template, card = render_post(ev)
    os.makedirs("drafts", exist_ok=True)
    os.makedirs("cards/out", exist_ok=True)
    safe = str(m["key"]).replace(":", "-")
    kind = "start" if ev["type"] == "match_started" else "end"
    sfx = "-dry" if dry else ""
    tp = f"drafts/live-{kind}-{safe}{sfx}.txt"
    jp = f"drafts/live-{kind}-{safe}{sfx}.json"
    cp = f"cards/out/live-{kind}-{safe}{sfx}.jpg"
    open(tp, "w", encoding="utf-8").write(text + "\n")
    json.dump(card, open(jp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    from auto_lib import run
    rc, _out, err = run(["card.py", template, "--json", jp, "-o", cp])
    if rc != 0 or not os.path.isfile(cp):
        log(f"card failed ({(err or '')[-300:]}) -> text-only"); cp = ""
    log(f"text:\n{text}\ncard: {cp or '-'}")
    url = m.get("url") or "https://bo3.gg/matches"
    hours = "12" if ev["type"] == "match_finished" else "6"
    ok = check_and_post(f"live-{kind}-{safe}", tp, cp, dry, url, check_args=["--since-hours", hours])
    if ok and not dry:
        remember(m["key"], ev["event_id"], m.get("status"))
    return bool(ok)


def run_due(posts, dry, send, max_posts=MAX_POSTS):
    """Send up to max_posts. A held result is skipped, not a failure, and does not use a slot."""
    sent = held = 0
    for i, ev in enumerate(posts):
        if sent >= max_posts:
            log(f"catch-up cap: sending {sent}, leaving {len(posts) - i} for the next run")
            break
        status = send(ev, dry)
        if status == "hold":
            held += 1
            continue
        if not status:
            log("FAILED")
            return 1
        sent += 1
    if held:
        log(f"held {held} result(s) for a map score; next tick retries them")
    log("DONE")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--assess", action="store_true", help="print the plan as JSON, post nothing")
    a = ap.parse_args(argv)
    if a.assess:
        info = assess()
        show = {"reason": info["reason"], "watching": info["watching"], "count": info["count"],
                "posts": [{"type": p["type"], "key": p["key"]} for p in info["posts"]]}
        print(json.dumps(show, ensure_ascii=False, indent=1))
        return 0
    lock = single_instance("live_auto")
    info = assess()
    log(info["reason"])
    if not info["posts"]:
        return 0
    return run_due(info["posts"], a.dry_run, send_one)


if __name__ == "__main__":
    sys.exit(main())
