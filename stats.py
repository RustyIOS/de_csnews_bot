#!/usr/bin/env python3
"""Channel stats for the weekly owner report (nothing is posted).

Usage:
  python3 stats.py [--days 7] [--channel de_csnews] [--no-save] [--summary-only]
Data:
  - subscribers: Bot API getChatMemberCount (env TELEGRAM_BOT_TOKEN, never printed); every run appends
    {ts, date, subscribers} to stats.json "history" -> weekly growth = now vs the newest snapshot >= 6.5 days old
    (or the oldest one, flagged partial).
  - posts: public web preview https://t.me/s/<channel> (paginated with ?before=<id>), last --days days:
    views, reactions (emoji -> count), type inferred from text (news/live/announce/results/poll/weekly/poll_result/
    tournament/welcome). Service messages (pinned, photo changed...) are skipped.
Output: JSON on stdout with "summary_ru" (short Russian text for the owner). Snapshot saved to stats.json "runs".
"""
import argparse, json, os, re, subprocess, sys, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from common import BASE, MSK, CHANNEL_ID, load_json, save_json, now_msk

STATS_FILE = os.path.join(BASE, "stats.json")
TYPE_RU = {"news": "новости", "live": "live-посты", "announce": "анонсы матчей", "results": "результаты",
           "poll": "опросы", "poll_result": "итоги опросов", "weekly": "итоги недели", "tournament": "анонсы турниров",
           "welcome": "приветствие"}


def num(s):
    s = (s or "").strip().replace(",", ".").replace(" ", "")
    m = re.match(r"^([\d.]+)([KM]?)$", s, re.I)
    if not m:
        return None
    v = float(m.group(1))
    return int(round(v * {"": 1, "K": 1e3, "M": 1e6}[m.group(2).upper()]))


def fetch_page(channel, before=None):
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    p = subprocess.run(["curl", "-sL", "--max-time", "30", "-A", "Mozilla/5.0 (X11; Linux x86_64)", url],
                       capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(f"fetch failed: {url}")
    return BeautifulSoup(p.stdout.decode("utf-8", "replace"), "html.parser")


def classify(text, has_poll, has_photo):
    t = text or ""
    low = t.lower()
    if has_poll:
        return "poll"
    if re.search(r"итоги опроса|угадали", low):
        return "poll_result"
    if "итоги недели" in low:
        return "weekly"
    if re.search(r"матчи дня", low):
        return "announce"
    if re.search(r"итоги дня", low):
        return "results"
    if re.search(r"добро пожаловать", low):
        return "welcome"
    if t.startswith("🔴") or re.search(r"матч начался|пистолет|эйс|\bace\b|клатч|ниндзя|половин|"
                                        r"ведут в серии|счёт в серии|овертайм|забира(ют|ет) \w+ — \d+:\d+", low) or t.startswith("🗺"):
        return "live"
    if re.search(r"скоро турнир|призов(ой|ым) фонд", low) and "источник" not in low:
        return "tournament"
    # result posts carry no source line and a series score
    if "источник:" not in low and re.search(r"\b\d:\d\b", t):
        return "results"
    return "news"


def parse_posts(soup, channel):
    out = []
    for m in soup.select("div.tgme_widget_message[data-post]"):
        if "service_message" in (m.get("class") or []):
            continue
        pid = int(m["data-post"].split("/")[-1])
        tm = m.select_one("time[datetime]")
        dt = datetime.fromisoformat(tm["datetime"]) if tm else None
        tx = m.select_one(".tgme_widget_message_text")
        text = tx.get_text("\n", strip=True) if tx else ""
        poll = m.select_one(".tgme_widget_message_poll")
        if poll and not text:
            q = poll.select_one(".tgme_widget_message_poll_question")
            text = q.get_text(" ", strip=True) if q else "Опрос"
        views = num((m.select_one(".tgme_widget_message_views") or {}).get_text() if m.select_one(".tgme_widget_message_views") else "")
        reacts = {}
        for r in m.select(".tgme_reaction"):
            em = r.select_one("b")
            key = em.get_text() if em else ("⭐" if "paid" in " ".join(r.get("class") or []) else "?")
            cnt = num(r.get_text("", strip=True).replace(key, "")) or 0
            reacts[key] = reacts.get(key, 0) + cnt
        has_photo = bool(m.select_one(".tgme_widget_message_photo_wrap, .tgme_widget_message_video_player"))
        title = next((ln for ln in text.split("\n") if len(re.sub(r"\W", "", ln)) > 2), text[:80])
        out.append({"id": pid, "url": f"https://t.me/{channel}/{pid}",
                    "date_msk": dt.astimezone(MSK).strftime("%Y-%m-%d %H:%M") if dt else None,
                    "ts": dt.isoformat() if dt else None, "views": views, "reactions": reacts,
                    "reactions_total": sum(reacts.values()), "has_photo": has_photo,
                    "type": classify(text, bool(poll), has_photo), "title": title[:120]})
    return out


def collect(channel, days, max_pages=30):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    posts, before, seen = {}, None, set()
    for _ in range(max_pages):
        soup = fetch_page(channel, before)
        page = parse_posts(soup, channel)
        ids = [int(m["data-post"].split("/")[-1]) for m in soup.select("div.tgme_widget_message[data-post]")]
        if not ids or set(ids) <= seen:
            break
        seen |= set(ids)
        for p in page:
            if p["ts"] and datetime.fromisoformat(p["ts"]) >= cutoff:
                posts[p["id"]] = p
        oldest = min(ids)
        page_dates = [datetime.fromisoformat(p["ts"]) for p in page if p["ts"]]
        if oldest <= 1 or (page_dates and min(page_dates) < cutoff):
            break
        before = oldest
        time.sleep(1)
    return sorted(posts.values(), key=lambda p: p["id"])


def subscribers(channel):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None, "TELEGRAM_BOT_TOKEN not set"
    import requests
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getChatMemberCount",
                         params={"chat_id": f"@{channel}"}, timeout=30).json()
    except Exception as e:
        return None, f"request failed: {type(e).__name__}"
    return (r["result"], None) if r.get("ok") else (None, r.get("description"))


def growth(history, now_count):
    if now_count is None or not history:
        return None
    now = datetime.now(timezone.utc)
    old = [h for h in history if h.get("subscribers") is not None and
           now - datetime.fromisoformat(h["ts"]) >= timedelta(days=6.5)]
    base = old[-1] if old else min(history, key=lambda h: h["ts"])
    if base.get("subscribers") is None:
        return None
    days = (now - datetime.fromisoformat(base["ts"])).total_seconds() / 86400
    return {"from": base["subscribers"], "from_date": base["date"], "to": now_count,
            "delta": now_count - base["subscribers"], "days": round(days, 1), "partial": not old}


def plural(n, one, few, many):
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--channel", default="de_csnews")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--summary-only", action="store_true")
    a = ap.parse_args()

    st = load_json(STATS_FILE, {"history": [], "runs": []})
    subs, sub_err = subscribers(a.channel)
    g = growth(st["history"], subs)
    posts = collect(a.channel, a.days)
    viewed = [p for p in posts if p["views"] is not None]
    avg = round(sum(p["views"] for p in viewed) / len(viewed), 1) if viewed else 0
    top = sorted(viewed, key=lambda p: (p["views"], p["reactions_total"]), reverse=True)[:5]
    by_type = defaultdict(lambda: {"posts": 0, "views": 0, "reactions": 0})
    for p in viewed:
        b = by_type[p["type"]]
        b["posts"] += 1; b["views"] += p["views"]; b["reactions"] += p["reactions_total"]
    types = []
    for t, b in by_type.items():
        types.append({"type": t, "posts": b["posts"], "avg_views": round(b["views"] / b["posts"], 1),
                      "avg_reactions": round(b["reactions"] / b["posts"], 2)})
    types.sort(key=lambda x: (x["avg_views"], x["avg_reactions"]), reverse=True)
    total_reacts = sum(p["reactions_total"] for p in posts)
    er = round(100 * avg / subs, 1) if subs else None

    # Russian summary
    L = [f"📊 Статистика @{a.channel} за {a.days:g} дн. ({now_msk().strftime('%d.%m.%Y')})"]
    if subs is not None:
        s = f"Подписчиков: {subs}"
        if g:
            s += f" ({'+' if g['delta'] >= 0 else ''}{g['delta']} за {g['days']:g} дн." + (", данных ещё мало" if g["partial"] else "") + ")"
            if g["partial"] and g["days"] < 1:
                s = f"Подписчиков: {subs} (первый замер, рост покажу через неделю)"
        L.append(s)
    else:
        L.append(f"Подписчиков: нет данных ({sub_err})")
    L.append(f"Постов: {len(posts)}, в среднем {(f'{avg:,.0f}'.replace(',', ' ') if avg >= 100 else f'{avg:g}')} {plural(int(avg), 'просмотр', 'просмотра', 'просмотров')} на пост"
             + (f" (~{er:g}% от подписчиков)" if er is not None else "") + f", реакций всего: {total_reacts}")
    if top:
        L.append("Топ по просмотрам:")
        for i, p in enumerate(top, 1):
            L.append(f"{i}. {p['title'][:70]} — {p['views']} 👁" + (f", {p['reactions_total']} реакц." if p["reactions_total"] else ""))
    if len(types) > 1 and len({t["avg_views"] for t in types}) > 1:
        L.append("Лучше всего заходят: " + ", ".join(f"{TYPE_RU.get(t['type'], t['type'])} ({t['avg_views']:g} 👁)" for t in types[:3]))
    elif len(types) > 1:
            L.append("Пока разницы между типами постов нет: просмотры у всех одинаковые.")
    summary = "\n".join(L)

    run = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "date": now_msk().strftime("%Y-%m-%d %H:%M"),
           "subscribers": subs, "posts": len(posts), "avg_views": avg, "reactions_total": total_reacts,
           "views_by_post": {str(p["id"]): p["views"] for p in posts}}
    if not a.no_save:
        st["history"].append({"ts": run["ts"], "date": run["date"], "subscribers": subs})
        st["history"] = st["history"][-400:]
        st["runs"] = (st.get("runs") or [])[-60:] + [run]
        save_json(STATS_FILE, st)
    if a.summary_only:
        print(summary); return
    print(json.dumps({"ok": True, "channel": a.channel, "days": a.days, "generated_msk": run["date"],
                      "subscribers": subs, "subscribers_error": sub_err, "growth": g,
                      "posts_count": len(posts), "avg_views": avg, "view_rate_pct": er,
                      "reactions_total": total_reacts, "top5": top, "by_type": types, "posts": posts,
                      "summary_ru": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
