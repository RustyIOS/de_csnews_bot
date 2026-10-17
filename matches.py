#!/usr/bin/env python3
"""CS2 pro matches (today's schedule / recent results) as JSON, for announcements & results posts.

Primary source: bo3.gg public JSON API (api.bo3.gg, no key) - has event tier (S/A/B/C/D),
team world ranks, BO format, per-map scores. Fallback: Liquipedia (MediaWiki API, action=parse
of Liquipedia:Matches, max 1 parse request / 30 s per their API ToS) - no tiers, no map
scores, filtered by a top-team list instead.

Usage:
  python3 matches.py --today [--date 2026-10-11] [--all] [--unposted]
  python3 matches.py --results [--hours 24] [--all] [--unposted]
  python3 matches.py --tournaments [--days 14] [--include-current] [--brief]   # upcoming big events
  python3 matches.py --mark announced|result|tournament KEY [KEY ...]   # record a posted announcement/result
  python3 matches.py --unmark announced|result KEY [KEY ...]
  options: --source auto|bo3|liquipedia   --max-rank 30   --brief (human-readable)

"Notable" filter (unless --all): tier S/A event, OR B-tier with a team ranked <= 20,
OR both teams ranked <= --max-rank. TBD-vs-TBD slots are kept only for S/A events.
Times are Moscow (MSK). Each match has a stable "key" used for state dedupe
(state.json -> "match_announcements" / "match_results"); "announced"/"result_posted"
flags show what is already recorded. This script never posts anything.
"""
import argparse, json, math, os, re, sys, time, shutil, subprocess, urllib.parse
from datetime import datetime, timedelta, timezone
from common import UA, MSK, BASE, load_json, save_json, load_state, save_state, now_msk, similar

BO3_API = "https://api.bo3.gg/api/v1/matches"
LP_API = "https://liquipedia.net/counterstrike/api.php"
TEAMS_CACHE = os.path.join(BASE, "teams_cache.json")   # bo3 top-team names, used by the LP fallback
STATE_KEYS = {"announced": "match_announcements", "result": "match_results", "tournament": "tournament_announcements"}
BO3_TOURN = "https://api.bo3.gg/api/v1/tournaments"
MONTHS_EN = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
TIER_LABEL = {"s": "S", "a": "A", "b": "B", "c": "C", "d": "D"}
# fallback list if teams_cache.json is missing (Liquipedia names/short names)
DEFAULT_TOP = ["Vitality", "MOUZ", "Spirit", "Team Spirit", "Natus Vincere", "NAVI", "FaZe", "G2", "Falcons",
               "Aurora", "FURIA", "The MongolZ", "MongolZ", "Legacy", "PARIVISION", "3DMAX", "Astralis",
               "Liquid", "Team Liquid", "HEROIC", "BetBoom", "Virtus.pro", "GamerLegion", "paiN", "M80",
               "B8", "NIP", "Ninjas in Pyjamas", "TYLOO", "Lynn Vision", "9z", "1win", "FlyQuest", "Complexity",
               "BIG", "Eternal Fire", "Monte", "Imperial", "MIBR", "Fnatic", "ENCE", "Gentle Mates", "Passion UA"]
TOP_EVENTS = re.compile(r"major|iem|esl pro league|blast|pgl|thunderpick world|starladder|cct global finals|"
                        r"perfect world|fissure|esports world cup|betboom dacha|austin|cologne", re.I)


def get(url, params=None, timeout=25, gzip=True):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, safe="[],")
    cmd = ["curl", "-g", "-sL", "--max-time", str(timeout), "-A", UA, "-w", "\n%{http_code}", url]
    if gzip:
        cmd.insert(1, "--compressed")
    p = subprocess.run(cmd, capture_output=True)
    body, _, code = p.stdout.rpartition(b"\n")
    if not code.strip().isdigit() or int(code) != 200:
        raise RuntimeError(f"HTTP {code.decode(errors='replace').strip() or 'error'} for {url.split('?')[0]}")
    return json.loads(body)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


# ---------------- bo3.gg ----------------
def bo3_pages(status, sort, stop, tiers=None, max_pages=6):
    """Yield matches page by page; stop(m) -> True ends pagination (list is sorted)."""
    for page in range(max_pages):
        params = {"page[limit]": 100, "page[offset]": page * 100, "sort": sort,
                  "filter[matches.status][in]": status, "filter[matches.discipline_id][eq]": 1,
                  "with": "teams,tournament,stage,round,games"}
        if tiers:
            params["filter[matches.tier][in]"] = tiers
        d = get(BO3_API, params)
        res = d.get("results") or []
        for m in res:
            if stop(m):
                return
            yield m
        if len(res) < 100:
            return
        time.sleep(1)


def bo3_norm(m):
    t1, t2 = m.get("team1") or {}, m.get("team2") or {}
    start = parse_ts(m.get("start_date"))
    end = parse_ts(m.get("end_date"))
    bo = m.get("bo_type")
    tour = m.get("tournament") or {}
    rnd = (m.get("round") or {}).get("name")
    stage = (m.get("stage") or {}).get("title")
    if stage and tour.get("name") and stage.startswith(tour["name"]):
        stage = stage[len(tour["name"]):].strip(" -:") or None
    maps = []
    for g in sorted(m.get("games") or [], key=lambda g: g.get("number") or 0):
        if g.get("status") != "finished" or not g.get("map_name"):
            continue
        wid = (g.get("winner_team_clan") or {}).get("team_id")
        w, l = g.get("winner_clan_score"), g.get("loser_clan_score")
        t1won = wid == m.get("team1_id")
        maps.append({"map": g["map_name"].replace("de_", "").capitalize(),
                     "score": f"{w}:{l}" if t1won else f"{l}:{w}",
                     "winner": (t1 if t1won else t2).get("name") if wid else None})
    s1, s2 = m.get("team1_score"), m.get("team2_score")
    winner = None
    if m.get("winner_team_id"):
        winner = t1.get("name") if m["winner_team_id"] == m.get("team1_id") else t2.get("name")
    out = {
        "key": f"bo3:{m['id']}",
        "source": "bo3.gg",
        "url": f"https://bo3.gg/matches/{m.get('slug')}",
        "status": m.get("status"),                     # upcoming / current (live) / finished / defwin...
        "team1": t1.get("name") or "TBD", "team2": t2.get("name") or "TBD",
        "team1_rank": t1.get("rank"), "team2_rank": t2.get("rank"),
        "team1_logo": t1.get("image_url"), "team2_logo": t2.get("image_url"),
        "start_msk": start.astimezone(MSK).strftime("%H:%M") if start else None,
        "date_msk": start.astimezone(MSK).strftime("%Y-%m-%d") if start else None,
        "start_utc": start.isoformat() if start else None,
        "end_utc": end.isoformat() if end else None,
        "event": tour.get("name"),
        "event_logo": tour.get("image_url"),
        "stage": " / ".join(x for x in (stage, rnd) if x) or None,
        "format": f"BO{bo}" if bo else None,
        "tier": TIER_LABEL.get(m.get("tier"), m.get("tier")),
        "stars": m.get("stars"),
        "importance": None,
    }
    if m.get("status") in ("finished", "current", "defwin") or s1 or s2:
        out.update({"score": f"{s1}:{s2}", "winner": winner, "maps": maps})
        need = math.ceil((bo or 1) / 2)
        if m.get("status") == "finished" and max(s1 or 0, s2 or 0) < need:
            out["note"] = "score incomplete/forfeit (winner below majority)"
        if m.get("status") == "defwin":
            out["note"] = "technical win / forfeit"
    return out


def importance(x, max_rank):
    tier = (x.get("tier") or "").upper()
    ranks = [r for r in (x.get("team1_rank"), x.get("team2_rank")) if r]
    best = min(ranks) if ranks else 999
    if tier in ("S", "A"):
        lvl = "high" if tier == "S" else "medium"
        if tier == "A" and best <= 10:
            lvl = "high"
        return lvl
    if tier == "B" and best <= 20:
        return "medium" if best <= 10 else "low"
    if len(ranks) == 2 and max(ranks) <= max_rank:
        return "medium" if max(ranks) <= 15 else "low"
    return None


def cache_top_teams(matches, max_rank):
    names = {}
    for m in matches:
        for t in (m.get("team1") or {}, m.get("team2") or {}):
            if t.get("name") and t.get("rank") and t["rank"] <= max_rank:
                names[t["name"]] = t["rank"]
    if names:
        old = load_json(TEAMS_CACHE, {}).get("teams", {})
        old.update(names)
        save_json(TEAMS_CACHE, {"updated": now_msk().isoformat(timespec="seconds"), "teams": old})


def bo3_today(day, max_rank):
    start = datetime.combine(day, datetime.min.time(), MSK)
    end = start + timedelta(days=1)
    raw = []
    # finished earlier today (newest first, stop when before day start)
    raw += list(bo3_pages("finished,defwin", "-start_date", lambda m: parse_ts(m["start_date"]) < start))
    raw += list(bo3_pages("upcoming,current", "start_date", lambda m: parse_ts(m["start_date"]) >= end))
    raw = [m for m in raw if m.get("start_date") and start <= parse_ts(m["start_date"]) < end]
    cache_top_teams(raw, max_rank)
    return sorted((bo3_norm(m) for m in raw), key=lambda x: x["start_utc"])


def bo3_results(hours, max_rank):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    # end_date can lag start_date by hours; scan a bit further back by start_date
    raw = list(bo3_pages("finished,defwin", "-start_date",
                         lambda m: parse_ts(m["start_date"]) < cutoff - timedelta(hours=8), max_pages=10))
    cache_top_teams(raw, max_rank)
    out = []
    for m in raw:
        t = parse_ts(m.get("end_date")) or parse_ts(m.get("start_date"))
        if t >= cutoff:
            out.append(bo3_norm(m))
    return sorted(out, key=lambda x: x["end_utc"] or x["start_utc"], reverse=True)


# ---------------- Liquipedia fallback ----------------
def lp_matches():
    from bs4 import BeautifulSoup
    d = get(LP_API, {"action": "parse", "page": "Liquipedia:Matches", "format": "json", "prop": "text"})
    soup = BeautifulSoup(d["parse"]["text"]["*"], "html.parser")
    out = []
    for blk in soup.select("div.match-info"):
        ts = blk.select_one("[data-timestamp]")
        if not ts:
            continue
        start = datetime.fromtimestamp(int(ts["data-timestamp"]), timezone.utc)
        names = [n.get_text(strip=True) for n in blk.select(".match-info-header-opponent .name")]
        full = [ (n.find("a") or {}).get("title") if n.find("a") else None for n in blk.select(".match-info-header-opponent .name")]
        lower = blk.select_one(".match-info-header-scoreholder-lower")
        upper = blk.select_one(".match-info-header-scoreholder-upper")
        fmt = re.sub(r"[()]", "", lower.get_text(strip=True)).upper() if lower else None
        ev = blk.select_one(".match-info-tournament-name")
        finished = bool(blk.select_one("[data-finished]"))
        x = {"key": f"lp:{int(start.timestamp())}:{'-'.join(names)}", "source": "Liquipedia",
             "url": None, "status": "finished" if finished else "upcoming",
             "team1": names[0] if names else "TBD", "team2": names[1] if len(names) > 1 else "TBD",
             "team1_full": full[0] if full else None, "team2_full": full[1] if len(full) > 1 else None,
             "team1_rank": None, "team2_rank": None,
             "start_msk": start.astimezone(MSK).strftime("%H:%M"),
             "date_msk": start.astimezone(MSK).strftime("%Y-%m-%d"),
             "start_utc": start.isoformat(), "end_utc": None,
             "event": ev.get_text(strip=True) if ev else None, "stage": None,
             "format": fmt, "tier": None, "stars": None, "importance": None}
        if finished and upper:
            sc = re.sub(r"\s", "", upper.get_text())
            x.update({"score": sc, "maps": []})
            try:
                a, b = map(int, sc.split(":"))
                x["winner"] = x["team1"] if a > b else x["team2"] if b > a else None
            except ValueError:
                x["winner"] = None
        out.append(x)
    return out


def lp_importance(x, top):
    hit = sum(1 for k in ("team1", "team2", "team1_full", "team2_full") if x.get(k) in top)
    if TOP_EVENTS.search(x.get("event") or ""):
        return "high" if hit else "medium"
    return "medium" if hit >= 2 else None


# ---------------- tournaments ----------------
def big_event(name, tier):
    return tier == "s" or (tier == "a" and bool(TOP_EVENTS.search(name or "")))


def bo3_tournaments(days, include_current):
    now = datetime.now(timezone.utc)
    until = now + timedelta(days=days)
    out = []
    for page in range(3):
        d = get(BO3_TOURN, {"page[limit]": 50, "page[offset]": page * 50, "sort": "start_date",
                            "filter[tournaments.status][in]": "upcoming,current" if include_current else "upcoming",
                            "filter[tournaments.discipline_id][eq]": 1, "filter[tournaments.tier][in]": "s,a"})
        res = d.get("results") or []
        stop = False
        for t in res:
            st = parse_ts(t.get("start_date"))
            if st and st > until:
                stop = True; break
            if not big_event(t.get("name"), t.get("tier")):
                continue
            if t.get("status") == "upcoming" and st and st < now - timedelta(days=1):
                continue
            out.append(t)
        if stop or len(res) < 50:
            break
    rows = []
    for t in out:
        det = {}
        try:
            det = get(f"{BO3_TOURN}/{t['slug']}")
            time.sleep(0.5)
        except Exception:
            pass
        teams = det.get("teams") or []
        st, en = parse_ts(t.get("start_date")), parse_ts(t.get("end_date"))
        rows.append({
            "key": f"tour:bo3:{t['id']}", "source": "bo3.gg", "name": t["name"],
            "url": f"https://bo3.gg/tournaments/{t['slug']}",
            "tier": TIER_LABEL.get(t.get("tier"), t.get("tier")), "status": t.get("status"),
            "start_date": st.astimezone(MSK).strftime("%Y-%m-%d") if st else None,
            "end_date": en.astimezone(MSK).strftime("%Y-%m-%d") if en else None,
            "start_utc": st.isoformat() if st else None,
            "days_until": round((st - datetime.now(timezone.utc)).total_seconds() / 86400, 1) if st else None,
            "prize": t.get("prize"), "event_type": det.get("event_type") or t.get("event_type"),
            "country": (det.get("country") or {}).get("name"), "city": (det.get("city") or {}).get("name"),
            "venue": (det.get("venue") or {}).get("name") if isinstance(det.get("venue"), dict) else None,
            "teams_count_bo3": len(teams) or None,
            "teams_known": [x.get("name") for x in sorted(teams, key=lambda x: x.get("rank") or 999)][:32],
            "logo": t.get("image_url"),
        })
    return rows


def lp_parse_date(txt, year_hint):
    """'Nov 02–08, 2026' | 'Nov 25 – Dec 13, 2026' | 'Oct 13–18' -> (date, date)."""
    txt = txt.replace("\u2013", "–").replace("-", "–")
    m = re.match(r"(\w{3})\s+(\d{1,2})(?:\s*–\s*(?:(\w{3})\s+)?(\d{1,2}))?(?:,\s*(\d{4}))?", txt)
    if not m or m.group(1).lower()[:3] not in MONTHS_EN:
        return None, None
    y = int(m.group(5) or year_hint)
    m1 = MONTHS_EN[m.group(1).lower()[:3]]
    m2 = MONTHS_EN.get((m.group(3) or m.group(1)).lower()[:3], m1)
    try:
        a = datetime(y, m1, int(m.group(2))).date()
        b = datetime(y + (1 if m2 < m1 else 0), m2, int(m.group(4) or m.group(2))).date()
    except ValueError:
        return None, None
    return a, b


def lp_tournaments():
    from bs4 import BeautifulSoup
    cache = os.path.join(BASE, "lp_tournaments_cache.json")   # Liquipedia ToS: parse requests are rate-limited
    c = load_json(cache, {})
    if c.get("ts") and time.time() - c["ts"] < 6 * 3600 and c.get("data"):
        d = c["data"]
    else:
        d = get(LP_API, {"action": "parse", "page": "S-Tier_Tournaments", "format": "json", "prop": "text"})
        save_json(cache, {"ts": time.time(), "data": d})
    soup = BeautifulSoup(d["parse"]["text"]["*"], "html.parser")
    out = []
    for tb in soup.select("table.table2__table"):
        h = tb.find_previous(class_="mw-heading")
        yh = re.search(r"(20\d\d)", h.get_text() if h else "")
        yh = int(yh.group(1)) if yh else now_msk().year
        for r in tb.select("tr.table2__row--body"):
            cells = r.find_all("td")
            ti = next((i for i, c in enumerate(cells) if "column__tournament" in (c.get("class") or [])), None)
            if ti is None or len(cells) < ti + 4:
                continue
            txt = [c.get_text(" ", strip=True) for c in cells]
            a_, b_ = lp_parse_date(txt[ti + 1], yh)
            link = cells[ti].select_one("a[title]")
            prize = re.sub(r"[^\d]", "", txt[ti + 2])
            teams = txt[ti + 4] if len(txt) > ti + 4 else ""
            rest = " ".join(txt[ti + 5:])
            out.append({"name": txt[ti], "start": a_, "end": b_,
                        "prize": int(prize) if prize else None,
                        "location": txt[ti + 3] if txt[ti + 3] not in ("", "TBA") else None,
                        "teams": int(teams) if teams.isdigit() else None,
                        "cancelled": "Cancelled" in rest or "Cancelled" in teams,
                        "url": "https://liquipedia.net" + link["href"] if link and link.get("href", "").startswith("/") else None})
    uniq = {}
    for t in out:            # the page can list an event twice (e.g. ongoing + yearly table)
        uniq.setdefault(t["name"], t)
    return list(uniq.values())


def _tn(n):
    n = (n or "").lower().replace("intel extreme masters", "iem")
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def tournaments(days, include_current, source):
    errors, rows, lp = [], None, None
    if source in ("auto", "bo3"):
        try:
            rows = bo3_tournaments(days, include_current)
        except Exception as e:
            errors.append(f"bo3.gg: {e}")
    if source in ("auto", "liquipedia"):
        try:
            lp = lp_tournaments()
        except Exception as e:
            errors.append(f"Liquipedia: {e}")
    today = now_msk().date()
    def lp_rows(exclude):
        out = []
        for t in lp:
            if t["name"] in exclude:
                continue
            if not t["start"] or t["cancelled"]:
                continue
            if not (today <= t["start"] <= today + timedelta(days=days) or (include_current and t["start"] <= today <= (t["end"] or t["start"]))):
                continue
            out.append({"key": f"tour:lp:{_tn(t['name']).replace(' ', '-')}", "source": "Liquipedia", "name": t["name"],
                         "url": t["url"], "tier": "S", "status": "current" if t["start"] <= today else "upcoming",
                         "start_date": t["start"].isoformat(), "end_date": (t["end"] or t["start"]).isoformat(),
                         "days_until": (t["start"] - today).days, "prize": t["prize"], "location": t["location"],
                         "teams_count": t["teams"], "liquipedia_url": t["url"]})
        return out

    if rows is None and lp is not None:      # Liquipedia-only fallback
        rows = sorted(lp_rows(set()), key=lambda r: r["start_date"])
        return rows, "Liquipedia", errors
    if rows is None:
        return None, None, errors
    matched = set()
    for r in rows:   # enrich bo3 rows with Liquipedia teams count / location
        m = None
        if lp:
            a_ = _tn(r["name"])
            rs = datetime.fromisoformat(r["start_date"]).date() if r.get("start_date") else None
            m = next((t for t in lp if _tn(t["name"]) == a_), None)
            if not m and rs:   # fuzzy name match only together with near-identical start dates
                for t in lp:
                    b_ = _tn(t["name"])
                    if t["start"] and abs((t["start"] - rs).days) <= 3 and (
                            similar(a_, b_, 0.75) or (set(a_.split()) & set(b_.split())) - {"2026", "2027", "cs", "the", "season"}):
                        m = t; break
        if m:
            matched.add(m["name"])
        r["teams_count_lp"] = m["teams"] if m else None
        r["liquipedia_url"] = m["url"] if m else None
        r["teams_count"] = r["teams_count_lp"] or r["teams_count_bo3"]
        loc = ", ".join(x for x in dict.fromkeys([r.get("city"), r.get("country")]) if x)
        if not loc and m and m.get("location"):
            loc = m["location"]
        r["location"] = (loc or None) if r.get("event_type") != "online" else "Online"
        if m and m.get("cancelled"):
            r["note"] = "Liquipedia marks this event as cancelled"
    if lp:   # Liquipedia S-tier events that bo3 doesn't rate S/A (e.g. BLAST Open, StarSeries)
        rows += lp_rows(matched)
        rows.sort(key=lambda r: r["start_date"] or "")
    return rows, "bo3.gg" + (" + Liquipedia" if lp else ""), errors


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--today", action="store_true")
    g.add_argument("--results", action="store_true")
    g.add_argument("--tournaments", action="store_true", help="upcoming S-tier / big events")
    ap.add_argument("--days", type=float, default=14, help="--tournaments: starting within N days")
    ap.add_argument("--include-current", action="store_true", help="--tournaments: also list running events")
    g.add_argument("--mark", nargs="+", metavar=("KIND", "KEY"))
    g.add_argument("--unmark", nargs="+", metavar=("KIND", "KEY"))
    ap.add_argument("--date", help="YYYY-MM-DD (MSK) for --today; default today")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--all", action="store_true", help="no notability filter")
    ap.add_argument("--unposted", action="store_true", help="hide matches already announced/reported")
    ap.add_argument("--max-rank", type=int, default=30)
    ap.add_argument("--source", choices=["auto", "bo3", "liquipedia"], default="auto")
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="apply events.json live_tiers for collector/gate (excludes media + demote_events when live_tiers=top)")
    a = ap.parse_args()

    if a.mark or a.unmark:
        args = a.mark or a.unmark
        kind = args[0]
        if kind not in STATE_KEYS or len(args) < 2:
            sys.exit("usage: --mark|--unmark announced|result|tournament KEY [KEY ...]")
        st = load_state(); sec = st.setdefault(STATE_KEYS[kind], {})
        for k in args[1:]:
            if a.mark:
                sec[k] = now_msk().isoformat(timespec="seconds")
            else:
                sec.pop(k, None)
        if len(sec) > 2000:
            st[STATE_KEYS[kind]] = dict(sorted(sec.items(), key=lambda kv: kv[1])[-1500:])
        save_state(st)
        print(json.dumps({"ok": True, STATE_KEYS[kind]: args[1:], "action": "mark" if a.mark else "unmark"}))
        return

    if a.tournaments:
        rows, used, errors = tournaments(a.days, a.include_current, a.source)
        if rows is None:
            print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2)); sys.exit(1)
        done = load_state().get("tournament_announcements", {})
        for r in rows:
            r["announced"] = r["key"] in done
        if a.unposted:
            rows = [r for r in rows if not r["announced"]]
        if a.brief:
            print(f"# source={used} big events starting within {a.days:g} days: {len(rows)}" + (f"  (errors: {errors})" if errors else ""))
            for r in rows:
                pz = f"${r['prize']:,}" if r.get("prize") else "prize ?"
                print(f"{r['start_date']}..{r['end_date']} [{r.get('tier')}|{r.get('status')}] {r['name']} - {pz}, "
                      f"teams {r.get('teams_count') or '?'}, {r.get('location') or 'location ?'} ({r.get('event_type') or '-'})  {r['key']}")
            return
        print(json.dumps({"ok": True, "source": used, "errors": errors, "mode": "tournaments", "days": a.days,
                          "generated_msk": now_msk().isoformat(timespec="seconds"), "count": len(rows),
                          "tournaments": rows}, ensure_ascii=False, indent=2))
        return

    day = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else now_msk().date()
    errors, used, matches = [], None, None
    if a.source in ("auto", "bo3"):
        try:
            matches = bo3_today(day, a.max_rank) if a.today else bo3_results(a.hours, a.max_rank)
            used = "bo3.gg"
            for x in matches:
                x["importance"] = importance(x, a.max_rank)
                if x["team1"] == "TBD" and x["team2"] == "TBD" and x.get("tier") not in ("S", "A"):
                    x["importance"] = None
        except Exception as e:
            errors.append(f"bo3.gg: {e}")
    if matches is None and a.source in ("auto", "liquipedia"):
        try:
            allm = lp_matches()
            used = "Liquipedia"
            top = set(DEFAULT_TOP) | set(load_json(TEAMS_CACHE, {}).get("teams", {}))
            if a.today:
                matches = [x for x in allm if x["date_msk"] == day.isoformat()]
            else:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=a.hours)
                matches = [x for x in allm if x["status"] == "finished" and parse_ts(x["start_utc"]) >= cutoff - timedelta(hours=4)]
            for x in matches:
                x["importance"] = lp_importance(x, top)
        except Exception as e:
            errors.append(f"Liquipedia: {e}")
    if matches is None:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
        sys.exit(1)

    # ---- event rules (events.json): demote B-tier events, add media/streamer leagues ----
    ev_cfg = load_json(os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.json"), {})
    live_top = a.live and (ev_cfg.get("live_tiers") or "all") == "top"
    for x in matches:
        for rule in ev_cfg.get("demote_events", []):
            if (x.get("tier") or "").upper() in ("S", "A"):
                break   # top-tier never demoted
            if re.search(rule["match"], x.get("event") or "", re.I):
                if live_top:
                    # live_tiers=top: demote_events hard-excluded from live (even with top-10 teams)
                    x["importance"] = None
                    x["demoted"] = rule["match"]
                    x["live_excluded"] = "live_tiers=top"
                else:
                    rk = [r for r in (x.get("team1_rank"), x.get("team2_rank")) if r]
                    if not rk or min(rk) > rule.get("min_rank", 20):
                        x["importance"] = None
                        x["demoted"] = rule["match"]
    # Media/streamer leagues: skip for --live when live_tiers=top (daily announce/results omit --live)
    if a.source == "auto" and ev_cfg.get("notable_events") and not live_top:
        try:
            import media_league
            mrows, merr = media_league.rows(day.isoformat())
            errors += merr
            if a.results and not a.date:
                # Rolling window only when the caller did not pin a Moscow day.
                # A finished streamer match with no start time stays in; one that
                # clearly started before the window is left out.
                cutoff = datetime.now(timezone.utc) - timedelta(hours=a.hours)
                kept = []
                for x in mrows:
                    if x.get("status") not in ("finished", "defwin"):
                        continue
                    start = parse_ts(x["start_utc"]) if x.get("start_utc") else None
                    if start and start < cutoff - timedelta(hours=4):
                        continue
                    kept.append(x)
                mrows = kept
            elif a.results:
                mrows = [x for x in mrows if x.get("status") in ("finished", "defwin")]
            matches += mrows
        except Exception as e:
            errors.append(f"media_league: {e}")

    total = len(matches)
    if not a.all:
        matches = [x for x in matches if x["importance"]]
    if a.results:
        matches = [x for x in matches if x.get("status") in ("finished", "defwin")]
    st = load_state()
    ann, res = st.get("match_announcements", {}), st.get("match_results", {})
    for x in matches:
        x["announced"] = x["key"] in ann
        x["result_posted"] = x["key"] in res
    if a.unposted:
        matches = [x for x in matches if not (x["result_posted"] if a.results else x["announced"])]

    if a.brief:
        print(f"# source={used} {'today ' + day.isoformat() if a.today else f'results last {a.hours:g}h'}: "
              f"{len(matches)} notable of {total}" + (f"  (errors: {errors})" if errors else ""))
        for x in matches:
            sc = f" {x['score']}" if x.get("score") else " vs"
            mp = ", ".join(f"{m['map']} {m['score']}" for m in x.get("maps") or [])
            print(f"{x['date_msk']} {x['start_msk']} MSK [{x.get('tier') or '-'}|{x['importance']}] "
                  f"{x['team1']}{sc} {x['team2']} ({x['format']}) - {x['event']}"
                  f"{' - ' + x['stage'] if x.get('stage') else ''} [{x['status']}]{' ' + mp if mp else ''}  {x['key']}")
        return
    print(json.dumps({"ok": True, "source": used, "errors": errors,
                      "mode": "today" if a.today else "results",
                      "date_msk": day.isoformat() if a.today else None,
                      "hours": a.hours if a.results else None,
                      "generated_msk": now_msk().isoformat(timespec="seconds"),
                      "total_before_filter": total, "count": len(matches), "matches": matches},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
