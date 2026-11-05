"""Same CS story from two sources, without a model.

news_auto used to keep both a Dust2 piece and a later Reddit post when the
English titles were only ~40% alike and the URLs differed. post.py then looked
back three hours and only matched an identical first line, or a shared score
plus two latin words. News headlines have no score, and the repeats were 6–15
hours apart.

This compares the words that survive translation: org and player names, plus
a few concepts that are the same fact in either language (major, patch, debt,
half, win). A clear overlap blocks the later copy for 48 hours. A borderline
pair can ask Gemini for one yes/no; no key, or any error, leaves it alone.
"""
import html
import os
import re
from datetime import datetime, timedelta

from common import BASE, CHANNEL_ID, MSK, load_json, now_msk

NEWS_WINDOW_HOURS = 48
SENT_LOG = os.path.join(BASE, "sent_log.json")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIE_MODEL = "gemini-3.1-flash-lite"

# Function words and fillers. Dropped before any comparison.
_STOP = {
    "a", "an", "the", "of", "to", "and", "or", "for", "in", "on", "at", "by", "with",
    "from", "if", "they", "their", "them", "his", "her", "its", "our", "it", "is",
    "are", "was", "were", "be", "been", "that", "this", "as", "after", "before",
    "about", "into", "over", "under", "than", "then", "out", "up", "not", "no",
    "just", "also", "has", "have", "had", "each", "would", "will", "can", "could",
    "who", "what", "when", "where", "while", "via", "vs", "per", "you", "your",
    "but", "so", "do", "did", "does", "only", "more", "most", "some", "such",
    "other", "another", "own", "same", "all", "any", "how", "why", "too", "very",
    "really", "still", "even", "here", "there", "now", "back", "away", "off",
    "down", "again", "during", "without", "within", "across", "among", "between",
    "because", "since", "until", "onto", "upon", "team", "teams", "player",
    "players", "owner", "owners", "said", "says", "say", "first", "second",
    "third", "new", "news", "today", "yesterday", "tomorrow", "already", "around",
    "been", "our", "of", "ceo", "his", "her", "them", "their", "they", "and",
    "the", "for", "its", "was", "are", "but", "not", "you", "our", "out",
    "и", "в", "во", "на", "за", "с", "со", "по", "о", "об", "от", "для", "из",
    "к", "ко", "что", "это", "как", "не", "а", "но", "или", "же", "ли", "бы",
    "уже", "еще", "ещё", "около", "после", "перед", "при", "без", "до", "над",
    "под", "все", "всё", "его", "ее", "её", "их", "он", "она", "они", "мы", "вы",
    "так", "то", "вот", "где", "когда", "если", "чем", "кто", "этот", "эта",
    "эти", "был", "была", "было", "были", "будет", "только", "между", "через",
    "более", "очень", "также", "тоже", "уже", "ещё", "при", "около",
    "новый", "новая", "новое", "новые", "вышло", "вышел", "вышла", "вышли",
    "сегодня", "вчера", "знакомьтесь",
}
# Latin (or folded) tokens that are concepts, not a club or a person.
_NOT_NAMES = {
    "патч", "мейджор", "долг", "половин", "побед", "обещ", "анонс", "полуфинал",
    "сингапур", "талант", "выплат", "погас", "cs", "cs2", "csgo",
    "counter-strike", "counterstrike", "qualified", "qualifies", "qualification",
    "game", "games", "pro", "official", "roster", "coach", "update", "updated",
    "updates", "patch", "patches", "season", "list", "gift", "car", "cars",
    "luxury", "major", "majors", "half", "halves", "debt", "debts", "win",
    "wins", "won", "promise", "promised", "promises", "paid", "talent",
    "talents", "announced", "announcement", "join", "joins", "joined", "beat",
    "beats", "sign", "signs", "signed", "leave", "leaves", "left", "get",
    "gets", "got", "make", "makes", "made", "give", "gives", "gave", "take",
    "takes", "took", "after", "before", "over", "into", "from", "with", "have",
    "has", "had", "been", "being", "were", "was", "its", "global", "offensive",
    "counter", "strike", "time", "times", "day", "days", "ago", "next", "last",
    "week", "year", "years", "top", "best", "good", "big", "old", "more",
    "less", "than", "just", "only", "also", "still", "even", "really", "very",
    "ceo", "our", "already", "around", "each", "both", "per", "via",
}
_SHORT_ORGS = {"g2", "vp", "b8", "nip", "pgl", "iem", "esl", "lg", "sk", "c9"}
_SYN = {
    "patch": "патч", "patches": "патч", "update": "патч", "updates": "патч",
    "updated": "патч", "major": "мейджор", "majors": "мейджор",
    "debt": "долг", "debts": "долг", "half": "половин", "halves": "половин",
    "talent": "талант", "talents": "талант", "singapore": "сингапур",
    "semis": "полуфинал", "semifinal": "полуфинал", "semi-final": "полуфинал",
    "win": "побед", "wins": "побед", "won": "побед",
    "promise": "обещ", "promised": "обещ", "promises": "обещ",
    "paid": "выплат",
}
# Longest-prefix wins, so "обновление" and "пообещал" collapse with their English twins.
_PREFIX = (
    ("обновлен", "патч"),
    ("announc", "анонс"),
    ("анонс", "анонс"),
    ("полуфинал", "полуфинал"),
    ("пообещ", "обещ"),
    ("обеща", "обещ"),
    ("побед", "побед"),
    ("выплат", "выплат"),
    ("погас", "погас"),
)
_RU_ENDINGS = (
    "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими",
    "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю", "ой", "ей", "ый", "ий",
    "ых", "их", "ом", "ем", "ах", "ях", "ов", "ев", "ам", "ям",
    "а", "я", "у", "ю", "ы", "и", "е", "о",
)
_HEAD = re.compile(r"^## (\d{2}):(\d{2}) · (.+?) · t\.me/de_csnews/(\d+)\s*$")
_TOPIC = re.compile(r"^- Тема:\s*(.+?)\s*$")
_YEAR = re.compile(r"20[0-3]\d$")


def sent_log_path():
    override = (os.environ.get("SENT_LOG") or "").strip()
    return override or SENT_LOG


def digest_directory():
    return os.environ.get("DIGEST_DIR") or os.path.join(BASE, "daily")


def _stem_ru(tok):
    if not re.search(r"[а-я]", tok):
        return tok
    for end in _RU_ENDINGS:
        if tok.endswith(end) and len(tok) - len(end) >= 4:
            return tok[: -len(end)]
    return tok


def _fold(tok):
    if "-" in tok and tok.endswith("s") and not tok.endswith("ss"):
        tok = tok[:-1]
    mapped = _SYN.get(tok)
    if mapped:
        return mapped
    for prefix, canon in _PREFIX:
        if tok.startswith(prefix):
            return canon
    stemmed = _stem_ru(tok)
    mapped = _SYN.get(stemmed)
    if mapped:
        return mapped
    for prefix, canon in _PREFIX:
        if stemmed.startswith(prefix):
            return canon
    return stemmed


def content_tokens(text):
    """Content words in stable order. Names stay; fillers and years do not."""
    raw = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    raw = raw.lower().replace("ё", "е")
    raw = raw.replace("–", " ").replace("—", " ").replace("−", " ")
    raw = re.sub(r"https?://\S+", " ", raw)
    raw = re.sub(r"[^0-9a-zа-я.\-]+", " ", raw)
    out, seen = [], set()
    for bit in raw.split():
        tok = bit.strip(".-")
        if not tok or tok in _STOP:
            continue
        tok = _fold(tok)
        if not tok or tok in _STOP:
            continue
        if _YEAR.match(tok) or re.fullmatch(r"\d{1,2}", tok):
            continue
        if len(tok) < 3 and tok not in _SHORT_ORGS and not any(c.isdigit() for c in tok):
            continue
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _is_entity(tok):
    if tok in _NOT_NAMES or tok in _STOP:
        return False
    if tok in _SHORT_ORGS:
        return True
    if any(c.isdigit() for c in tok) and re.search(r"[a-z]", tok):
        return True
    if "-" in tok and re.search(r"[a-z]", tok):
        return True
    if re.search(r"[a-z]", tok) and len(tok) >= 4:
        return True
    return False


def _analyze(text):
    toks = content_tokens(text)
    names = {t for t in toks if _is_entity(t)}
    return set(toks), names


def story_relation(a, b):
    """'yes', 'no', or 'maybe' for two headlines or source titles.

    'yes' is a clear same-fact match. 'maybe' shares names but not enough of
    the fact for a hard block. 'no' is a different story, including two
    headlines that each name someone the other does not (NAVI won X vs Spirit
    won X).
    """
    ta, na = _analyze(a)
    tb, nb = _analyze(b)
    if not ta or not tb:
        return "no"
    shared = ta & tb
    ents = shared & na & nb
    # Each side names a club or player the other does not: NAVI won X vs Spirit won X.
    # An extra name on only one side is fine (the longer headline just says more).
    if (na - shared) and (nb - shared):
        return "no"
    if len(shared) < 2:
        return "no"
    shorter = ta if len(ta) <= len(tb) else tb
    longer_len = len(ta) + len(tb) - len(shorter)
    cov_s = len(shared) / len(shorter)
    cov_l = len(shared) / longer_len
    anchored = (
        len(ents) >= 1
        and len(shared - (na | nb)) >= 1
        and cov_s + 1e-9 >= 0.6
    )
    paraphrase = (
        2 <= len(shorter) <= 5
        and 2 <= max(len(ta), len(tb)) <= 5
        and cov_s + 1e-9 >= 0.75
        and cov_l + 1e-9 >= 0.75
        and len(shared) >= 2
        and na <= shared
        and nb <= shared
    )
    if anchored or paraphrase:
        return "yes"
    if len(ents) >= 1 and len(shared) >= 2 and cov_s + 1e-9 >= 0.45:
        return "maybe"
    return "no"


def best_match(text, others, tie_break=None):
    """Headline from `others` that is the same story, or None.

    A 'yes' wins without calling `tie_break`. One 'maybe' is passed to
    `tie_break(text, other)` and blocks only when that returns true.
    """
    maybe = None
    for other in others:
        if not (other or "").strip():
            continue
        rel = story_relation(text, other)
        if rel == "yes":
            return other
        if rel == "maybe" and maybe is None:
            maybe = other
    if maybe is not None and tie_break is not None:
        try:
            if tie_break(text, maybe):
                return maybe
        except Exception:
            return None
    return None


def headline_of(body):
    """First line of a channel post, without tags or the source line."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", body or ""))
    for line in plain.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.lower().startswith("источник:"):
            continue
        return s
    return ""


def digest_topics(now=None, hours=NEWS_WINDOW_HOURS, directory=None):
    """News «Тема:» lines from daily/YYYY-MM-DD.md inside the window."""
    now = now or now_msk()
    if now.tzinfo is None:
        now = now.replace(tzinfo=MSK)
    else:
        now = now.astimezone(MSK)
    directory = directory or digest_directory()
    if not directory or not os.path.isdir(directory):
        return []
    cutoff = now - timedelta(hours=hours)
    found = []
    for delta in range(4):
        day = (now.date() - timedelta(days=delta))
        path = os.path.join(directory, day.strftime("%Y-%m-%d") + ".md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            m = _HEAD.match(line.strip())
            if not m or i + 1 >= len(lines):
                continue
            if m.group(3).strip() != "news":
                continue
            tm = _TOPIC.match(lines[i + 1].strip())
            if not tm:
                continue
            topic = tm.group(1).strip()
            if not topic or topic == "—":
                continue
            moment = datetime(
                day.year, day.month, day.day,
                int(m.group(1)), int(m.group(2)), tzinfo=MSK,
            )
            if cutoff <= moment <= now + timedelta(minutes=5):
                found.append(topic)
    return found


def recent_seen_titles(seen, now=None, hours=NEWS_WINDOW_HOURS):
    """English source titles of posts we marked posted inside the window."""
    now = now or now_msk()
    if now.tzinfo is None:
        now = now.replace(tzinfo=MSK)
    cutoff = now - timedelta(hours=hours)
    out = []
    for rec in (seen or {}).values():
        if not isinstance(rec, dict) or rec.get("status") != "posted":
            continue
        title = (rec.get("title") or "").strip()
        raw = rec.get("marked_at") or ""
        if not title or not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MSK)
        if dt >= cutoff:
            out.append(title)
    return out


def sent_heads(chat, now_epoch=None, hours=NEWS_WINDOW_HOURS, log=None):
    """First lines of sent_log rows for this chat inside the window."""
    import time
    now_epoch = time.time() if now_epoch is None else now_epoch
    records = load_json(sent_log_path(), []) if log is None else log
    out = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if chat is not None and str(rec.get("chat")) != str(chat):
            continue
        try:
            age = now_epoch - float(rec.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if age > hours * 3600 or age < -60:
            continue
        first = (rec.get("first") or "").strip()
        if first:
            out.append(first)
    return out


def recent_published_texts(chat=CHANNEL_ID, now=None, now_epoch=None, hours=NEWS_WINDOW_HOURS,
                           directory=None, log=None, seen=None):
    """Russian headlines we already published, plus recent posted source titles."""
    texts = []
    texts.extend(digest_topics(now=now, hours=hours, directory=directory))
    texts.extend(sent_heads(chat, now_epoch=now_epoch, hours=hours, log=log))
    if seen is not None:
        texts.extend(recent_seen_titles(seen, now=now, hours=hours))
    return texts


def parse_yes_no(text):
    words = re.sub(r"[^a-zа-я]+", " ", (text or "").lower().replace("ё", "е")).split()
    if not words:
        return None
    word = words[0]
    if word in {"да", "yes", "true", "same"}:
        return True
    if word in {"нет", "no", "false", "different"}:
        return False
    return None


_tie_cache = {}


def gemini_same_story(a, b):
    """One cheap yes/no. None when there is no key or the call does not answer.

    The key goes in the header, never the URL. Failures do not block a post.
    """
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        return None
    cache_key = (a[:300], b[:300])
    if cache_key in _tie_cache:
        return _tie_cache[cache_key]
    model = (os.environ.get("GEMINI_DEDUP_MODEL") or TIE_MODEL).strip() or TIE_MODEL
    prompt = (
        "Два заголовка CS2. Это один и тот же факт, а не просто общая команда, "
        "турнир или слово? Ответь одним словом: да или нет.\n"
        f"1. {a[:300]}\n2. {b[:300]}"
    )
    result = None
    try:
        import requests
        from urllib.parse import quote
        url = GEMINI_URL.format(model=quote(model, safe=""))
        r = requests.post(
            url,
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 16},
            },
            timeout=15,
        )
        if r.status_code == 200:
            body = r.json()
            parts = (((body.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
            texts = []
            for part in parts:
                if isinstance(part, dict) and not part.get("thought") and isinstance(part.get("text"), str):
                    texts.append(part["text"])
            result = parse_yes_no("".join(texts))
    except Exception:
        result = None
    _tie_cache[cache_key] = result
    return result


def default_tie_break():
    if not (os.environ.get("GEMINI_API_KEY") or "").strip():
        return None
    return gemini_same_story
