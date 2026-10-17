#!/usr/bin/env python3
"""Fetch fresh CS2 news and print NEW (unseen) items as JSON.

Does NOT modify state.json (items are marked seen only by post.py / mark_seen.py).
Writes the result to items.json too, so post.py can look items up by id.

Usage:
  python3 fetch.py                 # JSON list to stdout, last 24h
  python3 fetch.py --hours 12      # different freshness window
  python3 fetch.py --no-article    # skip fetching article pages (faster)
  python3 fetch.py --brief         # human-readable list instead of JSON
  python3 fetch.py --no-reddit     # skip r/GlobalOffensive
"""
import argparse, json, re, sys, html, shutil, subprocess, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import requests
from bs4 import BeautifulSoup
from common import (UA, MSK, ITEMS_FILE, load_state, save_json, norm_url, similar)

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


class Resp:
    def __init__(self, status, content):
        self.status_code, self.content = status, content
        self.text = content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.content)


def http_get(url, params=None, timeout=25):
    """GET via curl (Cloudflare-fronted sites like HLTV reject python-requests' TLS
    fingerprint with 403 but accept curl); falls back to requests."""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    if shutil.which("curl"):
        p = subprocess.run(["curl", "-sL", "--compressed", "--max-time", str(timeout), "-A", UA,
                            "-H", "Accept-Language: en-US,en;q=0.9", "-w", "\n%{http_code}", url],
                           capture_output=True)
        body, _, code = p.stdout.rpartition(b"\n")
        try:
            return Resp(int(code), body)
        except ValueError:
            pass
    r = S.get(url, timeout=timeout)
    return Resp(r.status_code, r.content)
CS_RE = re.compile(r"\b(cs2|cs ?2|cs:go|csgo|counter[- ]?strike)\b", re.I)
STEAM_IMG = "https://cdn.cloudflare.steamstatic.com/steam/apps/730/header.jpg"
NS = {"media": "http://search.yahoo.com/mrss/",
      "content": "http://purl.org/rss/1.0/modules/content/",
      "dc": "http://purl.org/dc/elements/1.1/"}

# name, url, needs_cs_filter
RSS_SOURCES = [
    ("HLTV", "https://www.hltv.org/rss/news", False),
    ("Dust2.us", "https://www.dust2.us/rss", False),
    ("PC Gamer", "https://www.pcgamer.com/feeds/tag/counter-strike-2", False),
    ("Dexerto", "https://www.dexerto.com/feed/", True),
]


def clean(txt):
    if not txt:
        return ""
    txt = BeautifulSoup(html.unescape(txt), "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", txt).strip()


def parse_date(s):
    try:
        d = parsedate_to_datetime(s.strip())
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def fetch_rss(name, url, cs_filter):
    r = http_get(url)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.iter("item"):
        g = lambda tag: (it.findtext(tag, default="", namespaces=NS) or "").strip()
        title, link = clean(g("title")), g("link")
        guid = g("guid") or link
        cats = " ".join((c.text or "") for c in it.findall("category"))
        desc = clean(g("description") or g("content:encoded"))
        if cs_filter and not (CS_RE.search(title) or CS_RE.search(cats)
                              or re.search(r"/counter-strike|/cs2", link, re.I)):
            continue
        img = ""
        for tag in ("media:content", "media:thumbnail", "enclosure"):
            el = it.find(tag, NS)
            if el is not None and el.get("url"):
                img = el.get("url"); break
        out.append({"id": f"{name.lower().replace(' ', '').replace('.', '')}:{guid.split('/')[-1] or guid}"
                          if not guid.startswith("http") else f"{name.lower().replace(' ', '').replace('.', '')}:{norm_url(guid)}",
                    "source": name, "title": title, "url": link,
                    "dt": parse_date(g("pubDate")), "summary": desc[:600], "image": img})
    return out


def fetch_steam():
    r = http_get("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
              params={"appid": 730, "count": 15, "feeds": "steam_community_announcements"}, timeout=25)
    r.raise_for_status()
    out = []
    for n in r.json()["appnews"]["newsitems"]:
        body = n.get("contents", "")
        imgm = re.search(r"\[img[^\]]*\](.*?)\[/img\]", body)
        img = ""
        if imgm:
            img = imgm.group(1).replace("{STEAM_CLAN_IMAGE}", "https://clan.akamai.steamstatic.com/images")
        text = re.sub(r"\[\*\]", " • ", body)
        text = re.sub(r"\[/?[^\]]+\]", " ", text).replace("\\[", "[")
        text = re.sub(r"\s+", " ", text).strip()
        out.append({"id": f"steam:{n['gid']}", "source": "Counter-Strike 2 (Steam)",
                    "title": n["title"], "url": f"https://store.steampowered.com/news/app/730/view/{n['gid']}",
                    "dt": datetime.fromtimestamp(n["date"], tz=timezone.utc),
                    "summary": text[:1500], "image": img or STEAM_IMG, "_full": True})
    return out


# ---------------------------------------------------------------- Reddit
# reddit.com/*.json (www/old/api) is login-walled / 403 for this box's IP as of 2026-10;
# the Atom feed works with our descriptive UA. Atom has no score/flair, so without OAuth
# the feed's own order (top of the last day, sorted by score) is the signal: rank <= REDDIT_TOP_N.
# If REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET are set (free "script"/app-only OAuth app),
# oauth.reddit.com JSON is used instead and the real score threshold applies.
REDDIT_SUB = "GlobalOffensive"
REDDIT_TOP_N = 25            # news/discussion: rank in top-of-day
REDDIT_TOP_N_HIGHLIGHT = 10  # highlight clips & long community posts: stricter
REDDIT_MIN_SCORE = 1000      # OAuth mode only
ATOM = {"a": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/"}
RD_MATCH_RE = re.compile(r"post-?match discussion|match thread|pre-?match|live discussion|daily discussion|"
                         r"weekly (thread|discussion)|megathread", re.I)
RD_SUPPORT_RE = re.compile(r"\b(help|tips? for|any tips|how (do|to|can|does)|new to|beginner|best (way|order|settings)|"
                           r"question|fps|stutter|lag(gy)?|crash(es|ing)?|settings|config|cfg|drivers?|"
                           r"can'?t (play|connect|launch)|not working|vac(ced)? (ban|for no)|trade ?ban|"
                           r"scam(med)?|hacked|steam guard|refund|lf[gm]|looking for (a )?(team|duo|friends))\b", re.I)
RD_GUIDE_RE = re.compile(r"\b(smoke|lineup|molotov|molly|flash|nade|utility) (from|for|to)\b|crosshair code|\bguide\b", re.I)
RD_HIGHLIGHT_RE = re.compile(r"\b(ace|[2-5]k|quad|triple|1v[2-5]|clutch|no-?scope|noscope|deagle|ninja ?defuse|"
                             r"wallbang|flick|insane|highlight)\b", re.I)
RD_NEWS_RE = re.compile(r"\b(sign(s|ed|ing)?|bench(ed)?|transfer|roster|leave[sd]?|leaving|join(s|ed|ing)?|contract|"
                        r"announce[ds]?|official(ly)?|confirm(s|ed)?|replace[sd]?|release[sd]?|stand-?in|retire[sd]?|"
                        r"drop(s|ped)?|kick(s|ed)?|update|patch|valve|vrs|ranking|"
                        r"statement|ban(ned|s)?|cheat(er|ing)?|lawsuit|sources?|report(s|ed)?|stats?|rating|"
                        r"record|first time|history|prize|invite[sd]?|qualif(y|ied|ies)|eliminat\w+|champion\w*)\b", re.I)
RD_MEDIA_DOMAINS = ("i.redd.it", "v.redd.it", "preview.redd.it", "reddit.com/gallery", "i.imgur.com", "imgur.com")


def reddit_classify(title, selftext, link, is_media):
    """-> kind: match_thread | support | guide | news | discussion | highlight | meme"""
    t = title or ""
    if RD_MATCH_RE.search(t) or re.search(r" / .+ / ", t):    # "A vs B / Event / Post-Match Discussion"
        return "match_thread"
    if RD_SUPPORT_RE.search(t) or (t.rstrip().endswith("?") and len(selftext) < 300):
        return "support"
    if RD_GUIDE_RE.search(t):
        return "guide"
    external = bool(link) and "reddit.com" not in link and not any(d in link for d in RD_MEDIA_DOMAINS)
    if RD_NEWS_RE.search(t) or re.search(r"[\"“”]", t) or "x.com/" in selftext or "twitter.com/" in selftext:
        return "news"
    if len(selftext) >= 300:
        return "discussion"
    if external and not re.search(r"youtu|twitch|kick\.com|streamable|medal\.tv|allstar", link):
        return "news"
    if RD_HIGHLIGHT_RE.search(t):
        return "highlight"
    return "meme"   # media / clip without context, short low-info titles


def _reddit_keep(kind, rank, score):
    if kind in ("match_thread", "support", "guide", "meme"):
        return False
    if score is not None:          # OAuth mode: real upvotes
        return score >= (REDDIT_MIN_SCORE * 2 if kind == "highlight" else REDDIT_MIN_SCORE)
    return rank <= (REDDIT_TOP_N_HIGHLIGHT if kind in ("highlight", "discussion") else REDDIT_TOP_N)


def fetch_reddit_oauth(cid, secret):
    tok = requests.post("https://www.reddit.com/api/v1/access_token", auth=(cid, secret),
                        data={"grant_type": "client_credentials"}, headers={"User-Agent": UA}, timeout=20)
    tok.raise_for_status()
    h = {"User-Agent": UA, "Authorization": "bearer " + tok.json()["access_token"]}
    r = requests.get(f"https://oauth.reddit.com/r/{REDDIT_SUB}/top", params={"t": "day", "limit": 50, "raw_json": 1},
                     headers=h, timeout=25)
    r.raise_for_status()
    out = []
    for rank, c in enumerate(r.json()["data"]["children"], 1):
        p = c["data"]
        if p.get("stickied") or p.get("over_18"):
            continue
        flair = p.get("link_flair_text") or ""
        if re.search(r"help|support|meme|fluff|match ?thread|tips|guide|feedback", flair, re.I):
            continue
        self_txt = (p.get("selftext") or "").strip()
        link = "" if p.get("is_self") else p.get("url_overridden_by_dest") or p.get("url") or ""
        is_media = bool(p.get("is_video") or p.get("is_gallery") or p.get("post_hint") in ("image", "hosted:video", "rich:video"))
        kind = reddit_classify(p["title"], self_txt, link, is_media)
        if not _reddit_keep(kind, rank, p.get("score", 0)):
            continue
        img = ""
        try:
            img = p["preview"]["images"][0]["source"]["url"]
        except Exception:
            th = p.get("thumbnail") or ""
            img = th if th.startswith("http") else ""
        out.append(_reddit_item(p["name"], p["title"], "https://www.reddit.com" + p["permalink"], link,
                                datetime.fromtimestamp(p["created_utc"], tz=timezone.utc), self_txt, img,
                                kind, rank, p.get("score"), flair, p.get("num_comments"), "u/" + p.get("author", "")))
    return out


def _reddit_item(name, title, permalink, link, dt, selftext, img, kind, rank, score, flair, comments, author):
    summ = selftext[:600]
    return {"id": f"reddit:{name}", "source": "Reddit", "title": title, "url": permalink, "dt": dt,
            "summary": summ, "text": selftext[:1800] if len(selftext) > 600 else "", "image": img,
            "_full": True, "reddit": {"kind": kind, "rank": rank, "score": score, "flair": flair,
                                      "comments": comments, "author": author, "link": link}}


def fetch_reddit():
    import os
    cid, sec = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if cid and sec:
        return fetch_reddit_oauth(cid, sec)
    r = http_get(f"https://www.reddit.com/r/{REDDIT_SUB}/top/.rss?t=day&limit=50")
    if r.status_code != 200:   # fallback: hot feed
        r = http_get(f"https://old.reddit.com/r/{REDDIT_SUB}/top/.rss?t=day&limit=50")
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for rank, e in enumerate(root.findall("a:entry", ATOM), 1):
        g = lambda tag: (e.findtext(tag, default="", namespaces=ATOM) or "").strip()
        title, content = html.unescape(g("a:title")), g("a:content")
        el = e.find("a:link", ATOM)
        permalink = el.get("href") if el is not None else ""
        links = re.findall(r'<a href="([^"]+)">\[link\]</a>', content)
        link = html.unescape(links[0]) if links else ""
        if link.rstrip("/") == permalink.rstrip("/"):
            link = ""                                   # text post
        md = re.search(r'<div class="md">(.*?)</div>', content, re.S)
        selftext = clean(md.group(1)) if md else ""
        th = e.find("media:thumbnail", ATOM)
        img = th.get("url") if th is not None else ""
        if re.match(r"https://i\.redd\.it/\S+\.(jpe?g|png|webp)$", link):
            img = link                                  # full-size original image
        is_media = bool(img) or any(d in link for d in RD_MEDIA_DOMAINS)
        kind = reddit_classify(title, selftext, link, is_media)
        if not _reddit_keep(kind, rank, None):
            continue
        dt = None
        try:
            dt = datetime.fromisoformat(g("a:published"))
        except Exception:
            pass
        out.append(_reddit_item(g("a:id"), title, permalink, link, dt, selftext, img, kind, rank, None, None,
                                None, g("a:author/a:name")))
    return out


def enrich(item):
    """Fetch article page: og:image + text excerpt."""
    try:
        r = http_get(item["url"], timeout=20)
        if r.status_code != 200:
            return
        s = BeautifulSoup(r.text, "lxml")
        og = s.find("meta", property="og:image")
        if og and og.get("content") and not item["image"]:
            item["image"] = html.unescape(og["content"])
        elif og and og.get("content") and "hltv" in item["source"].lower():
            item["image"] = html.unescape(og["content"])  # full-size og image
        paras = s.select("p.news-block") or s.select(".newstext-con p") or s.select("article p") or s.select("main p")
        texts = []
        for p in paras:
            t = p.get_text(" ", strip=True)
            if len(t) < 40 or t.lower().startswith(("clip link", "photo:", "image:")):
                continue
            texts.append(t)
            if sum(map(len, texts)) > 1500:
                break
        if texts:
            item["text"] = re.sub(r"\s+", " ", " ".join(texts))[:1800]
    except Exception as e:
        item["enrich_error"] = str(e)[:120]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--no-article", action="store_true")
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--no-reddit", action="store_true")
    a = ap.parse_args()

    st = load_state()
    seen_ids = set(st["seen"])
    seen_urls = {norm_url(v.get("url")) for v in st["seen"].values()}
    seen_titles = [v.get("title", "") for v in st["seen"].values()][-500:]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=a.hours)

    raw, errors = [], {}
    for name, url, f in RSS_SOURCES:
        try:
            raw += fetch_rss(name, url, f)
        except Exception as e:
            errors[name] = str(e)[:200]
    try:
        raw += fetch_steam()
    except Exception as e:
        errors["Steam"] = str(e)[:200]
    if not a.no_reddit:
        try:
            raw += fetch_reddit()
        except Exception as e:
            errors["Reddit"] = str(e)[:200]

    raw.sort(key=lambda x: x["dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    new, batch_titles, batch_urls = [], [], set()
    for it in raw:
        if not it["dt"] or it["dt"] < cutoff:
            continue
        u = norm_url(it["url"])
        if it["id"] in seen_ids or u in seen_urls or u in batch_urls:
            continue
        if any(similar(it["title"], t) for t in seen_titles + batch_titles):
            continue
        batch_urls.add(u); batch_titles.append(it["title"])
        new.append(it)

    if not a.no_article:
        for it in new:
            if not it.pop("_full", False):
                enrich(it)
    result = []
    for it in new:
        if re.search(r"openGraph\w*Logo|/logo|dust2logo", it.get("image") or "", re.I):
            it["image"] = ""  # generic site logo -> post as text
        it.pop("_full", None)
        it["published"] = it.pop("dt").astimezone(MSK).strftime("%Y-%m-%d %H:%M MSK")
        it.setdefault("text", "")
        result.append(it)

    save_json(ITEMS_FILE, {"fetched_at": datetime.now(MSK).isoformat(timespec="seconds"),
                           "errors": errors, "items": result})
    if errors:
        print("source errors: " + json.dumps(errors, ensure_ascii=False), file=sys.stderr)
    if a.brief:
        for it in result:
            rd = it.get("reddit")
            extra = f" [{rd['kind']}, top#{rd['rank']}" + (f", {rd['score']}↑" if rd.get('score') is not None else "") + (f", {rd['flair']}" if rd.get('flair') else "") + "]" if rd else ""
            print(f"[{it['id']}] {it['published']} | {it['source']}{extra} | {it['title']}\n    {it['url']}\n    img: {it['image'] or '-'}")
        print(f"{len(result)} new item(s)")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
