#!/usr/bin/env python3
"""Read-only pre-send check for a draft caption (HTML as passed to post.py). Nothing is posted or written.

  python3 check_post.py draft.txt [--photo|--text] [--posts 40] [--exclude-post N ...] [--offline]
  python3 check_post.py -  < draft.txt
  python3 check_post.py --post 33          # check an already published post (fetched from t.me/s, excluded from comparison)

Checks (FAIL = exit 1, WARN = verify by hand, exit 0):
  scores    FAIL  final map scores (13:8, 16:12 ...) / finished series scores that contradict the same map+teams in the last
                  ~40 channel posts (https://t.me/s/de_csnews = source of truth for already covered matches, LESSONS #2).
                  Compared as unordered pairs (13:8 == 8:13), teams = team words in the sentence + team hashtags.
  links     FAIL  auto-linkable text: URLs, domains (dust2.us, hltv.org), @handles, t.me (LESSONS #5).
  hashtags  FAIL  outside 2-4 tags besides #de_csnews;  WARN  #de_csnews missing (post.py appends it).
  length    FAIL  visible length > 1024 (photo caption) / 4096 (text); unbalanced <b>/<i>/<a> tags.
  counts    WARN  ordinal/count/record claims (третья, 6-й, рекорд, впервые ...) -> verify against the CURRENT primary
                  profile (HLTV player page / Liquipedia), not an older article (LESSONS #1).
  headline  WARN  headline missing bold, or repeated in the body (LESSONS #3).
"""
import argparse, difflib, html, re, sys, urllib.request
from datetime import datetime
from common import UA, MSK

MAPS = {"mirage": "Mirage", "inferno": "Inferno", "nuke": "Nuke", "ancient": "Ancient", "anubis": "Anubis",
        "dust2": "Dust2", "dust 2": "Dust2", "dust ii": "Dust2", "даст": "Dust2", "дасте": "Dust2", "cache": "Cache",
        "train": "Train", "overpass": "Overpass", "vertigo": "Vertigo", "мираж": "Mirage", "инферно": "Inferno",
        "нюк": "Nuke", "анубис": "Anubis", "кэш": "Cache"}
MAP_RE = re.compile(r"(?<![\w])(" + "|".join(sorted(map(re.escape, MAPS), key=len, reverse=True)) + r")(?![\w])", re.I)
SCORE_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{1,2})(?![\d:])")
GENERIC_TAG = re.compile(r"^(cs2|cs|de_csnews|[а-яё_]+|.*(league|major|series|cup|masters|open|final|epl|iem|blast|pgl|esl|cct|pulse|beat|stake|championship|invitational|frag|thunderpick|qualifier).*)$", re.I)
SERIES_FINAL = re.compile(r"матч окончен|чемпион|всухую|обыграв|обыграли|разобра|забрали (бронзу|титул|трофей)|в финале|итог", re.I)
ORD = (r"перв|втор|трет|четв[её]рт|пят|шест|седьм|восьм|девят|десят|одиннадцат|двенадцат|двадцат|тридцат|сот")
ORD_RE = re.compile(r"\b(?:" + ORD + r")(?:ый|ая|ое|ой|ий|ья|ье|ую|ей|ого|ему|ым|ом|ые|ых|ыми|ьей|ьего|ьи)\b(?:\s+(\w+))?"
                    r"|\b\d+-?(?:й|я|е|го|му|м|ю|ой|ая|ый)\b(?:\s+(\w+))?"
                    r"|\b(рекорд\w*|впервые|первый в истории|лучш\w+ в истории|в \d+-й раз)\b", re.I)
ORD_SKIP = re.compile(r"^(мест\w*|карт\w*|раунд\w*|половин\w*|день|дня|матч\w*|минут\w*|пистолетк\w*|овертайм\w*|тайм\w*|стади\w*)$", re.I)
LINK_RE = re.compile(r"https?://\S+|\bt\.me/\S+|(?<![\w@#])@[A-Za-z0-9_]{3,}|\b[\w-]+\.(?:com|org|gg|us|ru|net|tv|io|me|co|uk|de|fr|dk|info|pro|xyz)\b", re.I)


def visible(h):
    t = re.sub(r"<br\s*/?>", "\n", h)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t)


POST_TS = {}  # post id -> epoch seconds (for --since-hours)


def fetch_channel(n, exclude):
    posts, before = {}, None
    while len(posts) < n:
        url = "https://t.me/s/de_csnews" + (f"?before={before}" if before else "")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        page = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
        found = re.findall(r'data-post="de_csnews/(\d+)"(.*?)(?=data-post="de_csnews/|\Z)', page, re.S)
        if not found:
            break
        for pid, seg in found:
            m = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', seg, re.S)
            if m and int(pid) not in exclude:
                d = re.search(r'datetime="([^"]+)"', seg)
                when = ""
                if d:
                    try:
                        _dt = datetime.fromisoformat(d.group(1))
                        POST_TS[int(pid)] = _dt.timestamp()
                        when = _dt.astimezone(MSK).strftime("%d.%m %H:%M MSK")
                    except ValueError:
                        when = d.group(1)
                posts[int(pid)] = (visible(m.group(1)), when)
        mn = min(int(p) for p, _ in found)
        if before is not None and mn >= before:
            break
        before = mn
        if mn <= 1:
            break
    return dict(sorted(posts.items())[-n:])


def sentences(text):
    return [s for s in re.split(r"\n+|(?<=[.!?])\s+", text) if s.strip()]


def team_tags(text):
    return {t for t in re.findall(r"#(\w+)", text) if not GENERIC_TAG.match(t)}


def extract_scores(text, vocab):
    """-> list of (kind, map|None, frozenset teams, (a,b), snippet). kind: map | series."""
    tags = team_tags(text)
    out = []
    body = "\n".join(l for l in text.split("\n") if not l.strip().startswith("#"))
    sents = sentences(body)
    for i, s in enumerate(sents):
        # teams of THIS match: the sentence, then preceding sentences of the post, hashtags only as a last resort
        words = set()
        # recap line "Team A 2:1 Team B (maps...)" (results_auto.py): teams are the two names on THIS line only
        rl = re.match(r"^\s*([\w.' -]+?) ([0-3]):([0-3]) ([\w.' -]+?)\s*(\(|$)", s)
        if rl:
            words = {rl.group(1).split()[-1], rl.group(4).split()[0]}
        for prev in ([] if rl else reversed(sents[:i + 1])):
            words |= {w for w in re.findall(r"[\w.]+", prev) if w in vocab}
            if len(words) >= 2:
                break
        teams = frozenset(words if len(words) >= 2 else words | tags)
        for m in SCORE_RE.finditer(s):
            a, b = int(m.group(1)), int(m.group(2))
            hi, lo = max(a, b), min(a, b)
            pre = s[:m.start()]
            maps = list(MAP_RE.finditer(pre))
            mp = MAPS[maps[-1].group(1).lower()] if maps else None
            if mp is None:
                nxt = MAP_RE.search(s[m.end():m.end() + 20])
                mp = MAPS[nxt.group(1).lower()] if nxt else None
            snip = s.strip()[:140]
            if hi >= 13 and (hi - lo >= 2) and (hi == 13 or hi >= 16) and mp:
                out.append(("map", mp, teams, tuple(sorted((a, b))), snip))
            elif hi <= 3 and not mp and SERIES_FINAL.search(s) and re.search(r"\b(bo|в серии|финал|матч|всухую|обыграв|разобрав)", s, re.I):
                out.append(("series", None, teams, tuple(sorted((a, b))), snip))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?")
    ap.add_argument("--post", type=int, help="check published post N (fetched from the preview)")
    ap.add_argument("--photo", action="store_true", help="photo caption (1024 limit, default)")
    ap.add_argument("--text", action="store_true", help="text message (4096 limit)")
    ap.add_argument("--posts", type=int, default=40)
    ap.add_argument("--exclude-post", type=int, action="append", default=[])
    ap.add_argument("--offline", action="store_true", help="skip the channel comparison")
    ap.add_argument("--since-hours", type=float, help="compare scores only with channel posts from the last N hours "
                    "(daily recap: avoids false FAILs on group-stage rematches, e.g. 9z-Aurora opening vs decider)")
    a = ap.parse_args()
    fails, warns = [], []
    exclude = set(a.exclude_post)
    channel = None
    if a.post:
        exclude.add(a.post)
        allp = fetch_channel(max(a.posts, 60), set())
        if a.post not in allp:
            sys.exit(f"post {a.post} not found in preview")
        raw = allp[a.post][0]
        channel = {k: v for k, v in allp.items() if k not in exclude}
        channel = dict(list(channel.items())[-a.posts:])
    else:
        raw = sys.stdin.read() if a.file in (None, "-") else open(a.file, encoding="utf-8").read()
    text = visible(raw).strip()
    lines = [l for l in text.split("\n")]

    # length / html
    limit = 4096 if a.text else 1024
    vis_len = len(text) + (0 if "#de_csnews" in text else len(" #de_csnews"))
    if vis_len > limit:
        fails.append(f"length: {vis_len} visible chars > {limit}")
    for tag in ("b", "i", "a", "u", "s", "code"):
        if len(re.findall(rf"<{tag}(?:\s[^>]*)?>", raw)) != len(re.findall(rf"</{tag}>", raw)):
            fails.append(f"html: unbalanced <{tag}> tags")

    # hashtags
    tags = re.findall(r"#(\w+)", text)
    if "de_csnews" not in tags:
        warns.append("hashtags: #de_csnews missing (post.py appends it; add it if you post another way)")
    n = len([t for t in tags if t != "de_csnews"])
    if not 2 <= n <= 4:
        fails.append(f"hashtags: {n} tags besides #de_csnews (need 2-4)")

    # links
    for m in LINK_RE.finditer(text):
        if m.group(0).lower() in ("@de_csnews",):
            continue  # own channel mention is fine
        fails.append(f"links: auto-linkable text «{m.group(0)}» (use clean names: HLTV, Dust2, not domains/@handles)")

    # headline
    first = next((l for l in lines if l.strip()), "")
    if not a.post and "<b>" not in raw.split("\n", 1)[0]:
        warns.append("headline: first line is not bold")
    body = "\n".join(l for l in lines[1:] if l.strip() and not l.strip().startswith("#"))
    hn = re.sub(r"[^\w\s:]", " ", first.lower()).split()
    if hn and body:
        bn = re.sub(r"[^\w\s:]", " ", body.lower())
        for s in sentences(bn):
            ratio = difflib.SequenceMatcher(None, " ".join(hn), " ".join(s.split())).ratio()
            if ratio >= 0.7:
                warns.append(f"headline: repeated in body ({ratio:.0%} similar): «{s.strip()[:100]}»")
                break
        content = [w for w in hn if len(w) > 3 and not w.isdigit()]
        first_s = (sentences(bn) or [""])[0]
        if len(content) >= 3 and sum(w in first_s for w in content) / len(content) >= 0.8:
            warns.append("headline: first body sentence repeats most headline words - rephrase so it adds information")

    # counts / ordinals
    for s in sentences(text):
        hits = []
        for m in ORD_RE.finditer(s):
            nxt = m.group(1) or m.group(2)
            if nxt and ORD_SKIP.match(nxt):
                continue
            hits.append(m.group(0))
        if hits:
            warns.append(f"counts: {', '.join('«%s»' % h for h in hits)} - verify against the CURRENT primary profile "
                         f"(HLTV player/team page, Liquipedia), not an older article: «{s.strip()[:160]}»")

    # scores vs channel history
    try:
        if not a.offline:
            if channel is None:
                channel = fetch_channel(a.posts, exclude)
            if a.since_hours:
                import time as _t
                cut = _t.time() - a.since_hours * 3600
                channel = {k: v for k, v in channel.items() if POST_TS.get(k, cut) >= cut}
            vocab = set()
            for t, _ in channel.values():
                vocab |= team_tags(t)
            vocab |= team_tags(text)
            mine = extract_scores(text, vocab)
            # Live single-map drafts (1 map score) must not collide with an earlier rematch's
            # finished multi-map recap for the same teams (LESSONS rematch / Stake Pulse Beat).
            mine_maps = sum(1 for k, *_ in mine if k == "map")
            seen = set()
            for pid, (t, dt) in channel.items():
                ch_scores = extract_scores(t, vocab)
                ch_maps = sum(1 for k, *_ in ch_scores if k == "map")
                for kind, mp, teams, sc, snip in ch_scores:
                    for k2, mp2, teams2, sc2, snip2 in mine:
                        if kind != k2 or mp != mp2 or len(teams & teams2) < 2:
                            continue
                        # skip: draft is one live map, channel post is a multi-map series recap
                        if kind == "map" and mine_maps == 1 and ch_maps >= 2:
                            continue
                        key = (kind, mp, sc, sc2, pid)
                        if sc != sc2 and key not in seen:
                            seen.add(key)
                            what = mp if kind == "map" else "series"
                            msg = (f"scores: {what} {'/'.join(sorted(teams & teams2))}: draft says {sc2[1]}:{sc2[0]}, "
                                   f"channel post {pid} ({dt}) says {sc[1]}:{sc[0]} - «{snip}» "
                                   f"https://t.me/de_csnews/{pid}")
                            (fails if kind == "map" else warns).append(msg + ("" if kind == "map" else " (series score may be mid-match; check)"))
            if mine:
                print(f"info: {len(mine)} score(s) in draft compared with {len(channel)} channel posts "
                      f"({min(channel)}-{max(channel)})" if channel else "info: no channel posts fetched")
    except Exception as e:
        warns.append(f"scores: channel history NOT checked ({e.__class__.__name__}: {e}) - compare scores with t.me/s/de_csnews by hand")

    for w in warns:
        print("WARN", w)
    for f in fails:
        print("FAIL", f)
    print(f"result: {'FAIL' if fails else 'OK'} ({len(fails)} fail, {len(warns)} warn)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
