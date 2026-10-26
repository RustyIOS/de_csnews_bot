#!/usr/bin/env python3
"""CS2 news for @de_csnews, no Grok routine. Slots 10:51, 13:51, 16:51, 19:51, 22:51 MSK.

  python3 news_auto.py --dry-run
  python3 news_auto.py                # real send only when POSTING_ENABLED is handled by gha_run

Pipeline: fetch.py (sites + Reddit) -> public RSS for core X accounts, or skip X ->
t.me/s channels with news:true in tg_channels.json. Dedup against state.json seen.
Drop match results (live/results cover those), memes, guides. Ask OpenRouter for
at most 3 Russian posts, then check_post.py and post.py (source line, duplicate
guard, 75 s gap). A dry-run writes nothing to state and sends nothing.

OPENROUTER_API_KEY is read from the environment and never printed. Free OpenRouter
is about 50 requests/day: one short prompt per slot, one retry on 429, then the
next model. The slot is marked done only after a real decision, so a 429 inside
the 20-minute catch-up can try again. Seen ids live in state.json on the state branch.
"""
import argparse, datetime, html, json, os, re, subprocess, sys, tempfile, time
import xml.etree.ElementTree as ET
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auto_lib import log, news_slot_id, run, save_state as save_auto, single_instance, state as auto_state
from common import (BASE, ITEMS_FILE, MSK, UA, load_json, load_state, mark, norm_url, now_msk,
                    save_json, save_state, similar)

MAX_POSTS = 3
MAX_CANDIDATES = 6
MODELS = ("dots-studio/dots-3-note-preview:free", "openrouter/free")
# Probe one account. If nothing answers, skip X instead of burning the slot on dead mirrors.
X_RSS = (
    "https://rss.xcancel.com/{user}/rss",
    "https://nitter.poast.org/{user}/rss",
    "https://nitter.privacydev.net/{user}/rss",
)
X_PREFER = ("HLTVorg", "CounterStrike", "ESLCS", "BLASTPremier", "Sheep_VALORANT", "Mauisnake")

RESULT_RE = re.compile(
    r"\b(defeat[s]?|defeated|beat[s]?|take[s]? down|eliminat\w*|reverse sweep|post-?match|map \d|"
    r"wins?(?: against)?|close(?:s|d)? out)\b|"
    r"\b\d{1,2}\s*[-–:]\s*\d{1,2}\b.{0,40}\b(bo[135]|mirage|inferno|nuke|dust\s?2|ancient|anubis|"
    r"overpass|train|vertigo|cache)\b|"
    r"(победил\w*|проиграл\w*|обыграл\w*|уступил\w*|счёт карты|счет карты)",
    re.I)
MEME_RE = re.compile(r"\b(meme|shitpost|giveaway|розыгрыш|промокод)\b|😂|🤣", re.I)
FLUFF_RE = re.compile(r"покушать|братишка|дайджест|мемас|жиза", re.I)
HIGHLIGHT_RE = re.compile(
    r"(отобрал\w*|забрал\w*|выиграл\w*|слил\w*).{0,40}карт|"
    r"тридцат\w*|наебашил|эйс\b|квадр\w*|клатч",
    re.I)
GUIDE_RE = re.compile(r"\b(how to|best settings|tier list|sensitivity|crosshair|lineup)\b|"
                      r"как (играть|настроить)", re.I)
NEWSY_RE = re.compile(
    r"\b(sign(?:s|ed|ing)?|bench(?:ed)?|transfer|roster|leave[sd]?|leaving|join(?:s|ed|ing)?|"
    r"contract|retire[sd]?|release[sd]?|stand-?in|kick(?:ed|s)?|ban(?:ned|s)?|valve|update|patch|"
    r"major|qualifier|lawsuit|depart\w*|appoint\w*|coach|org)\b|"
    r"(трансфер|состав|подписал|уш[её]л|ушла|бан|патч|обновлен|контракт|тренер|"
    r"травм\w*|заболел\w*|здоровь\w*|дисквал\w*|отстран\w*|ветрян\w*)",
    re.I)
SCORE_RE = re.compile(r"\b\d{1,2}\s*[:\-–]\s*\d{1,2}\b")
TAG_RE = re.compile(r"#([\w]+)", re.U)
EMOJI_RE = re.compile("[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF" "]")
DOMAIN_RE = re.compile(
    r"https?://\S+|\bt\.me/\S+|(?<![\w@#])@[A-Za-z0-9_]{3,}|"
    r"\b[\w.-]+\.(?:com|org|gg|us|ru|net|tv|io|me|co|uk|de|fr|dk|info|pro|xyz)\b",
    re.I)
SYSTEM_PROMPT = """Ты редактор канала @de_csnews. Пиши естественным русским языком CS-сообщества: без канцелярита, без калек, с нормальными падежами.
Выбери не больше 3 материалов, которые правда новость (трансфер, состав, бан, патч, турнир, организация, слова игрока). Пропусти мемы, гайды, розыгрыши и результаты матчей: счёт серии и карты пишут лайв и «Итоги дня».
На каждый выбранный материал верни пост:
- первая строка: <b>короткий заголовок</b>, не больше одного эмодзи;
- пустая строка, затем 2–4 предложения. Только факты из текста источника. Ничего не добавляй от себя. Если это слух или источник сам не уверен, начни тело со слова «По слухам».
- последняя строка: 2–4 хэштега и #de_csnews. Без ссылок, доменов и @ников. Строку «Источник» не пиши.
Ответ — только JSON без пояснений:
{"posts":[{"id":"id из списка","text":"<b>заголовок</b>\\n\\nФакт. Факт.\\n\\n#CS2 #HLTV #de_csnews"}]}
Если достойного нет: {"posts":[]}."""


def curl_text(url, timeout=8):
    try:
        p = subprocess.run(
            ["curl", "-sL", "--compressed", "--max-time", str(timeout), "--max-filesize", "400000",
             "-A", UA, "-H", "Accept-Language: en-US,en;q=0.9", url],
            capture_output=True, timeout=timeout + 3)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if p.returncode != 0 or not p.stdout:
        return ""
    return p.stdout.decode("utf-8", "replace")


def clean_rss(txt):
    txt = re.sub(r"<[^>]+>", " ", html.unescape(txt or ""))
    return re.sub(r"\s+", " ", txt).strip()


def parse_rss_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s.strip())
    except (TypeError, ValueError, OverflowError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def parse_x_rss(user, xml):
    if not xml or "<item" not in xml.lower():
        return []
    try:
        root = ET.fromstring(xml.encode("utf-8", "replace"))
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        m = re.search(r"/status(?:es)?/(\d+)", link)
        if not m:
            continue
        title = clean_rss(it.findtext("title") or "")
        desc = clean_rss(it.findtext("description") or "")
        text = desc if len(desc) >= len(title) else title
        if title and title not in text:
            text = f"{title}\n{desc}".strip()
        dt = parse_rss_date(it.findtext("pubDate") or "")
        if not dt or len(text) < 20:
            continue
        out.append({
            "id": m.group(1),
            "username": user,
            "text": text[:1000],
            "created_at": dt.astimezone(timezone.utc).isoformat(),
            "url": f"https://x.com/{user}/status/{m.group(1)}",
        })
    return out


def core_usernames():
    data = load_json(os.path.join(BASE, "x_accounts.json"), {})
    names = [a["username"] for a in data.get("accounts", []) if a.get("group") == "core" and a.get("username")]
    ordered = [u for u in X_PREFER if u in names]
    ordered += [u for u in names if u not in ordered]
    return ordered[:6]


def ingest_x(hours):
    """Public RSS only. No X API key and no Grok X tools. Failure skips X."""
    users = core_usernames()
    if not users:
        log("X: no core accounts")
        return False
    posts, base = [], None
    for tmpl in X_RSS:
        xml = curl_text(tmpl.format(user=users[0]))
        got = parse_x_rss(users[0], xml)
        if got or (xml and "<item" in xml.lower()):
            base = tmpl
            posts = got
            break
    if not base:
        log("X: public RSS did not answer; skipping X")
        return False
    for user in users[1:]:
        posts += parse_x_rss(user, curl_text(base.format(user=user)))
    if not posts:
        log(f"X: {base.split('/')[2]} answered, no posts in range")
        return True
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(posts, f)
        rc, out, err = run(["x_ingest.py", path, "--hours", str(hours)], timeout=60)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    log(f"X via public RSS: {len(posts)} raw, x_ingest rc={rc}\n{(err or '')[-400:]}")
    if rc != 0:
        log(f"X ingest failed, continuing without it: {(out or '')[-200:]}")
        return False
    return True


def keep_tg(text):
    text = (text or "").strip()
    if len(text) < 40 or MEME_RE.search(text) or FLUFF_RE.search(text) or GUIDE_RE.search(text):
        return False
    if RESULT_RE.search(text) or HIGHLIGHT_RE.search(text):
        return False
    # A short tip with no roster/patch/health word is usually a live flash or a joke.
    if not NEWSY_RE.search(text) and len(text) < 280:
        return False
    # Live score flashes ("13:8 мирж") are not news. A long post that also has a news word stays.
    if SCORE_RE.search(text) and len(text) < 180 and not NEWSY_RE.search(text):
        return False
    return True


def scan_tg(hours):
    import tg_scan
    chans = load_json(os.path.join(BASE, "tg_channels.json"), [])
    since = datetime.datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for c in chans:
        if not c.get("news"):
            continue
        try:
            posts = tg_scan.fetch(c["channel"])
        except Exception as ex:
            log(f"tg {c['channel']}: {ex}")
            continue
        for p in posts:
            if p["time"].astimezone(timezone.utc) < since or not keep_tg(p["text"]):
                continue
            mid = p["url"].rstrip("/").split("/")[-1]
            title = p["text"].split("\n", 1)[0][:140]
            out.append({
                "id": f"tg:{c['channel']}:{mid}",
                "source": f"Telegram / {c['channel']}",
                "title": title,
                "url": p["url"],
                "summary": p["text"][:600],
                "text": p["text"][:1500],
                "image": "",
                "published": p["time"].astimezone(MSK).strftime("%Y-%m-%d %H:%M MSK"),
            })
    return out


def is_junk(item):
    blob = f"{item.get('title') or ''} {item.get('summary') or ''}"
    kind = ((item.get("reddit") or {}).get("kind") or "")
    if kind in ("highlight", "discussion", "meme", "match_thread", "support", "guide"):
        return True
    if MEME_RE.search(blob) or FLUFF_RE.search(blob) or GUIDE_RE.search(blob) or HIGHLIGHT_RE.search(blob):
        return True
    if (item.get("source") or "").startswith("Telegram /") and not NEWSY_RE.search(blob) and len(blob) < 280:
        return True
    # A bare "X win Y" is a result. Keep it when the piece is really about a Major/VRS slot,
    # and let the prompt write that part without the map score.
    odds = bool(re.search(r"\b(VRS|major|qualif\w*|invite)\b", blob, re.I))
    if RESULT_RE.search(item.get("title") or "") and not (NEWSY_RE.search(blob) or odds):
        return True
    if RESULT_RE.search(blob) and not NEWSY_RE.search(blob) and not odds:
        return True
    return False


def rank(item):
    src = item.get("source") or ""
    blob = f"{item.get('title') or ''} {item.get('summary') or ''}"
    score = 0
    if src in ("HLTV", "Counter-Strike 2 (Steam)", "Dust2.us"):
        score += 3
    if src.startswith("X / @") and (item.get("x") or {}).get("group") == "core":
        score += 2
    if src.startswith("Telegram /"):
        score += 1
    if NEWSY_RE.search(blob):
        score += 2
    if src == "Reddit":
        score -= 1
    return score


def deduped(items):
    st = load_state()
    seen_ids = set(st.get("seen") or {})
    seen_urls = {norm_url(v.get("url")) for v in (st.get("seen") or {}).values()}
    seen_titles = [v.get("title") or "" for v in (st.get("seen") or {}).values()][-500:]
    out, titles, urls = [], [], set()
    for it in items:
        u = norm_url(it.get("url"))
        if not it.get("id") or it["id"] in seen_ids or u in seen_urls or u in urls:
            continue
        if any(similar(it.get("title") or "", t) for t in seen_titles + titles):
            continue
        urls.add(u)
        titles.append(it.get("title") or "")
        out.append(it)
    return out


def candidates_of(items):
    fresh = [it for it in deduped(items) if not is_junk(it)]
    fresh.sort(key=lambda it: (rank(it), it.get("published") or ""), reverse=True)
    return fresh[:MAX_CANDIDATES]


def prompt_for(items):
    blocks = []
    for it in items:
        body = (it.get("text") or it.get("summary") or "").strip()
        body = re.sub(r"\s+", " ", body)[:500]
        blocks.append(f"[{it['id']}] {it.get('source')} | {it.get('published')}\n{it.get('title')}\n{body}")
    return "Материалы:\n\n" + "\n\n".join(blocks)


def model_list():
    raw = os.environ.get("OPENROUTER_MODELS", "")
    if raw.strip():
        return tuple(m.strip() for m in raw.split(",") if m.strip())
    return MODELS


def redact(text):
    text = re.sub(r"sk-or-v1-[A-Za-z0-9_-]+", "[redacted]", text or "")
    text = re.sub(r"Bearer\s+\S+", "Bearer [redacted]", text)
    return text[:400]


def message_text(message):
    content = (message or {}).get("content")
    if isinstance(content, list):
        parts = []
        for p in content:
            parts.append(p.get("text", "") if isinstance(p, dict) else str(p))
        return "".join(parts)
    return content or ""


def chat(user_prompt):
    """One short completion. None means every model failed (caller must not mark the slot)."""
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        log("OPENROUTER_API_KEY is not set")
        return None
    import requests
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://t.me/de_csnews",
        "X-Title": "de_csnews",
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    for model in model_list():
        for attempt in (1, 2):
            try:
                r = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json={"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 1000},
                    timeout=45,
                )
            except requests.RequestException as ex:
                log(f"OpenRouter {model}: {ex.__class__.__name__}")
                break
            if r.status_code == 429 or r.status_code >= 500:
                log(f"OpenRouter {model}: HTTP {r.status_code} (try {attempt})")
                if attempt == 1:
                    time.sleep(5)
                    continue
                break
            if r.status_code != 200:
                log(f"OpenRouter {model}: HTTP {r.status_code} {redact(r.text)}")
                break
            try:
                msg = message_text(r.json()["choices"][0]["message"])
            except (ValueError, KeyError, IndexError, TypeError):
                log(f"OpenRouter {model}: unexpected body {redact(r.text)}")
                break
            if msg.strip():
                log(f"OpenRouter draft from {model} ({len(msg)} chars)")
                return msg
            log(f"OpenRouter {model}: empty content")
            break
    return None


def parse_model_posts(content):
    if not content:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    i = text.find("{")
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i:])
    except json.JSONDecodeError:
        return None
    posts = obj.get("posts") if isinstance(obj, dict) else None
    if not isinstance(posts, list):
        return None
    return posts[:MAX_POSTS]


def cyrillic_share(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    cyr = sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё")
    return cyr / len(letters)


def normalize_draft(text):
    """Shape a model draft so check_post can judge the facts, not the markup."""
    text = (text or "").replace("\\n", "\n").strip()
    text = re.sub(r"(?im)^.*источник\s*:.*$", "", text)
    text = DOMAIN_RE.sub("", text)
    text = re.sub(r"<(?!/?(?:b|i)\b)", "&lt;", text)
    lines = [re.sub(r"[ \t]+", " ", ln).rstrip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return None
    tags = []
    if lines[-1].lstrip().startswith("#"):
        tags = TAG_RE.findall(lines.pop())
    if not lines:
        return None
    # One block with several sentences: the first is the headline, the rest is the body.
    if len(lines) == 1:
        plain = re.sub(r"</?b>", "", lines[0]).strip()
        bits = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", plain) if p.strip()]
        if len(bits) >= 3:
            lines = [bits[0], " ".join(bits[1:])]
    head = lines[0]
    head = re.sub(r"</?b>", "", head).strip()
    emojis = EMOJI_RE.findall(head)
    head = EMOJI_RE.sub("", head).strip()
    if emojis:
        head = f"{head} {emojis[0]}".strip()
    body = " ".join(lines[1:] if len(lines) > 1 else [])
    body = re.sub(r"\s+", " ", body).strip()
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", body) if p.strip()]
    parts = parts[:4]
    if len(parts) < 2:
        return None
    if cyrillic_share(head + " " + " ".join(parts)) < 0.45:
        return None
    kept, seen = [], set()
    for t in tags:
        if t.lower() == "de_csnews" or t.lower() in seen:
            continue
        seen.add(t.lower())
        kept.append("#" + t)
        if len(kept) == 4:
            break
    if len(kept) < 2:
        for extra in ("#CS2", "#Новости"):
            if extra.lower() not in seen and extra[1:].lower() not in seen:
                kept.append(extra)
                seen.add(extra[1:].lower())
            if len(kept) >= 2:
                break
    kept = kept[:4]
    post = f"<b>{head}</b>\n\n" + " ".join(parts) + "\n\n" + " ".join(kept + ["#de_csnews"])
    visible = re.sub(r"<[^>]+>", "", post)
    if len(visible) > 900:
        return None
    return post


def remember_items(extra):
    """post.py looks items up in items.json. fetch.py already saved its own batch."""
    if not extra:
        return
    cache = load_json(ITEMS_FILE, {"items": []})
    cache.setdefault("items", [])
    have = {i.get("id") for i in cache["items"]}
    for it in extra:
        if it.get("id") and it["id"] not in have:
            cache["items"].append(it)
            have.add(it["id"])
    save_json(ITEMS_FILE, cache)


def publish(item, text, dry):
    os.makedirs("drafts", exist_ok=True)
    safe = re.sub(r"[^\w.-]+", "-", item["id"])[:80]
    path = os.path.join("drafts", f"news-{safe}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    check_args = [] if item.get("image") else ["--text"]
    rc, out, err = run(["check_post.py", path, *check_args], timeout=90)
    log(f"check_post {item['id']} rc={rc}\n{out}{err}")
    if rc != 0:
        return False
    base = ["post.py", item["id"], "--text-file", path]
    rc, out, err = run(base + ["--dry-run"], timeout=60)
    log(f"post.py --dry-run {item['id']} rc={rc}\n{out}{err}")
    if rc != 0 or dry:
        return rc == 0
    rc, out, err = run(base, timeout=180)
    blob = (out or "") + (err or "")
    log(f"post.py SEND {item['id']} rc={rc}\n{redact(blob)}")
    if "DUPLICATE:" in blob:
        log("DUPLICATE: counting as posted")
        return True
    return rc == 0 and '"ok": true' in (out or "")


def mark_skipped(items):
    if not items:
        return
    st = load_state()
    for it in items:
        if it["id"] not in st["seen"]:
            mark(st, it, "skipped")
    save_state(st)


def note_slot():
    slot = news_slot_id(now_msk())
    if not slot:
        return
    st = auto_state()
    slots = list(st.get("news_slots") or [])
    if slot in slots:
        return
    st["news_slots"] = (slots + [slot])[-40:]
    save_auto(st)
    log(f"news slot {slot} done")


def load_fetch(hours):
    rc, out, err = run(["fetch.py", "--hours", str(hours)], timeout=300)
    if err.strip():
        log(f"fetch.py stderr:\n{err[-500:]}")
    if rc != 0:
        log(f"fetch.py exit {rc}")
        return [], False
    try:
        return json.loads(out or "[]"), True
    except json.JSONDecodeError:
        log("fetch.py did not return JSON")
        return [], False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hours", type=float, default=12)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    lock = single_instance("news_auto")  # noqa: F841 — hold the lock for the process
    slot = news_slot_id(now_msk())
    if slot and not a.dry_run and not a.force and slot in (auto_state().get("news_slots") or []):
        log(f"news slot {slot} already served")
        return 0

    fetched, fetch_ok = load_fetch(a.hours)
    x_ok = ingest_x(a.hours)
    try:
        tips = scan_tg(a.hours)
    except Exception as ex:
        log(f"tg scan failed: {ex}")
        tips = []
        tg_ok = False
    else:
        tg_ok = True
    if not (fetch_ok or x_ok or tg_ok):
        log("every source failed; slot left open for the catch-up")
        return 1

    remember_items(tips)
    pool = list(fetched) + list(tips)
    # x_ingest appended to items.json; pull those in too (fetch.py's stdout missed them).
    for it in load_json(ITEMS_FILE, {}).get("items") or []:
        if it.get("id", "").startswith("x:"):
            pool.append(it)
    junk = [it for it in deduped(pool) if is_junk(it)]
    picked = candidates_of(pool)
    log(f"candidates {len(picked)} (junk filtered {len(junk)}, tips {len(tips)})")
    for it in picked:
        log(f"  [{it['id']}] {it.get('source')} | {it.get('title')}")
    if not picked:
        log("nothing newsworthy")
        if not a.dry_run:
            mark_skipped(junk)
            note_slot()
        return 0

    raw = chat(prompt_for(picked))
    if raw is None:
        log("no draft; slot stays open so the catch-up can retry")
        return 1 if not a.dry_run else 0
    parsed = parse_model_posts(raw)
    if parsed is None:
        log(f"model did not return JSON; slot stays open\n{redact(raw)}")
        return 1 if not a.dry_run else 0

    by_id = {it["id"]: it for it in picked}
    sent = 0
    chosen, format_fail = set(), set()
    for row in parsed:
        if not isinstance(row, dict):
            continue
        iid = str(row.get("id") or "")
        item = by_id.get(iid)
        if not item or iid in chosen:
            continue
        chosen.add(iid)
        text = normalize_draft(row.get("text") or "")
        if not text:
            log(f"drop {item['id']}: draft failed the format check")
            format_fail.add(iid)
            continue
        log(f"draft {item['id']}:\n{text}")
        if publish(item, text, a.dry_run):
            sent += 1
        # A failed send stays unseen, so a later slot can retry that story.
        if sent >= MAX_POSTS:
            break
    log(f"news posts {'shown' if a.dry_run else 'sent'}: {sent}")
    if not a.dry_run:
        # Junk, stories the model passed on, and drafts that were not Russian posts.
        # A story whose send failed is left unseen. post.py marks the ones that went out.
        mark_skipped(junk + [it for it in picked if it["id"] not in chosen or it["id"] in format_fail])
        note_slot()
    return 0


if __name__ == "__main__":
    sys.exit(main())
