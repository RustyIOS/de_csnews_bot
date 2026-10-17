#!/usr/bin/env python3
"""Live in-match events for a CS2 match (bo3.gg), deduped via state.json -> "live_events".

Usage:
  python3 live.py bo3:130754 [--brief] [--dry-run] [--late] [--x-file DUMP ...]
  python3 live.py bo3:130754 --listen [--minutes 300]   # background websocket recorder (true live rounds)
  python3 live.py --list | --reset bo3:130754

Output: JSON list of NEW events since the last run (or --brief text). Nothing is posted.
Event types:
  map_started   {map, map_number}
  pistol        {map, map_number, round (1|13), winner, side, score}       (needs live feed or --late)
  ace           {map, map_number, round, player, team}                     (needs live feed or --late)
  half_time     {map, map_number, score "7:5" (team1:team2), sides {team: "CT"|"T"} for the first half}
  map_finished  {map, map_number, winner, loser, score "13:9"/"16:14" (winner first), overtime, overtimes,
                 series "1:0" (team1:team2)}
  match_started {stage, format, first_map, scheduled_utc, map1_begin_utc, options, poll_key "live:bo3:<id>"}
                (emitted once when bo3 status turns "current"; post "Матч начался" + poll.py --key <poll_key>)
  match_finished {winner, loser, series (team1:team2), maps[], forfeit, poll_key}  -> poll.py --stop <poll_key>
  x_highlight   {kind ace|pistol|clutch|map|other, author, text, url, created_msk}   (from --x-file)
Every event has: id (dedupe key), key, team1, team2, source, delay_note, late (bool).

Sources, best first:
  1. bo3.gg live websocket wss://live.bo3.gg/ws/live_game/<match_id> - GAME_EVENT_MATCH_END_ROUND
     (winning_team_id/side, round_number), AGGREGATED_GAME_STATE (team_one/two, player_states.kills_in_round).
     Only works while live.py --listen is running (messages are buffered to live_buf/<id>.jsonl) AND bo3 has
     live coverage for the match (match.parsed_status != "waiting"). Real-time (seconds).
  2. bo3.gg REST snapshot /api/v1/live/matches/<id>/last_snapshot - same data, polled.
  3. bo3.gg REST match/games - map status + final map score (map_won/match_won); minutes of delay.
  3b. Liquipedia tournament wikitext (lp_live.py): editors fill first side + first-half split live -> half_time
     ~1-5 min after round 12 (editor-dependent), and finished map scores (map_finished fallback). No per-round score.
  4. --late: bo3.gg REST games/<id>/rounds + rounds/<n>/players_stats (demo-parsed, exact pistol winners
     and aces) - appears only 2-3 h after the map ends. Events are flagged late=true.
  5. --x-file: X search dumps (routine agent runs x search_posts_all; see README) from ESLCS/HLTVorg/BLAST/PGL
     and the two teams' accounts, posted during the match: aces/clutches/pistols as highlight posts (1-3 min delay).
"""
import argparse, json, os, re, subprocess, sys, time, urllib.parse
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
from datetime import datetime, timedelta, timezone
from common import UA, MSK, BASE, load_json, save_json, load_state, save_state, now_msk

API = "https://api.bo3.gg/api/v1"
BUF_DIR = os.path.join(BASE, "live_buf")
WS_URL = "wss://live.bo3.gg/ws/live_game/{}"
X_CORE = {"eslcs", "hltvorg", "blastpremier", "pglesports", "counterstrike"}
MAX_SEEN = 400


def get(path, params=None, timeout=20, ok404=False):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, safe="[],")
    p = subprocess.run(["curl", "-g", "-sL", "--compressed", "--max-time", str(timeout), "-A", UA,
                        "-w", "\n%{http_code}", url], capture_output=True)
    body, _, code = p.stdout.rpartition(b"\n")
    code = int(code) if code.strip().isdigit() else 0
    if code == 404 and ok404:
        return None
    if code != 200:
        raise RuntimeError(f"HTTP {code} for {url.split('?')[0]}")
    return json.loads(body)


def mapname(m):
    return (m or "").replace("de_", "").capitalize() or None


def fetch_match(mid):
    d = get("/matches", {"filter[matches.id][eq]": mid, "with": "teams,games,tournament,stage,round"})
    res = d.get("results") or []
    if not res:
        raise SystemExit(f"bo3 match {mid} not found")
    return res[0]


class Ctx:
    def __init__(self, m):
        self.m = m
        t1, t2 = m.get("team1") or {}, m.get("team2") or {}
        self.t1id, self.t2id = m.get("team1_id"), m.get("team2_id")
        self.t1, self.t2 = t1.get("name") or "TBD", t2.get("name") or "TBD"
        self.slugs = {self.t1id: t1.get("slug"), self.t2id: t2.get("slug")}

    def name(self, team_id=None, clan=None):
        if team_id == self.t1id:
            return self.t1
        if team_id == self.t2id:
            return self.t2
        return self.norm_clan(clan)

    def norm_clan(self, clan):
        """bo3 clan names differ from team names ("Team Spirit" vs "Spirit")."""
        if not clan:
            return clan
        c = clan.lower()
        for n in (self.t1, self.t2):
            if n.lower() in c or c in n.lower():
                return n
        return clan

    def ev(self, etype, eid, **kw):
        e = {"id": eid, "type": etype, "key": f"bo3:{self.m['id']}", "team1": self.t1, "team2": self.t2,
             "event": (self.m.get("tournament") or {}).get("name"), "late": False}
        e.update(kw)
        return e


# ---------------- 3. REST map / match status ----------------
def rest_events(ctx):
    m, out = ctx.m, []
    s = [0, 0]
    games = sorted(m.get("games") or [], key=lambda g: g.get("number") or 0)
    if m.get("status") in ("current", "finished") and any(g.get("status") in ("current", "finished") for g in games) \
            or m.get("status") == "current":
        g1 = games[0] if games else {}
        stage = (m.get("stage") or {}).get("title")
        tn = (m.get("tournament") or {}).get("name")
        if stage and tn and stage.startswith(tn):
            stage = stage[len(tn):].strip(" -:") or None
        stage = " / ".join(x for x in (stage, (m.get("round") or {}).get("name")) if x)
        out.append(ctx.ev("match_started", "match:start", stage=stage or None,
                          format=f"BO{m['bo_type']}" if m.get("bo_type") else None,
                          first_map=mapname(g1.get("map_name")), scheduled_utc=m.get("start_date"),
                          map1_begin_utc=g1.get("begin_at"), poll_key=f"live:bo3:{m['id']}",
                          options=[ctx.t1, ctx.t2], source="bo3 REST"))
    for g in sorted(m.get("games") or [], key=lambda g: g.get("number") or 0):
        n, mp = g.get("number"), mapname(g.get("map_name"))
        if g.get("status") in ("current", "finished") and mp:
            out.append(ctx.ev("map_started", f"map{n}:start", map=mp, map_number=n, source="bo3 REST",
                              at=g.get("begin_at")))
        if g.get("status") == "finished" and g.get("winner_clan_score") is not None:
            wid = (g.get("winner_team_clan") or {}).get("team_id")
            lid = (g.get("loser_team_clan") or {}).get("team_id")
            w = ctx.name(wid, g.get("winner_clan_name"))
            l = ctx.name(lid, g.get("loser_clan_name"))
            if wid == ctx.t1id or w == ctx.t1:
                s[0] += 1
            else:
                s[1] += 1
            ws_, ls_ = g["winner_clan_score"], g["loser_clan_score"]
            total = (ws_ or 0) + (ls_ or 0)
            ot = total > 24
            out.append(ctx.ev("map_finished", f"map{n}:finished", map=mp, map_number=n, winner=w, loser=l,
                              score=f"{ws_}:{ls_}", overtime=ot,
                              overtimes=((total - 24 + 5) // 6) if ot else 0,
                              series=f"{s[0]}:{s[1]}", source="bo3 REST"))
    if m.get("status") in ("finished", "defwin") and m.get("winner_team_id"):
        w = ctx.name(m["winner_team_id"])
        l = ctx.t2 if w == ctx.t1 else ctx.t1
        out.append(ctx.ev("match_finished", "match:finished", winner=w, loser=l,
                          series=f"{m.get('team1_score')}:{m.get('team2_score')}",
                          maps=[{"map": e["map"], "winner": e["winner"], "score": e["score"]}
                                for e in out if e["type"] == "map_finished"],
                          forfeit=m.get("status") == "defwin", poll_key=f"live:bo3:{m['id']}",
                          source="bo3 REST"))
    return out


# ---------------- 1/2. live feed (websocket buffer + REST snapshot) ----------------
def _teams(msg):
    """Return [(team_id, name, side, score, players[(nick, kills_in_round)])] from a snapshot/game-state message."""
    res = []
    for k in ("team_one", "team_two"):
        t = msg.get(k)
        if not isinstance(t, dict):
            continue
        fx = t.get("fixture") or {}
        players = []
        for p in t.get("player_states") or []:
            pf = p.get("fixture") or {}
            nick = pf.get("player_nickname") or p.get("nickname") or p.get("name") or pf.get("name")
            players.append((nick, p.get("kills_in_round")))
        res.append((fx.get("team_id") or t.get("team_id") or t.get("id"), fx.get("name") or t.get("name"),
                    (t.get("side") or "").upper(), t.get("score"), players))
    return res


def feed_events(ctx, msgs, rs):
    """msgs: decoded live messages in order. rs: per-match round state (mutated)."""
    out = []
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        mt = msg.get("message_type") or ""
        gnum = msg.get("game_number") or rs.get("game_number")
        if gnum:
            rs["game_number"] = gnum
        mp = mapname(msg.get("map_name")) or rs.get("map")
        if mp:
            rs["map"] = mp
        teams = _teams(msg)
        if teams:
            rs["teams"] = [[t[0], t[1], t[2], t[3]] for t in teams]
            # remember kills_in_round per player (last value seen before the round ends)
            rn = msg.get("round_number")
            for tid, tname, side, score, players in teams:
                for nick, kir in players:
                    if nick and isinstance(kir, int) and kir >= 5:
                        r = rn or "?"
                        out.append(ctx.ev("ace", f"map{gnum}:ace:{r}:{nick}", map=mp, map_number=gnum, round=rn,
                                          player=nick, team=ctx.name(tid, tname), source="bo3 live"))
        if mt == "GAME_EVENT_MATCH_END_ROUND" or ("round_number" in msg and teams):
            rn = msg.get("round_number")
            if mt == "GAME_EVENT_MATCH_END_ROUND" and rn in (1, 13):
                wid = msg.get("winning_team_id")
                side = (msg.get("winning_team_side") or "").upper()
                wname = msg.get("winning_team_name")
                if not wid and side and rs.get("teams"):
                    for t in rs["teams"]:
                        if t[2] == side:
                            wid, wname = t[0], t[1]
                w = ctx.name(wid, wname)
                out.append(ctx.ev("pistol", f"map{gnum}:pistol{rn}", map=mp, map_number=gnum, round=rn,
                                  winner=w, side=side or None, source="bo3 live"))
            # score-based pistol detection (snapshot polling): total rounds 1 or 13 just observed
            if teams and all(isinstance(t[3], int) for t in teams):
                tot = sum(t[3] for t in teams)
                prev = rs.get("scores", {}).get(str(gnum))
                rs.setdefault("scores", {})[str(gnum)] = [t[3] for t in teams]
                if prev and sum(prev) + 1 == tot and tot in (1, 13):
                    for t, p in zip(teams, prev):
                        if t[3] == p + 1:
                            out.append(ctx.ev("pistol", f"map{gnum}:pistol{tot}", map=mp, map_number=gnum,
                                              round=tot, winner=ctx.name(t[0], t[1]), side=t[2] or None,
                                              score=f"{teams[0][3]}:{teams[1][3]}", source="bo3 live"))
                elif not prev and tot == 1:
                    for t in teams:
                        if t[3] == 1:
                            out.append(ctx.ev("pistol", f"map{gnum}:pistol1", map=mp, map_number=gnum, round=1,
                                              winner=ctx.name(t[0], t[1]), side=t[2] or None, score="1:0" if
                                              t is teams[0] else "0:1", source="bo3 live"))
                if tot == 12 or (mt == "GAME_EVENT_MATCH_END_ROUND" and msg.get("round_number") == 12):
                    out.append(half_event(ctx, gnum, mp, [(ctx.name(t[0], t[1]), t[2], t[3]) for t in teams],
                                          "bo3 live"))
                rs["live_score"] = {"map": mp, "map_number": gnum,
                                    "score": f"{teams[0][3]}:{teams[1][3]}",
                                    "teams": [ctx.name(t[0], t[1]) for t in teams]}
        if mt == "GAME_EVENT_MATCH_END_MAP" and msg.get("winning_team_name"):
            pass  # map_finished comes from REST with the final score (dedupe id map{n}:won)
    return out


def read_buffer(mid, rs):
    path = os.path.join(BUF_DIR, f"{mid}.jsonl")
    if not os.path.exists(path):
        return []
    off = rs.get("buf_offset", 0)
    msgs = []
    with open(path, encoding="utf-8") as f:
        f.seek(off)
        for line in f:
            try:
                rec = json.loads(line)
                m = json.loads(rec["m"]) if isinstance(rec.get("m"), str) else rec.get("m")
                if isinstance(m, dict) and isinstance(m.get("data"), list):
                    msgs.extend(m["data"])
                elif isinstance(m, list):
                    msgs.extend(m)
                else:
                    msgs.append(m)
            except Exception:
                continue
        rs["buf_offset"] = f.tell()
    return msgs


def snapshot(mid):
    try:
        s = get(f"/live/matches/{mid}/last_snapshot", {"discipline_id": 1}, ok404=True)
    except RuntimeError:
        return None
    return s


def half_event(ctx, n, mp, rows, source, late=False):
    """rows: [(team name, side played in 1st half, score after 12 rounds)]"""
    sc = {r[0]: r[2] for r in rows}
    return ctx.ev("half_time", f"map{n}:half", map=mp, map_number=n,
                  score=f"{sc.get(ctx.t1, '?')}:{sc.get(ctx.t2, '?')}",
                  sides={r[0]: r[1] for r in rows if r[1]}, source=source, late=late)


# ---------------- 3b. Liquipedia (editors fill half splits live) ----------------
def lp_events(ctx, rs):
    """half_time from Liquipedia wikitext (t1firstside + first-half splits, ~1-5 min after round 12, editor-dependent);
    map_finished fallback when LP marks the map finished before bo3 REST (same dedupe id)."""
    import lp_live
    cache = rs.setdefault("lp_titles", {})
    r = lp_live.live_maps(ctx.m, cache)
    if not r:
        return [], None
    out, s, live = [], [0, 0], None
    for mi in r["maps"]:
        n, mp = mi["n"], mi["map"]
        if mi["skip"]:
            continue
        bo3done = any(g.get("number") == n and g.get("status") == "finished" for g in ctx.m.get("games") or [])
        if mi["half"] and not mi["final"] and not bo3done:   # stale half-time after map end is not posted
            h1, h2 = mi["half"]
            f1 = mi["first_side_t1"]
            f2 = {"CT": "T", "T": "CT"}.get(f1)
            out.append(half_event(ctx, n, mp, [(ctx.t1, f1, h1), (ctx.t2, f2, h2)], "Liquipedia"))
            live = {"map": mp, "map_number": n, "score": f"{h1}:{h2}", "after": "half", "source": "Liquipedia"}
        if mi["final"]:
            a, b = mi["final"]
            s[0 if a > b else 1] += 1
            w, l = (ctx.t1, ctx.t2) if a > b else (ctx.t2, ctx.t1)
            ot = a + b > 24
            out.append(ctx.ev("map_finished", f"map{n}:finished", map=mp, map_number=n, winner=w, loser=l,
                              score=f"{max(a, b)}:{min(a, b)}", overtime=ot,
                              overtimes=((a + b - 24 + 5) // 6) if ot else 0,
                              series=f"{s[0]}:{s[1]}", source="Liquipedia"))
            live = None
    return out, {"rev_ts": r["rev_ts"], "page": r["title"], "live": live}


# ---------------- 4. late (demo-parsed) rounds ----------------
def late_events(ctx, rs):
    out = []
    done = set(rs.get("late_done", []))
    for g in sorted(ctx.m.get("games") or [], key=lambda g: g.get("number") or 0):
        if g.get("status") != "finished" or g["id"] in done:
            continue
        d = get(f"/games/{g['id']}/rounds")
        rounds = d.get("game_rounds") or []
        if not rounds:
            continue
        n, mp = g.get("number"), mapname(g.get("map_name"))
        for r in rounds:
            if r.get("round_number") == 12:
                w, l = ctx.norm_clan(r.get("winner_clan_name")), ctx.norm_clan(r.get("loser_clan_name"))
                out.append(half_event(ctx, n, mp, [(w, r.get("winner_clan_side"), r.get("winner_clan_score")),
                                                   (l, r.get("loser_clan_side"), r.get("loser_clan_score"))],
                                      "bo3 demo", late=True))
            if r.get("round_number") in (1, 13):
                out.append(ctx.ev("pistol", f"map{n}:pistol{r['round_number']}", map=mp, map_number=n,
                                  round=r["round_number"], winner=ctx.norm_clan(r.get("winner_clan_name")),
                                  side=r.get("winner_clan_side"),
                                  score=f"{r.get('winner_clan_score')}:{r.get('loser_clan_score')} (winner first)",
                                  source="bo3 demo", late=True))
        ps = get(f"/games/{g['id']}/players_stats") or []
        nick = {p.get("steam_profile_id"): ((p.get("steam_profile") or {}).get("player") or {}).get("nickname")
                or (p.get("steam_profile") or {}).get("nickname") for p in ps}
        clan = {p.get("steam_profile_id"): p.get("clan_name") for p in ps}
        if any((p.get("multikills") or {}).get("5") for p in ps):
            # find the round(s): team kills == 5 by one player
            for r in rounds:
                if not any(c.get("kills", 0) >= 5 for c in r.get("game_round_team_clans") or []):
                    continue
                rp = get(f"/games/{g['id']}/rounds/{r['round_number']}/players_stats") or []
                for p in rp:
                    if (p.get("kills") or 0) >= 5:
                        who = nick.get(p.get("steam_profile_id")) or str(p.get("steam_profile_id"))
                        out.append(ctx.ev("ace", f"map{n}:ace:{r['round_number']}:{who}", map=mp, map_number=n,
                                          round=r["round_number"], player=who,
                                          team=ctx.norm_clan(p.get("clan_name") or clan.get(p.get("steam_profile_id"))),
                                          source="bo3 demo", late=True))
                time.sleep(0.3)
        done.add(g["id"])
    rs["late_done"] = sorted(done)
    return out


# ---------------- 5. X fallback ----------------
KIND = [("ace", re.compile(r"\bace\b|\b5k\b|ЭЙС", re.I)),
        ("pistol", re.compile(r"pistol", re.I)),
        ("clutch", re.compile(r"clutch|1v[2-5]\b|1 ?vs ?[2-5]", re.I)),
        ("multikill", re.compile(r"\bquad\b|\b4k\b|4 ?kills?", re.I)),
        ("map", re.compile(r"\b(take|takes|win|wins|won|claim|seal|secure|tie|level)\b.*\b(map|mirage|nuke|inferno|"
                           r"anubis|ancient|dust2|dust 2|train|overpass|vertigo|cache)\b|map point|series|\b[0-3] ?[-:] ?[0-3]\b.*\bmap|"
                           r"\b(up next|next map|heading to)\b", re.I))]


def load_x(paths):
    posts, users = [], {}
    for p in paths:
        raw = sys.stdin.read() if p == "-" else open(p, encoding="utf-8").read()
        chunks = []
        i = raw.find("{")
        try:
            chunks.append(json.loads(raw[i:raw.rfind("}") + 1]))
        except Exception:
            try:
                chunks.extend(json.loads(raw))
            except Exception:
                continue
        for d in chunks:
            if isinstance(d, dict) and "data" in d:
                for u in (d.get("includes") or {}).get("users", []):
                    users[u["id"]] = u.get("username")
                posts.extend(d.get("data") or [])
            elif isinstance(d, dict) and "id" in d:
                posts.append(d)
    for p in posts:
        p["username"] = p.get("username") or users.get(p.get("author_id"))
    return posts


def x_events(ctx, posts, team_handles):
    out = []
    start = datetime.fromisoformat(ctx.m["start_date"].replace("Z", "+00:00")) - timedelta(minutes=20)
    end = datetime.fromisoformat(ctx.m["end_date"].replace("Z", "+00:00")) + timedelta(minutes=30) \
        if ctx.m.get("end_date") else datetime.now(timezone.utc) + timedelta(minutes=5)
    names = [n.lower() for n in (ctx.t1, ctx.t2)] + [h.lower() for h in team_handles]
    for p in posts:
        u = (p.get("username") or "").lower()
        ts = p.get("created_at")
        if not ts:
            continue
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if not (start <= t <= end):
            continue
        text = p.get("text") or ""
        tl = text.lower()
        is_team = u in [h.lower() for h in team_handles]
        if not is_team and u not in X_CORE:
            continue
        if u in X_CORE and not is_team:
            # core accounts post about several matches; require a team mention unless only this match is live
            if not any(n in tl for n in names) and not any(KIND[i][1].search(text) for i in (0, 2, 3)):
                continue
        kind = next((k for k, rx in KIND if rx.search(text)), "other")
        if kind == "other":
            continue
        out.append(ctx.ev("x_highlight", f"x:{p['id']}", kind=kind, author=p.get("username"), text=text,
                          url=f"https://x.com/{p.get('username')}/status/{p['id']}",
                          created_msk=t.astimezone(MSK).strftime("%H:%M"), source="X"))
    return out


def team_handles(ctx):
    acc = load_json(os.path.join(BASE, "x_accounts.json"), {}).get("accounts", [])
    out = []
    for a in acc:
        if a.get("group") != "teams":
            continue
        u = a["username"].lower()
        for n in (ctx.t1, ctx.t2):
            k = re.sub(r"[^a-z0-9]", "", n.lower())
            if k and (k in re.sub(r"[^a-z0-9]", "", u) or k in (a.get("note") or "").lower()):
                out.append(a["username"])
    # aliases
    alias = {"mouz": "mousesports", "navi": "natusvincere", "spirit": "Team__Spirit", "aurora": "AuroraCS2_GG"}
    for n in (ctx.t1, ctx.t2):
        if n.lower() in alias:
            out.append(alias[n.lower()])
    return sorted(set(out))


# ---------------- listener ----------------
def listen(mid, minutes):
    import asyncio
    try:
        import websockets
    except ImportError:
        raise SystemExit("pip install --user --break-system-packages websockets")
    os.makedirs(BUF_DIR, exist_ok=True)
    path = os.path.join(BUF_DIR, f"{mid}.jsonl")
    end = time.time() + minutes * 60

    async def run():
        while time.time() < end:
            try:
                async with websockets.connect(WS_URL.format(mid), origin="https://bo3.gg", open_timeout=15,
                                              ping_interval=None, user_agent_header=UA) as ws:
                    print(now_msk().strftime("%H:%M:%S"), "connected", flush=True)

                    async def pinger():
                        while True:
                            await asyncio.sleep(10)
                            await ws.send("ping")
                    pt = asyncio.create_task(pinger())
                    try:
                        while time.time() < end:
                            msg = await asyncio.wait_for(ws.recv(), timeout=max(1, end - time.time()))
                            if msg == "pong":
                                continue
                            with open(path, "a", encoding="utf-8") as f:
                                f.write(json.dumps({"t": time.time(), "m": msg}, ensure_ascii=False) + "\n")
                    finally:
                        pt.cancel()
            except asyncio.TimeoutError:
                break
            except Exception as e:
                print(now_msk().strftime("%H:%M:%S"), "ws error:", repr(e)[:200], flush=True)
                await asyncio.sleep(5)
    asyncio.run(run())


# ---------------- main ----------------
def brief(e):
    t = e["type"]
    tag = " [late]" if e.get("late") else ""
    if t == "map_started":
        return f"Карта {e['map_number']} ({e['map']}) началась"
    if t == "pistol":
        return f"{e['map']} r{e['round']}: пистолетку взяли {e['winner']}{(' ('+e['side']+')') if e.get('side') else ''}{tag}"
    if t == "ace":
        return f"{e['map']} r{e['round']}: ЭЙС {e['player']} ({e.get('team')}){tag}"
    if t == "half_time":
        sides = ", ".join(f"{k} {v}" for k, v in (e.get("sides") or {}).items())
        return f"{e['map']}: перерыв, {e['team1']} {e['score']} {e['team2']} (1-я половина: {sides}){tag}"
    if t == "map_finished":
        ot = f", овертайм x{e['overtimes']}" if e.get("overtime") else ""
        return (f"{e['map']} (карта {e['map_number']}): {e['winner']} {e['score']} {e['loser']}{ot}; "
                f"серия {e['team1']} {e['series']} {e['team2']}")
    if t == "match_started":
        return (f"Матч начался: {e['team1']} vs {e['team2']} ({e.get('format')}, {e.get('event')}, {e.get('stage')}), "
                f"1-я карта {e.get('first_map') or '?'}; poll key {e['poll_key']}")
    if t == "match_finished":
        return (f"Матч окончен: {e['winner']} победили {e['loser']}, серия {e['team1']} {e['series']} {e['team2']}"
                f"{' (тех. победа)' if e.get('forfeit') else ''}")
    if t == "x_highlight":
        return f"X {e['created_msk']} MSK @{e['author']} [{e['kind']}]: {e['text'][:140]!r} {e['url']}"
    return json.dumps(e, ensure_ascii=False)


# ---- single-flight + idle gate (LESSONS 22) ----
LIVE_LOCK = os.path.join(BASE_DIR, ".live.lock")      # flock held by every live.py run (except --listen / cs: reads)
LIVE_LEASE = os.path.join(BASE_DIR, ".live.lease")    # one live ROUTINE run at a time (gate ... release)
LEASE_MIN = 20                                        # a lease older than this is stale (crashed / killed run)


def take_flock():
    import fcntl
    fh = open(LIVE_LOCK, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("BUSY: another live.py run holds .live.lock - exiting (nothing done)")
        sys.exit(0)
    fh.seek(0); fh.truncate(); fh.write(f"{os.getpid()} {now_msk().isoformat(timespec='seconds')}\n"); fh.flush()
    return fh                                          # keep a reference; released on exit


def gate(a):
    """Routine entry point. Prints one of IDLE / BUSY / ACTIVE as the first word; always exit 0."""
    # Box-only: revive sched/ after a restart. On Actions the workflow is the scheduler;
    # starting a watchdog here would fight it. NO_SCHED=1 or GITHUB_ACTIONS skips this.
    if os.environ.get("NO_SCHED") != "1" and os.environ.get("GITHUB_ACTIONS") != "true":
        try:
            subprocess.run([os.path.join(BASE_DIR, "sched", "ensure.sh")], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20)
        except Exception:
            pass
    fh = take_flock()
    try:
        lease = json.load(open(LIVE_LEASE))
        age = (now_msk() - datetime.fromisoformat(lease["started"])).total_seconds() / 60
        if age < LEASE_MIN and not a.force_gate:
            print(f"BUSY: live run started {lease['started']} ({age:.0f} min ago) still holds .live.lease - exit now")
            return
    except Exception:
        pass
    cmd = [sys.executable, os.path.join(BASE_DIR, "collector.py"), "--check"]
    if a.sim_schedule:
        cmd += ["--sim-schedule", a.sim_schedule]
    if a.ignore_window:
        cmd += ["--ignore-window"]
    try:
        res = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=90).stdout.strip().splitlines()[-1])
    except Exception as ex:
        res = {"needed": None, "reason": f"check failed: {ex!r}"[:200]}
    if res.get("needed") == []:
        print(f"IDLE: {res.get('reason')} - stop now (no X, no TG scans, no collector, no news_queue)")
        return
    json.dump({"started": now_msk().isoformat(timespec="seconds"), "pid": os.getpid(), "needed": res.get("needed")},
              open(LIVE_LEASE, "w"))
    print("ACTIVE:", json.dumps(res, ensure_ascii=False), "- lease taken; run `live.py --release` at the end")


def release():
    try:
        os.remove(LIVE_LEASE)
        print("released .live.lease")
    except FileNotFoundError:
        print("no lease")


# ---- catch-up burst control (lesson 23) ----------------------------------------------------------
LIVE_GAP_SEC = int(os.environ.get("LIVE_GAP_SEC", "75"))   # min spacing between live posts in the channel
_MAPRE = __import__("re").compile(r"map(\d+):(start|half|end|finished)")


def _evt_pos(e):
    """(map_number, phase) for ordering; phase 0=start 1=half/moments 2=end. match:start=(0,0), match end=(99,3)."""
    eid, t = str(e.get("event_id", "")), e.get("type", "")
    if t == "match_finished" or eid in ("match:finished", "match:end", "end"):
        return (99, 3)
    if t == "match_started" or eid in ("match:start", "start"):
        return (0, 0)
    m = _MAPRE.match(eid)
    n = int(m.group(1)) if m else int(e.get("map_number") or (e.get("data") or {}).get("map_number") or 0)
    ph = {"start": 0, "half": 1, "end": 2, "finished": 2}.get(m.group(2) if m else "", 1)
    return (n, ph)


def collapse_stale(events):
    """Catch-up rule: when several events of ONE match are pending at once, only the newest state is posted.
    Returns (keep, superseded). Keeps: the latest map/match event per match, plus match_started if the match
    is still running (first post of the series). Everything older (earlier map results, earlier map starts,
    half-times and pistols/aces of finished maps) is superseded: its info goes into the kept post's text
    ('Hooch и Ct0m разошлись по одной: 16:14 и 13:9')."""
    by = {}
    for e in events:
        by.setdefault((e.get("match") or {}).get("key") or e.get("key"), []).append(e)
    keep, sup = [], []
    for k, evs in by.items():
        core = [e for e in evs if e.get("type") != "tg_moment"]
        if len(core) <= 1:
            keep += evs; continue
        top = max(core, key=_evt_pos)
        tn = _evt_pos(top)
        for e in evs:
            if e is top or e.get("type") == "tg_moment":
                keep.append(e)
            elif e.get("type") == "match_started" and tn != (99, 3):
                keep.append(e)                       # series opener stays (shown first, gap before the rest)
            else:
                sup.append(e)
    return keep, sup


def live_gap_wait(chat=None, gap=LIVE_GAP_SEC, sent_log=None, sleep=True):
    """Seconds to wait so the next live post is >= gap after the previous channel post (sent_log.json).
    sleep=True actually waits (capped at gap)."""
    sent_log = sent_log or os.path.join(BASE_DIR, "sent_log.json")
    try:
        log = json.load(open(sent_log))
        last = max(float(r.get("ts") or 0) for r in log if chat is None or str(r.get("chat")) == str(chat))
    except Exception:
        return 0
    w = max(0, min(gap, last + gap - time.time()))
    if sleep and w > 0:
        time.sleep(w)
    return w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("key", nargs="?")
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="don't record events in state.json")
    ap.add_argument("--late", action="store_true", help="also use demo-parsed rounds (2-3 h after map end)")
    ap.add_argument("--x-file", action="append", default=[])
    ap.add_argument("--no-map-start", action="store_true")
    ap.add_argument("--no-lp", action="store_true", help="skip Liquipedia half-time/map scores")
    ap.add_argument("--listen", action="store_true")
    ap.add_argument("--minutes", type=int, default=330)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--gate", action="store_true", help="routine start: IDLE/BUSY/ACTIVE (+ lease)")
    ap.add_argument("--release", action="store_true", help="routine end: drop the lease")
    ap.add_argument("--force-gate", action="store_true", help="ignore a fresh lease (only after checking it's dead)")
    ap.add_argument("--sim-schedule", help="test: schedule file for --gate")
    ap.add_argument("--ignore-window", action="store_true", help="test: --gate outside 10-24 MSK")
    ap.add_argument("--wait-gap", action="store_true", help="sleep until >= LIVE_GAP_SEC (75 s) since last channel post")
    a = ap.parse_args()
    if a.wait_gap:
        print("waited %.0fs" % live_gap_wait()); return
    if a.gate:
        return gate(a)
    if a.release:
        return release()
    _lock = None if (a.listen or a.list or (a.key or "").startswith("cs:")) else take_flock()
    st = load_state()
    le = st.setdefault("live_events", {})
    if a.list:
        for k, v in le.items():
            print(k, len(v.get("seen", [])), "events, updated", v.get("updated"), v.get("live_score"))
        return
    if a.key and a.key.startswith("cs:"):   # media/streamer league (events.json, cybersport.ru) - read-only
        import media_league
        items, st_, row = media_league.poll(a.key)
        seen = set(load_state().get("live_events", {}).get(a.key, {}).get("seen", []))
        items = [i for i in items if i["event_id"] not in seen]
        print(json.dumps({"key": a.key, "status": st_, "match": row, "new_events": items}, ensure_ascii=False, indent=2))
        return
    if not a.key or not a.key.startswith("bo3:"):
        raise SystemExit("key must be bo3:<match id>")
    mid = int(a.key.split(":")[1])
    if a.reset:
        le.pop(a.key, None)
        save_state(st)
        print("reset", a.key)
        return
    if a.listen:
        return listen(mid, a.minutes)

    # make sure the minute collector runs while a match is live (it exits by itself when nothing is live)
    if os.environ.get("NO_COLLECTOR") != "1":
        try:
            subprocess.run(["bash", os.path.join(BASE_DIR, "ensure_collector.sh")], capture_output=True, timeout=60)
        except Exception:
            pass
    # backup sender for spaced news posts (news_queue.py): publish anything due (no-op when queue is empty)
    if os.environ.get("NO_NEWS_QUEUE") != "1":
        try:
            subprocess.run([sys.executable, os.path.join(BASE_DIR, "news_queue.py"), "--send-due"],
                           capture_output=True, timeout=240)
        except Exception:
            pass
    rs = le.get(a.key) or {"seen": []}
    rs = json.loads(json.dumps(rs))
    ctx = Ctx(fetch_match(mid))
    events, notes = [], []
    events += rest_events(ctx)
    msgs = read_buffer(mid, rs)
    snap = snapshot(mid) if ctx.m.get("status") == "current" else None
    if snap:
        msgs.append(snap)
    if msgs:
        events += feed_events(ctx, msgs, rs)
    if ctx.m.get("status") == "current" and not msgs:
        notes.append(f"no bo3 live feed (parsed_status={ctx.m.get('parsed_status')}, "
                     f"live_coverage={ctx.m.get('live_coverage')}): pistols/aces/live score unavailable; "
                     f"use --x-file or --late")
    if ctx.m.get("status") == "current" and not a.no_lp:
        try:
            lpe, lpi = lp_events(ctx, rs)
            have = {e["id"] for e in events}
            events += [e for e in lpe if e["id"] not in have]   # bo3 map_finished wins if both present
            if lpi:
                rs["live_score"] = lpi["live"]
                rs["lp_rev"] = lpi["rev_ts"]
        except Exception as ex:
            notes.append(f"Liquipedia error: {ex!r}"[:200])
    if a.late:
        events += late_events(ctx, rs)
    if a.x_file:
        events += x_events(ctx, load_x(a.x_file), team_handles(ctx))
    if a.no_map_start:
        events = [e for e in events if e["type"] != "map_started"]
    seen = set(rs.get("seen", []))
    new, ids = [], set()
    for e in events:
        if e["id"] in seen or e["id"] in ids:
            continue
        ids.add(e["id"])
        new.append(e)
    try:
        qp = subprocess.run([sys.executable, os.path.join(BASE_DIR, "live_queue.py"), "--pending", "--brief"],
                            capture_output=True, text=True, timeout=30).stdout.strip()
        qlines = [l for l in qp.splitlines() if l.strip().startswith("-") and "no pending" not in l.lower()]
        if qlines:
            notes.append(f"collector queue has {len(qlines)} pending pre-detected events: run "
                         f"`python3 live_queue.py --pending --brief`, handle them per README 'Live collector', "
                         f"then `live_queue.py --mark posted|skipped ID`")
    except Exception:
        pass
    status = {"key": a.key, "status": ctx.m.get("status"), "parsed_status": ctx.m.get("parsed_status"),
              "series": f"{ctx.t1} {ctx.m.get('team1_score')}:{ctx.m.get('team2_score')} {ctx.t2}",
              "live_score": rs.get("live_score"), "lp_rev": rs.get("lp_rev"), "notes": notes}
    if not a.dry_run:
        rs["seen"] = (rs.get("seen", []) + [e["id"] for e in new])[-MAX_SEEN:]
        rs["updated"] = now_msk().isoformat(timespec="seconds")
        rs["match_status"] = ctx.m.get("status")
        le[a.key] = rs
        # prune finished matches older than 3 days
        cutoff = (now_msk() - timedelta(days=3)).isoformat()
        for k in [k for k, v in le.items() if v.get("updated", "") < cutoff]:
            le.pop(k)
        save_state(st)
    if a.brief:
        print(f"# {status['series']} [{status['status']}/{status['parsed_status']}] live={status['live_score']}")
        for n in notes:
            print("# note:", n)
        for e in new:
            print("-", brief(e))
        if not new:
            print("- (no new events)")
    else:
        print(json.dumps({"match": status, "events": new}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
