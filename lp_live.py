"""Liquipedia live half-time / map scores for a bo3.gg match (wikitext of the tournament page, edited live by LP editors).

Liquipedia API terms: identifying UA, gzip, <= 1 request / 2 s, action=query (not parse). We do 1-3 requests per call.
Map wikitext:  |map2={{Map|map=Inferno|finished=|t1firstside=ct|t1t=|t1ct=|t2t=|t2ct=|...}}
  -> half-time when the two first-half values are filled and sum to 12; final when finished=true.
"""
import json, re, subprocess, time, urllib.parse
from datetime import datetime, timedelta, timezone
from common import UA

LP_API = "https://liquipedia.net/counterstrike/api.php"
TZ = {"CEST": 2, "CET": 1, "UTC": 0, "GMT": 0, "BST": 1, "EEST": 3, "EET": 2, "MSK": 3, "EDT": -4, "EST": -5,
      "CDT": -5, "CST": -6, "PDT": -7, "PST": -8, "SGT": 8, "CST(China)": 8, "KST": 9, "JST": 9, "AEST": 10, "AEDT": 11, "BRT": -3}
_last = [0.0]


def lp_get(params):
    wait = 2.1 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    url = LP_API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    p = subprocess.run(["curl", "-sL", "--compressed", "--max-time", "20", "-A", UA, url], capture_output=True)
    return json.loads(p.stdout)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("team ", ""))


# LP team templates are often short codes (TeamOpponent|nip, |ic, |apogee) -> map to bo3 names (normalized).
LP_ALIASES = {"nip": ["ninjasinpyjamas", "nip"], "ic": ["innercircle"], "apogee": ["betclic", "betclicapogee", "betclicapogeeesports"],
              "navi": ["natusvincere"], "vp": ["virtuspro"], "mouz": ["mousesports"], "liquid": ["teamliquid"]}


def acronym(s):
    return "".join(w[0] for w in re.findall(r"[a-z0-9]+", (s or "").lower().replace("team ", "")) if w)


def team_match(tpl, name):
    a, b = norm(tpl), norm(name)
    if not (a and b):
        return False
    if a == b or b.startswith(a) or a.startswith(b) or a in b or b in a:
        return True
    if b in LP_ALIASES.get(a, []) or any(b.startswith(x) or x.startswith(b) for x in LP_ALIASES.get(a, [])):
        return True
    return len(a) >= 2 and acronym(name) == a  # "Ninjas in Pyjamas" -> nip, "Inner Circle" -> ic


def parse_date(s):
    m = re.search(r"(\w+ \d{1,2}, \d{4}) - (\d{1,2}:\d{2})\s*(?:\{\{Abbr/(\w+))?", s or "")
    if not m:
        return None
    dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%B %d, %Y %H:%M")
    return (dt - timedelta(hours=TZ.get(m.group(3) or "UTC", 0))).replace(tzinfo=timezone.utc)


def split_matches(text):
    """Yield (start, body) of every {{Match ...}} block (brace-balanced)."""
    for m in re.finditer(r"\{\{Match\b", text):
        i, depth = m.start(), 0
        j = i
        while j < len(text):
            if text.startswith("{{", j):
                depth += 1; j += 2; continue
            if text.startswith("}}", j):
                depth -= 1; j += 2
                if depth == 0:
                    break
                continue
            j += 1
        yield text[i:j]


def parse_match(body):
    g = lambda k: (re.search(r"\|" + k + r"=([^|}\n]*)", body) or [None, ""])[1].strip()
    opp = re.findall(r"\|opponent([12])=\{\{TeamOpponent\|([^|}]*)", body)
    maps = []
    for n, mb in re.findall(r"\|map(\d)=\{\{Map\|([^}]*)\}\}", body):
        f = dict(re.findall(r"(\w+)=([^|]*)", mb))
        f = {k: v.strip() for k, v in f.items()}
        maps.append({"n": int(n), **f})
    return {"t1": dict(opp).get("1"), "t2": dict(opp).get("2"), "date": parse_date(g("date")),
            "finished": g("finished"), "hltv": g("hltv"), "maps": maps}


def iv(x):
    return int(x) if (x or "").isdigit() else None


def map_info(mp):
    """-> dict(half=(t1,t2) or None, first={'t1': 'CT'|'T'}, final=(t1,t2) or None)"""
    fs = (mp.get("t1firstside") or "").lower()
    t1t, t1ct, t2t, t2ct = (iv(mp.get(k)) for k in ("t1t", "t1ct", "t2t", "t2ct"))
    out = {"map": mp.get("map"), "n": mp["n"], "first_side_t1": fs.upper() or None, "half": None, "final": None,
           "finished": mp.get("finished") == "true", "skip": mp.get("finished") == "skip"}
    if fs in ("ct", "t"):
        h1, h2 = (t1ct, t2t) if fs == "ct" else (t1t, t2ct)
        if h1 is not None and h2 is not None and h1 + h2 == 12:
            out["half"] = (h1, h2)
    ot = [iv(mp.get(k)) for k in ("t1otct", "t1ott", "t2otct", "t2ott")]
    if out["finished"] and None not in (t1t, t1ct, t2t, t2ct):
        a, b = t1t + t1ct + sum(x or 0 for x in ot[:2]), t2t + t2ct + sum(x or 0 for x in ot[2:])
        out["final"] = (a, b)
    return out


def find_match(m, cache):
    """m: bo3 match dict. cache: dict persisted by caller (titles per tournament). Returns (title, parsed, swapped)."""
    tn = (m.get("tournament") or {}).get("name") or ""
    t1 = (m.get("team1") or {}).get("name") if isinstance(m.get("team1"), dict) else None
    t2 = (m.get("team2") or {}).get("name") if isinstance(m.get("team2"), dict) else None
    start = datetime.fromisoformat(m["start_date"].replace("Z", "+00:00"))
    titles = cache.get(tn)
    if not titles:  # reuse titles another match of the same tournament already found (LP search ranking is unstable)
        try:
            for ev in json.load(open("state.json")).get("live_events", {}).values():
                if (ev.get("lp_titles") or {}).get(tn):
                    titles = cache[tn] = list(ev["lp_titles"][tn])
                    break
        except Exception:
            pass
    if not titles:
        s = lp_get({"action": "query", "list": "search", "srsearch": tn, "srlimit": 5})
        found = [x["title"] for x in s["query"]["search"] if "Tier" not in x["title"]]
        # a subpage hit ("X/Qualifier/1") means the main page is its parent "X" -> prefer pages containing "/"
        base = []
        yr = (re.search(r"\b(20\d\d)\b", tn) or [None, None])[1]
        for t in found:
            parts = t.split("/")
            if yr and yr in parts[1:]:  # "X/World Championship/2026/Europe/1" -> "X/World Championship/2026" (LESSONS 19)
                cand = "/".join(parts[:parts.index(yr, 1) + 1])
            else:
                cand = "/".join(parts[:2]) if len(parts) >= 3 else t
            if "/" in cand and cand not in base:
                base.append(cand)
        base = (base + [t for t in found if t not in base])[:2]
        titles = list(base)
        for b in base:
            ap = lp_get({"action": "query", "list": "allpages", "apprefix": b.split(":")[-1] + "/", "aplimit": 20})
            titles += [x["title"] for x in ap["query"]["allpages"]]
        if yr:  # series pages list older editions first (2023/2024/2025...) -> keep only this year's pages
            titles = [t for t in titles if yr in t.split("/")[1:]] or titles
        cache[tn] = titles = titles[:20]
    d = lp_get({"action": "query", "prop": "revisions", "rvprop": "content|timestamp", "rvslots": "main",
                "titles": "|".join(titles[:20])})
    best = None
    for p in d["query"]["pages"].values():
        if not p.get("revisions"):
            continue
        rev = p["revisions"][0]
        for body in split_matches(rev["slots"]["main"].get("*", "")):
            pm = parse_match(body)
            if not pm["date"] or abs((pm["date"] - start).total_seconds()) > 4 * 3600:
                continue
            if t1 and t2 and team_match(pm["t1"], t1) and team_match(pm["t2"], t2):
                sw = False
            elif t1 and t2 and team_match(pm["t1"], t2) and team_match(pm["t2"], t1):
                sw = True
            else:
                continue
            pm["rev_ts"] = rev["timestamp"]
            best = (p["title"], pm, sw)
    return best


def live_maps(m, cache):
    """-> {"title", "rev_ts", "maps": [map_info with half/final oriented team1:team2 of bo3]} or None"""
    r = find_match(m, cache)
    if not r:
        return None
    title, pm, sw = r
    maps = []
    for mp in pm["maps"]:
        mi = map_info(mp)
        if sw:
            for k in ("half", "final"):
                if mi[k]:
                    mi[k] = (mi[k][1], mi[k][0])
            if mi["first_side_t1"]:
                mi["first_side_t1"] = {"CT": "T", "T": "CT"}[mi["first_side_t1"]]
        maps.append(mi)
    return {"title": title, "rev_ts": pm["rev_ts"], "maps": maps, "hltv": pm.get("hltv")}


if __name__ == "__main__":
    import sys, live
    m = live.fetch_match(int(sys.argv[1]))
    try:  # same title cache as live.py / collector (state.json live_events[key].lp_titles)
        cache = dict(json.load(open("state.json")).get("live_events", {}).get(f"bo3:{sys.argv[1]}", {}).get("lp_titles") or {})
    except Exception:
        cache = {}
    print(json.dumps(live_maps(m, cache), ensure_ascii=False, indent=1, default=str))
