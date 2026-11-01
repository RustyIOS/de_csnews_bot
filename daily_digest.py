"""Append one deterministic line-block per confirmed channel post.

The file is daily/YYYY-MM-DD.md (Europe/Moscow). Another reader can open the
raw file on the state branch; nothing here calls a model.

    # YYYY-MM-DD

    ## HH:MM · type · t.me/de_csnews/N
    - Тема: <first line of the post>
    - Факты: турнир: …; стадия: …; команды: A — B; формат: bo3; счёт: 2:1; карты: Mirage 13:9; игроки: nick
    - Источники: https://…, https://…

`type` is one of: news, announce, live, result, day recap.
Empty facts or sources are an em dash. Several matches in one post are
joined with ` | `. Only fields already present in the caption, the draft
JSON next to the text file, or the item metadata are used.
"""
import fcntl
import html
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

from common import BASE, CHANNEL_ID, MSK, now_msk

CHANNEL_USER = "de_csnews"
KEEP_DAYS = 30
DATE_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
HEADING = re.compile(r"(?m)^## \d{2}:\d{2} · .+ · t\.me/de_csnews/(\d+)\s*$")
HREF_RE = re.compile(r"""href\s*=\s*["'](https?://[^"']+)["']""", re.I)
BARE_RE = re.compile(r"https?://[^\s<>\"')\]]+")
VS_RE = re.compile(
    r"^(?:(?P<time>\d{1,2}:\d{2})\s+)?(?P<t1>.+?)\s+vs\s+(?P<t2>.+?)(?:,\s*(?P<rest>.*))?$",
    re.I,
)
SERIES_RE = re.compile(
    r"^(?P<t1>.+?)\s+(?P<score>\d{1,2}:\d{1,2})\s+(?P<t2>.+?)(?:\s+\((?P<maps>[^)]*)\))?\s*$"
)
START_RE = re.compile(r"^Матч начался:\s*(?P<t1>.+?)\s+[—–\-]\s+(?P<t2>.+?)\s*$", re.I)
MAP_RE = re.compile(r"([A-Za-z][A-Za-z0-9.]*)\s+(\d{1,2}:\d{1,2})")
STAGE_HINT = re.compile(
    r"(группо|группа\s+[A-Za-z0-9]|гранд|полуфинал|четвертьфинал|финал|верхн\w*\s+сетк|нижн\w*\s+сетк|"
    r"матч за 3|group\s+[A-Za-z0-9]|grand\s*final|semi-?final|quarter-?final|upper|lower)",
    re.I,
)
NICK_RE = re.compile(r"(?i)эйс\s+([A-Za-z0-9][A-Za-z0-9_.-]{1,24})")
NICK_KEYS = {"player", "nickname", "nick", "player_nickname"}
URL_KEYS = {"url", "liquipedia_url", "source_url", "link"}
SKIP_KEYS = {"image", "logo", "photo", "thumbnail", "summary", "text", "title", "caption"}
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".mp4")


def digest_dir(directory=None):
    if directory:
        return directory
    return os.environ.get("DIGEST_DIR") or os.path.join(BASE, "daily")


def _dry(dry_run):
    if dry_run:
        return True
    if os.environ.get("GHA_DRY_RUN") == "true":
        return True
    if os.environ.get("DRY_RUN") == "1":
        return True
    return False


def _is_channel(chat, msg_chat=None):
    found = []

    def add(value):
        if value is None:
            return
        if isinstance(value, dict):
            add(value.get("id"))
            add(value.get("username"))
            return
        found.append(str(value).strip().lstrip("@"))

    add(chat)
    add(msg_chat)
    allowed = {str(CHANNEL_ID), CHANNEL_USER}
    return any(v in allowed for v in found)


def strip_html(text):
    plain = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(plain).replace("\xa0", " ")


def _one(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def topic_from(body, title):
    chosen = ""
    for line in strip_html(body).splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.lower().startswith("источник:"):
            continue
        chosen = s
        break
    if not chosen:
        chosen = (title or "").strip()
    chosen = re.sub(r"^[^\w]+", "", chosen, flags=re.UNICODE).strip()
    chosen = _one(chosen)
    if len(chosen) > 240:
        chosen = chosen[:239].rstrip() + "…"
    return chosen or "—"


def detect_type(item_id, body):
    iid = item_id or ""
    if iid.startswith("ann-"):
        return "announce"
    if iid.startswith("res-"):
        return "day recap"
    if iid.startswith("live-start-"):
        return "live"
    if iid.startswith("live-end-"):
        return "result"
    head = ""
    for line in strip_html(body).splitlines():
        s = line.strip()
        if s:
            head = s
            break
    low = head.lower()
    if "итоги дня" in low:
        return "day recap"
    if "матчи дня" in low:
        return "announce"
    if "матч начался" in low:
        return "live"
    plain_head = re.sub(r"^[^\w]+", "", head).strip()
    if plain_head and not re.search(r"[а-яА-Я]", plain_head) and len(plain_head) < 80:
        if SERIES_RE.match(plain_head) and "http" not in low:
            return "result"
    return "news"


def resolve_when(when, telegram_date):
    if when is not None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=MSK)
        return when.astimezone(MSK)
    if telegram_date is not None and telegram_date != "":
        return datetime.fromtimestamp(int(telegram_date), tz=timezone.utc).astimezone(MSK)
    return now_msk()


def _sidecar(text_file):
    if not text_file:
        return None
    root, ext = os.path.splitext(text_file)
    path = (root + ".json") if ext.lower() == ".txt" else (text_file + ".json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _map_list(maps):
    out = []
    for mp in maps or []:
        if isinstance(mp, str):
            text = _one(mp)
            if text:
                out.append(text)
        elif isinstance(mp, dict):
            name = _one(mp.get("map"))
            score = _one(mp.get("score"))
            if name and score:
                out.append(f"{name} {score}")
            elif score:
                out.append(score)
    return out


def _match_dict(raw, event=None):
    if not isinstance(raw, dict):
        return None
    team1 = _one(raw.get("team1"))
    team2 = _one(raw.get("team2"))
    if not team1 and not team2:
        return None
    stage = _one(raw.get("stage") or raw.get("stage_short"))
    fmt = _one(raw.get("format"))
    if re.fullmatch(r"bo[135]", fmt, re.I):
        fmt = fmt.lower()
    return {
        "event": _one(raw.get("event") or event) or None,
        "stage": stage or None,
        "team1": team1 or None,
        "team2": team2 or None,
        "format": fmt or None,
        "score": _one(raw.get("score")) or None,
        "maps": _map_list(raw.get("maps")),
    }


def matches_from_meta(meta):
    if not isinstance(meta, dict):
        return []
    if isinstance(meta.get("groups"), list):
        out = []
        for group in meta["groups"]:
            if not isinstance(group, dict):
                continue
            for row in group.get("matches") or []:
                match = _match_dict(row, group.get("event"))
                if match:
                    out.append(match)
        return out
    if isinstance(meta.get("matches"), list):
        out = []
        for row in meta["matches"]:
            event = row.get("event") if isinstance(row, dict) else None
            match = _match_dict(row, event)
            if match:
                out.append(match)
        if out:
            return out
    one = _match_dict(meta)
    return [one] if one else []


def _content_lines(plain):
    lines = []
    for line in (plain or "").splitlines():
        s = _one(line)
        if not s or s.startswith("#") or s.lower().startswith("источник:"):
            continue
        lines.append(s)
    return lines


def _team_ok(name):
    name = (name or "").strip(" ,;")
    if not name or not re.match(r"[A-Za-zА-Яа-яЁё0-9]", name):
        return False
    if re.search(r"\d{1,2}:\d{1,2}", name):
        return False
    return True


def _split_format_stage(rest):
    if not rest:
        return None, None
    fmt, stages = None, []
    for part in [p.strip() for p in rest.split(",") if p.strip()]:
        if re.fullmatch(r"bo[135]", part, re.I):
            fmt = part.lower()
        elif STAGE_HINT.search(part):
            stages.append(part)
    return fmt, ", ".join(stages) or None


def _parse_maps(blob):
    named = []
    for name, score in MAP_RE.findall(blob or ""):
        if name.lower() in {"bo1", "bo3", "bo5"}:
            continue
        named.append(f"{name} {score}")
    if named:
        return named
    if blob and re.fullmatch(r"[\d:,\s]+", blob):
        return re.findall(r"\d{1,2}:\d{1,2}", blob)
    return []


def _parse_meta(line):
    if re.search(r"\d+:\d+", line):
        return None
    fmt, stages = None, []
    parts = [p.strip() for p in line.split(",") if p.strip()]
    if not parts:
        return None
    for part in parts:
        if re.fullmatch(r"bo[135]", part, re.I):
            fmt = part.lower()
        elif STAGE_HINT.search(part):
            stages.append(part)
        else:
            return None
    if not fmt and not stages:
        return None
    return fmt, ", ".join(stages) or None


def parse_text_matches(plain):
    """Pull tournament / teams / scores out of the captions this repo already writes."""
    current = None
    pending = []
    out = []

    def take(match):
        if current:
            match["event"] = match.get("event") or current
            out.append(match)
        else:
            pending.append(match)

    def last():
        if pending:
            return pending[-1]
        if out:
            return out[-1]
        return None

    for line in _content_lines(plain):
        if "🏆" in line:
            current = _one(re.sub(r"^[^\w]+", "", line.split("🏆", 1)[1]))
            if pending and current:
                for match in pending:
                    match["event"] = current
                out.extend(pending)
                pending = []
            continue
        stripped = _one(re.sub(r"^[^\w]+", "", line))
        started = START_RE.match(stripped) or START_RE.match(line)
        if started:
            take({
                "team1": _one(started.group("t1")),
                "team2": _one(started.group("t2")),
                "event": current,
                "maps": [],
            })
            continue
        versus = VS_RE.match(stripped)
        if versus and _team_ok(versus.group("t1")) and _team_ok(versus.group("t2")):
            fmt, stage = _split_format_stage(versus.group("rest"))
            take({
                "team1": _one(versus.group("t1")),
                "team2": _one(versus.group("t2")),
                "format": fmt,
                "stage": stage,
                "event": current,
                "maps": [],
            })
            continue
        series = SERIES_RE.match(stripped)
        if series and _team_ok(series.group("t1")) and _team_ok(series.group("t2")):
            take({
                "team1": _one(series.group("t1")),
                "team2": _one(series.group("t2")),
                "score": series.group("score"),
                "maps": _parse_maps(series.group("maps") or ""),
                "event": current,
            })
            continue
        maps = _parse_maps(stripped)
        if maps:
            row = last()
            if row is not None:
                row.setdefault("maps", [])
                row["maps"] = list(row["maps"]) + maps
            continue
        meta = _parse_meta(stripped)
        if meta:
            row = last()
            if row is not None:
                fmt, stage = meta
                if fmt and not row.get("format"):
                    row["format"] = fmt
                if stage and not row.get("stage"):
                    row["stage"] = stage
    if pending:
        out.extend(pending)
    return out


def _dedupe(items):
    out, seen = [], set()
    for item in items:
        key = item.casefold()
        if not item or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def collect_nicks(meta, body):
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in NICK_KEYS and isinstance(value, str) and value.strip():
                    found.append(value.strip())
                elif key == "players" and isinstance(value, list):
                    for player in value:
                        if isinstance(player, str) and player.strip():
                            found.append(player.strip())
                        else:
                            walk(player)
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(meta)
    found.extend(NICK_RE.findall(strip_html(body or "")))
    return _dedupe([_one(n) for n in found])


def render_facts(matches, nicks):
    chunks = []
    for match in matches:
        bits = []
        event = _one(match.get("event"))
        stage = _one(match.get("stage"))
        team1 = _one(match.get("team1"))
        team2 = _one(match.get("team2"))
        fmt = _one(match.get("format"))
        if re.fullmatch(r"bo[135]", fmt, re.I):
            fmt = fmt.lower()
        score = _one(match.get("score"))
        maps = [_one(m) for m in (match.get("maps") or []) if _one(m)]
        if event:
            bits.append(f"турнир: {event}")
        if stage:
            bits.append(f"стадия: {stage}")
        if team1 and team2:
            bits.append(f"команды: {team1} — {team2}")
        elif team1 or team2:
            bits.append(f"команды: {team1 or team2}")
        if fmt:
            bits.append(f"формат: {fmt}")
        if score:
            bits.append(f"счёт: {score}")
        if maps:
            bits.append("карты: " + ", ".join(maps))
        if bits:
            chunks.append("; ".join(bits))
    nick_txt = ", ".join(nicks)
    if nick_txt:
        piece = f"игроки: {nick_txt}"
        if len(chunks) == 1:
            chunks[0] += "; " + piece
        elif chunks:
            chunks.append(piece)
        else:
            chunks.append(piece)
    return " | ".join(chunks) if chunks else "—"


def _keep_url(url):
    if not isinstance(url, str):
        return None
    url = html.unescape(url).strip().rstrip(".,);")
    if not re.match(r"https?://", url, re.I):
        return None
    base = url.split("?", 1)[0].split("#", 1)[0].lower()
    if base.endswith(IMAGE_EXT):
        return None
    hostpath = re.sub(r"^https?://", "", base, flags=re.I).rstrip("/")
    if hostpath == "t.me/de_csnews" or hostpath.startswith("t.me/de_csnews/"):
        return None
    if hostpath == "t.me/s/de_csnews" or hostpath.startswith("t.me/s/de_csnews/"):
        return None
    return url


def _urls_in(obj):
    found = []

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                name = str(key).lower()
                if name in SKIP_KEYS or name.endswith(("_logo", "_image", "_thumbnail")):
                    continue
                if name in URL_KEYS and isinstance(item, str):
                    found.append(item)
                elif name == "links" and isinstance(item, list):
                    for link in item:
                        if isinstance(link, str):
                            found.append(link)
                        else:
                            walk(link)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return found


def collect_sources(item, body, meta):
    raw = []
    raw.extend(_urls_in(item or {}))
    raw.extend(_urls_in(meta or {}))
    raw.extend(HREF_RE.findall(body or ""))
    raw.extend(BARE_RE.findall(strip_html(body or "")))
    out, seen = [], set()
    for url in raw:
        cleaned = _keep_url(url)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return ", ".join(out) if out else "—"


def render_entry(moment, kind, message_id, topic, facts, sources):
    return (
        f"## {moment:%H:%M} · {kind} · t.me/de_csnews/{message_id}\n"
        f"- Тема: {topic}\n"
        f"- Факты: {facts}\n"
        f"- Источники: {sources}\n"
    )


def find_message(directory, message_id):
    if not directory or not os.path.isdir(directory):
        return None
    mid = str(int(message_id))
    for name in sorted(os.listdir(directory)):
        if not DATE_FILE.match(name):
            continue
        path = os.path.join(directory, name)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for found in HEADING.findall(text):
            if found == mid:
                return path
    return None


def prune(directory, today=None, keep_days=KEEP_DAYS):
    """Delete daily/YYYY-MM-DD.md strictly older than `keep_days` before `today`."""
    if not directory or not os.path.isdir(directory):
        return []
    today = today or now_msk().date()
    cutoff = today - timedelta(days=keep_days)
    removed = []
    for name in sorted(os.listdir(directory)):
        match = DATE_FILE.match(name)
        if not match:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            os.remove(os.path.join(directory, name))
            removed.append(name)
    return removed


def _write(path, content):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Lock:
    def __init__(self, path):
        self.path = path
        self.f = None

    def __enter__(self):
        self.f = open(self.path, "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc):
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        finally:
            self.f.close()


def record_sent(*, message_id, chat, item_id, body, item=None, text_file=None,
                when=None, telegram_date=None, msg_chat=None, dry_run=False,
                directory=None):
    """Append one digest entry. Dry-runs and non-channel chats write nothing.

    The same message_id is never written twice. Returns the markdown path, or
    None when nothing was written.
    """
    if _dry(dry_run):
        return None
    if not _is_channel(chat, msg_chat):
        return None
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return None
    if mid <= 0:
        return None

    directory = digest_dir(directory)
    os.makedirs(directory, exist_ok=True)
    moment = resolve_when(when, telegram_date)
    day = moment.date()
    meta = _sidecar(text_file)
    kind = detect_type(item_id, body)
    matches = matches_from_meta(meta)
    if not matches and kind != "news":
        matches = parse_text_matches(strip_html(body))
    facts = render_facts(matches, collect_nicks(meta, body))
    sources = collect_sources(item, body, meta)
    title = (item or {}).get("title") if isinstance(item, dict) else ""
    entry = render_entry(moment, kind, mid, topic_from(body, title), facts, sources)
    path = os.path.join(directory, f"{day.isoformat()}.md")

    with _Lock(directory.rstrip("/") + ".lock"):
        existing_path = find_message(directory, mid)
        if existing_path:
            return existing_path
        prune(directory, today=day)
        previous = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                previous = f.read()
        if not previous.strip():
            content = f"# {day.isoformat()}\n\n{entry}"
        else:
            if not previous.endswith("\n"):
                previous += "\n"
            content = previous + "\n" + entry
        _write(path, content)
    return path
