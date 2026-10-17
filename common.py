"""Shared helpers for the CS2 news pipeline."""
import json, os, re, difflib, tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "state.json")
ITEMS_FILE = os.path.join(BASE, "items.json")      # cache of last fetch output (pending items)
MSK = ZoneInfo("Europe/Moscow")
UA = "Mozilla/5.0 (compatible; de_csnews-bot/1.0; +https://t.me/de_csnews)"  # full browser UAs via curl get 403 from HLTV/Cloudflare
CHANNEL_ID = "-1003635521376"   # @de_csnews


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_state():
    st = load_json(STATE_FILE, {})
    st.setdefault("seen", {})
    return st


def save_state(st):
    save_json(STATE_FILE, st)


def norm_url(u):
    u = (u or "").strip().split("#")[0]
    u = re.sub(r"^https?://(www\.)?", "", u)
    u = re.sub(r"[?].*$", "", u)
    return u.rstrip("/").lower()


def norm_title(t):
    t = (t or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def similar(a, b, thr=0.85):
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= thr


def now_msk():
    return datetime.now(timezone.utc).astimezone(MSK)


def mark(st, item, status):
    """Record an item as seen (status: posted / skipped / seeded)."""
    st["seen"][item["id"]] = {
        "status": status,
        "title": item.get("title", ""),
        "url": item.get("url", ""),
        "source": item.get("source", ""),
        "marked_at": now_msk().isoformat(timespec="seconds"),
    }
    # keep state bounded
    if len(st["seen"]) > 3000:
        items = sorted(st["seen"].items(), key=lambda kv: kv[1].get("marked_at", ""))
        st["seen"] = dict(items[-2000:])

# Match polls (sendPoll) and poll_result posts. Disabled 14.10.2026 at the user's request.
# Re-enable: set POLLS_ENABLED=True here (or env POLLS_ENABLED=1 for a single run).
POLLS_ENABLED = os.environ.get("POLLS_ENABLED", "0").lower() in ("1", "true", "yes")
