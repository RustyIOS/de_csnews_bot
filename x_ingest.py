#!/usr/bin/env python3
"""Ingest X (Twitter) posts fetched by the hourly agent (x MCP tools) into items.json,
so post.py can publish them like any other item. Does NOT touch state.json.

Input (one or more files, or '-' for stdin), any of:
  * raw x tool output (search_posts_all / get_users_posts response: {"data":[..], "includes":{users,media}}),
    incl. CallDynamicTool write_to_file dumps (header/footer text around the JSON is ignored);
  * a JSON list of raw responses;
  * a JSON list of simplified posts:
      {"id", "username" (or "author"), "text", "created_at", "url", "image"/"media_url",
       "is_reply"?, "is_retweet"?, "links"? [expanded urls]}
Filters: last --hours (24), drops replies/retweets, pure promo/sponsor/betting posts, near-empty posts
("mood", "W 🔥"), posts about other games (LoL, Valorant, Dota, Fortnite...) and, for multigame accounts
(x_accounts.json multigame=true), anything without a CS signal. Dedupe vs state.json (ids, urls incl. linked
article urls, similar titles) and vs items already in items.json.
Items: id "x:<post id>", source "X / @username", url https://x.com/<user>/status/<id>, image = photo /
video thumbnail / link-card image, summary = full text with expanded links.

Usage:
  python3 x_ingest.py agent-tools/<file>.txt [more files] [--hours 24] [--brief] [--dry-run] [--min-likes N]
  python3 fetch.py > new.json   # FIRST: fetch.py rewrites items.json; run x_ingest.py after it
"""
import argparse, html, json, os, re, sys
from datetime import datetime, timedelta, timezone
from common import (BASE, ITEMS_FILE, MSK, load_json, save_json, load_state, norm_url, similar)

ACCOUNTS_FILE = os.path.join(BASE, "x_accounts.json")

PROMO_RE = re.compile(
    r"\b(use (my |our )?code|promo ?code|giveaway|enter to win|win (a|an|our|one)\b|retweet to win|\d+ ?% ?off|"
    r"discount|merch|jersey|shop (now|the)|available now|pre-?order|link in bio|sponsored|#ad\b|#sponsored|"
    r"partnered with|thanks to our (friends|partners?)|official \w+( \w+)? (partner|chair|monitor|headset|mouse)|"
    r"presented by|powered by|brought to you by|in partnership with|casino|odds|bet on|place your bets?|"
    r"tickets?( are)? (on sale|available)|get your tickets|subscribe|watch (it )?(live|now)|tune in|"
    r"live (now|on twitch|on youtube|on kick)|we'?re live|going live|stream(ing)? (live|now))", re.I)
OTHER_GAME_RE = re.compile(
    r"\b(LoL|League of Legends|CBLOL|LCK|LEC|LCS|Worlds 20\d\d|VALORANT|VCT|Dota ?2?|The International|"
    r"Fortnite|FNCS|Rocket League|RLCS|Rainbow ?Six|R6|Siege|Apex|ALGS|PUBG|Overwatch|OWCS|Call of Duty|CoD|"
    r"Warzone|EA ?FC|FIFA|eFootball|Free Fire|MLBB|Mobile Legends|Deadlock|chess|Halo|Tekken|Street Fighter|"
    r"StarCraft|Hearthstone|Marvel Rivals|Brawl Stars|Clash Royale|Honor of Kings|Wild Rift|TFT|Teamfight|"
    r"Smash|Valo)\b|#\w*(LoL|Valorant|VCT|Dota|Fortnite|FNCS|R6|RL|Apex|PUBG|OW|CoD|MLBB|FC)\b", re.I)
CS_SIGNAL_RE = re.compile(
    r"\b(CS2?|Counter-?Strike|CS:GO|CSGO|HLTV|VRS|Major|ESL ?Pro ?League|EPL|IEM|BLAST|PGL|StarLadder|"
    r"Dust ?2|Mirage|Inferno|Nuke|Ancient|Anubis|Overpass|Train|Vertigo|Cache|AWP|Deagle|ace|clutch|1v[1-5])\b|"
    r"#(CS2|CS|CSGO|CounterStrike|ESLProLeague|ESLProTour|EPL|BLASTPremier|BLAST\w*|IEM\w*|PGL\w*|StarLadder\w*|\w*Major\w*|HLTV\w*)\b", re.I)
URL_RE = re.compile(r"https?://t\.co/\w+")


def parse_blobs(text):
    """Extract every top-level JSON value from text (tool dumps have non-JSON header/footer lines)."""
    dec, i, out = json.JSONDecoder(), 0, []
    while True:
        m = re.search(r"[\[{]", text[i:])
        if not m:
            break
        j = i + m.start()
        try:
            obj, end = dec.raw_decode(text, j)
            out.append(obj); i = end
        except json.JSONDecodeError:
            i = j + 1
    return out


def posts_from(obj):
    """Normalize raw API responses / simplified lists to a list of post dicts."""
    if isinstance(obj, list):
        res = []
        for x in obj:
            res += posts_from(x)
        return res
    if not isinstance(obj, dict):
        return []
    if "data" in obj and isinstance(obj["data"], (list, dict)):
        data = obj["data"] if isinstance(obj["data"], list) else [obj["data"]]
        inc = obj.get("includes", {}) or {}
        users = {u["id"]: u.get("username") for u in inc.get("users", [])}
        media = {m["media_key"]: m for m in inc.get("media", [])}
        res = []
        for p in data:
            if "text" not in p:
                continue
            p = dict(p)
            p["_username"] = users.get(p.get("author_id")) or p.get("username") or \
                (re.search(r"x\.com/([^/]+)/status", p.get("url", "")) or [None, None])[1]
            p["_media"] = [media[k] for k in p.get("attachments", {}).get("media_keys", []) if k in media]
            res.append(p)
        return res
    if "text" in obj and ("id" in obj):
        return [obj]
    return []


def clean_text(p):
    t = p.get("note_tweet", {}).get("text") or p.get("text", "")
    for u in (p.get("entities", {}) or {}).get("urls", []) or []:
        exp = u.get("unwound_url") or u.get("expanded_url") or ""
        if u.get("url"):
            # media links (pic.x.com / x.com/.../photo) are dropped, real links are expanded
            t = t.replace(u["url"], "" if re.search(r"(pic\.x\.com|pic\.twitter\.com|/(photo|video)/\d)", exp) or not exp else exp)
    return html.unescape(URL_RE.sub("", t)).strip()


def links_of(p):
    out = list(p.get("links", []) or [])
    for u in (p.get("entities", {}) or {}).get("urls", []) or []:
        exp = u.get("unwound_url") or u.get("expanded_url") or ""
        if exp and not re.search(r"(x|twitter)\.com/.+/(photo|video)/|pic\.(x|twitter)\.com", exp):
            out.append(exp)
    return out


def image_of(p):
    for k in ("image", "media_url", "image_url"):
        if p.get(k):
            return p[k]
    for m in p.get("_media", []):
        if m.get("type") == "photo" and m.get("url"):
            return m["url"] + ("" if "?" in m["url"] else "?name=large")
        if m.get("preview_image_url"):
            return m["preview_image_url"]
    for u in (p.get("entities", {}) or {}).get("urls", []) or []:
        imgs = u.get("images") or []
        if imgs:
            return max(imgs, key=lambda x: x.get("width", 0)).get("url", "")
    return ""


def meaningful_len(t):
    t = re.sub(r"https?://\S+|[@#]\w+", "", t)
    return len(re.sub(r"[^\w]+", "", t))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--min-likes", type=int, default=0)
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="don't write items.json")
    a = ap.parse_args()

    accounts = {x["username"].lower(): x for x in load_json(ACCOUNTS_FILE, {}).get("accounts", [])}
    posts = []
    for f in a.files:
        txt = sys.stdin.read() if f == "-" else open(f, encoding="utf-8").read()
        for blob in parse_blobs(txt):
            posts += posts_from(blob)

    st = load_state()
    seen_ids = set(st["seen"])
    seen_urls = {norm_url(v.get("url")) for v in st["seen"].values()}
    cache = load_json(ITEMS_FILE, {"items": []})
    cache.setdefault("items", [])
    have_ids = {i["id"] for i in cache["items"]}
    known_urls = seen_urls | {norm_url(i.get("url")) for i in cache["items"]} | \
        {norm_url(l) for i in cache["items"] for l in (i.get("x", {}) or {}).get("links", [])}
    known_titles = [v.get("title", "") for v in st["seen"].values()][-500:] + [i.get("title", "") for i in cache["items"]]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=a.hours)

    new, dropped, batch_ids = [], {}, set()
    drop = lambda why: dropped.__setitem__(why, dropped.get(why, 0) + 1)
    for p in posts:
        pid = str(p.get("id", ""))
        user = p.get("_username") or p.get("username") or p.get("author") or ""
        user = user.lstrip("@")
        iid = f"x:{pid}"
        if not pid or iid in batch_ids:
            drop("duplicate in input"); continue
        batch_ids.add(iid)
        try:
            dt = datetime.fromisoformat(str(p.get("created_at", "")).replace("Z", "+00:00"))
        except ValueError:
            drop("no date"); continue
        if dt < cutoff:
            drop("older than window"); continue
        refs = {r.get("type") for r in p.get("referenced_tweets", []) or []}
        raw_text = p.get("text", "")
        if "replied_to" in refs or p.get("in_reply_to_user_id") or p.get("is_reply") or raw_text.startswith("@"):
            drop("reply"); continue
        if "retweeted" in refs or p.get("is_retweet") or raw_text.startswith("RT @"):
            drop("retweet"); continue
        text = clean_text(p)
        acc = accounts.get(user.lower(), {})
        if meaningful_len(text) < 20:
            drop("too short / no context"); continue
        if PROMO_RE.search(text):
            drop("promo/sponsor/betting"); continue
        cs = CS_SIGNAL_RE.search(text)
        if OTHER_GAME_RE.search(text) and not cs:
            drop("other game"); continue
        if acc.get("multigame") and not cs:
            drop("multigame account, no CS signal"); continue
        likes = (p.get("public_metrics") or {}).get("like_count")
        if a.min_likes and likes is not None and likes < a.min_likes:
            drop("below --min-likes"); continue
        url = p.get("url") or f"https://x.com/{user}/status/{pid}"
        links = links_of(p)
        if iid in seen_ids or iid in have_ids or norm_url(url) in known_urls:
            drop("already seen/ingested"); continue
        if any(norm_url(l) in known_urls for l in links):
            drop("links to an article we already have"); continue
        title = re.sub(r"https?://\S+", "", text).strip().split("\n")[0].strip()[:140]
        if any(similar(title, t) for t in known_titles):
            drop("similar title already seen"); continue
        known_titles.append(title)
        kinds = {m.get("type") for m in p.get("_media", [])}
        new.append({
            "id": iid, "source": f"X / @{user}", "title": title, "url": url,
            "published": dt.astimezone(MSK).strftime("%Y-%m-%d %H:%M MSK"),
            "summary": text[:1000], "text": "", "image": image_of(p),
            "x": {"username": user, "group": acc.get("group", "other"), "likes": likes,
                  "reposts": (p.get("public_metrics") or {}).get("retweet_count"),
                  "media": sorted(k for k in kinds if k), "quoted": "quoted" in refs, "links": links},
        })

    new.sort(key=lambda i: i["published"], reverse=True)
    if not a.dry_run and new:
        cache["items"] += new
        cache["x_ingested_at"] = datetime.now(MSK).isoformat(timespec="seconds")
        save_json(ITEMS_FILE, cache)
    print(f"x_ingest: {len(posts)} post(s) in, {len(new)} new item(s)" + (" (dry run, items.json untouched)" if a.dry_run else "")
          + f"; dropped: {json.dumps(dropped, ensure_ascii=False)}", file=sys.stderr)
    if a.brief:
        for it in new:
            x = it["x"]
            print(f"[{it['id']}] {it['published']} | {it['source']} [{x['group']}, ♥{x['likes']}, {'/'.join(x['media']) or 'text'}] | {it['title']}\n    {it['url']}\n    img: {it['image'] or '-'}"
                  + (f"\n    links: {' '.join(x['links'])}" if x['links'] else ""))
    else:
        print(json.dumps(new, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
