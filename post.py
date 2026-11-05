#!/usr/bin/env python3
"""Post one news item to the Telegram channel, then mark it seen in state.json.

Usage:
  python3 post.py ITEM_ID --text-file post.txt [--dry-run] [--chat @test_or_id] [--no-mark]
  echo "<b>Заголовок</b>\\n\\nТекст" | python3 post.py ITEM_ID [--dry-run]

Text is Telegram HTML (<b>, <i>, <a href>...). The source line
'<i>Источник: SOURCE</i>' (no link) is appended automatically.
Item data (url/source/image) comes from items.json (written by fetch.py);
override with --url / --source / --image ('' to force a text-only post).
--image accepts a URL or a LOCAL file path (e.g. a card from card.py) - local files are uploaded.
Photo post: sendPhoto (caption <=1024 visible chars) -> retry with uploaded file ->
fallback sendMessage. Text-only: sendMessage (<=4096).
Token is read from env TELEGRAM_BOT_TOKEN and never printed.
A confirmed channel send also appends one daily/YYYY-MM-DD.md entry (see daily_digest.py).
Dry-run returns before that write.
"""
import argparse, html, json, os, re, sys, subprocess, tempfile
import requests
from common import (ITEMS_FILE, CHANNEL_ID, UA, load_json, load_state, save_state, mark)
from daily_digest import record_sent
from story_dedup import (
    NEWS_WINDOW_HOURS, best_match, default_tie_break, digest_topics, headline_of,
    recent_seen_titles, sent_heads, sent_log_path,
)

SOURCE_LABELS = {"Counter-Strike 2 (Steam)": "Steam / Valve"}


def visible_len(h):
    return len(html.unescape(re.sub(r"<[^>]+>", "", h)))


def tg(method, token, data, files=None):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=60)
    try:
        j = r.json()
    except Exception:
        j = {"ok": False, "description": f"HTTP {r.status_code}"}
    return j


def clean_label(label):
    """Plain-text source name that Telegram won't auto-link (no domains, no @mentions)."""
    import re as _re
    X_NAMES = {"hltvorg": "HLTV", "counterstrike": "Counter-Strike", "eslcs": "ESL", "blastpremier": "BLAST",
               "pglesports": "PGL", "teamvitalitycs": "Vitality", "mousesports": "MOUZ", "team__spirit": "Team Spirit",
               "natusvincere": "NAVI", "fazeesports": "FaZe", "g2csgo": "G2", "furia": "FURIA",
               "falconsesport": "Falcons", "auroracs2_gg": "Aurora", "1mongolz": "The MongolZ",
               "parivisiongg": "PARIVISION", "astraliscs": "Astralis", "teamliquidcs": "Team Liquid",
               "heroicgg": "HEROIC", "3dmaxgaming": "3DMAX", "mibr": "MIBR", "gamerlegion": "GamerLegion",
               "nipcs": "NIP", "b8esportsgg": "B8", "virtuspro": "Virtus.pro", "eternalfiregg": "Eternal Fire",
               "mauisnake": "Mauisnake", "thorin": "Thorin", "sheep_valorant": "Sheep"}
    m = _re.match(r"^X\s*/\s*@?(\w+)$", label.strip())
    if m:
        label = X_NAMES.get(m.group(1).lower(), m.group(1))
    label = label.replace("@", "")
    label = _re.sub(r"\.(us|gg|com|net|org|dk|io|tv|ru)\b", "", label, flags=_re.I)
    return label


def download(url):
    fd, path = tempfile.mkstemp(suffix=".img")
    os.close(fd)
    p = subprocess.run(["curl", "-sL", "--max-time", "30", "-A", UA, "-o", path, "-w", "%{http_code}", url],
                       capture_output=True, text=True)
    if p.stdout.strip() != "200" or os.path.getsize(path) < 1000:
        os.unlink(path); return None
    return path


def validate_image(path):
    """Full-decode local image before Telegram upload. Abort on truncate/corrupt/tiny."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.load()  # force full decode (truncated JPEG fails here)
            w, h = im.size
        if w < 64 or h < 64:
            raise ValueError(f"dimensions too small ({w}x{h})")
        if os.path.getsize(path) < 1000:
            raise ValueError(f"file too small ({os.path.getsize(path)} bytes)")
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"BROKEN IMAGE: {path}: {e}")


DEDUP_HOURS = 3          # live/announce: same first line, or a shared score and two names
# News repeats (Dust2 at 11:02, Reddit at 17:04) sit outside that window and have no score.
# sent_log.json path: story_dedup.sent_log_path() (override with SENT_LOG in the environment).


def fingerprint(body):
    """Headline fingerprint: latin words (teams/maps) + scores from the first line."""
    first = re.sub(r"<[^>]+>", "", html.unescape(body)).strip().splitlines()[0] if body.strip() else ""
    scores = set(re.findall(r"\d+\s*[:\u2013-]\s*\d+", first))
    scores = {re.sub(r"\s|\u2013|-", ":", s).replace("::", ":") for s in scores}
    words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9.\-]{1,}", first)}
    return sorted(words), sorted(scores)


_DEFAULT_TIE = object()


def find_dup(body, chat, hours=DEDUP_HOURS, news=False, now=None, headlines=None, log=None,
             tie_break=_DEFAULT_TIE, seen=None):
    """A recent channel post this body would repeat.

    `hours` (default 3) is the live window: identical first line, or a shared
    score plus two latin words. News also looks back 48 hours at the headline
    against sent_log, the daily digest «Тема:» lines, and source titles we
    already posted. `headlines` / `log` / `seen` stand in for those files in tests.
    """
    import time
    now = time.time() if now is None else now
    records = load_json(sent_log_path(), []) if log is None else log
    words, scores = fingerprint(body)
    first = (re.sub(r"<[^>]+>", "", body).strip().splitlines() or [""])[0]
    for r in records:
        if not isinstance(r, dict):
            continue
        if r.get("chat") != str(chat) or now - r.get("ts", 0) > hours * 3600:
            continue
        w2, s2 = set(r.get("words", [])), set(r.get("scores", []))
        if r.get("first") and r["first"] == first:
            return r
        if scores and set(scores) & s2 and len(set(words) & w2) >= 2:
            return r
    if not news:
        return None
    head = headline_of(body)
    if not head:
        return None
    prev = []
    labeled = []
    for r in records:
        if not isinstance(r, dict) or r.get("chat") != str(chat):
            continue
        try:
            age = now - float(r.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if age > NEWS_WINDOW_HOURS * 3600 or age < -60 or not r.get("first"):
            continue
        prev.append(r["first"])
        labeled.append(r)
    if headlines is None:
        extra = digest_topics() if str(chat) in {str(CHANNEL_ID), "de_csnews", "@de_csnews"} else []
        if seen is None:
            seen = (load_state().get("seen") or {})
        extra.extend(recent_seen_titles(seen))
    else:
        extra = list(headlines)
    # sent_heads covers the same rows as `labeled` when log is the file; tests pass `log`.
    if log is None and headlines is None:
        extra.extend(sent_heads(chat, now_epoch=now))
    if tie_break is _DEFAULT_TIE:
        tie_break = default_tie_break()
    match = best_match(head, prev + extra, tie_break=tie_break)
    if not match:
        return None
    for r in labeled:
        if r.get("first") == match:
            return r
    return {"message_id": None, "first": match, "chat": str(chat)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("item_id")
    ap.add_argument("--text-file", help="file with Russian HTML text (default: stdin)")
    ap.add_argument("--dry-run", action="store_true", help="print payload, send nothing, don't mark")
    ap.add_argument("--chat", default=CHANNEL_ID, help="override chat_id (e.g. for testing)")
    ap.add_argument("--no-mark", action="store_true", help="don't mark item as seen after sending")
    ap.add_argument("--url"); ap.add_argument("--source"); ap.add_argument("--image")
    ap.add_argument("--no-source", action="store_true", help="omit the 'Источник' line (match results, announcements)")
    ap.add_argument("--title")
    ap.add_argument("--no-gap", action="store_true", help="skip the 75 s spacing after the previous channel post")
    ap.add_argument("--force-dup", action="store_true",
                    help="send even if this repeats a post (3 h for a score line, 48 h for the same news story)")
    a = ap.parse_args()

    items = {i["id"]: i for i in load_json(ITEMS_FILE, {}).get("items", [])}
    item = dict(items.get(a.item_id, {"id": a.item_id}))
    for k in ("url", "source", "image", "title"):
        if getattr(a, k) is not None:
            item[k] = getattr(a, k)
    if not item.get("url") or not item.get("source"):
        sys.exit(f"Item {a.item_id} not in items.json; pass --url and --source (and --image).")

    text = (open(a.text_file, encoding="utf-8").read() if a.text_file else sys.stdin.read()).strip()
    if not text:
        sys.exit("Empty text.")
    label = SOURCE_LABELS.get(item["source"], item["source"])
    label = clean_label(label)
    TAG_MAP = {"hltvorg": "HLTV", "counterstrike": "CS2", "eslcs": "ESL", "blastpremier": "BLAST",
               "pglesports": "PGL", "teamvitalitycs": "Vitality", "teamvitality": "Vitality", "mousesports": "MOUZ",
               "team__spirit": "Spirit", "teamspirit": "Spirit", "natusvincere": "NAVI", "navi": "NAVI",
               "fazeesports": "FaZe", "fazeclan": "FaZe", "g2csgo": "G2", "g2esports": "G2", "furiagg": "FURIA",
               "falconsesport": "Falcons", "auroracs2_gg": "Aurora", "auroracs2": "Aurora", "1mongolz": "TheMongolZ",
               "parivisiongg": "PARIVISION", "astraliscs": "Astralis", "teamliquidcs": "Liquid", "teamliquid": "Liquid",
               "heroicgg": "HEROIC", "3dmaxgaming": "3DMAX", "gamerlegion": "GamerLegion", "nipcs": "NIP",
               "b8esportsgg": "B8", "virtuspro": "VirtusPro", "eternalfiregg": "EternalFire"}
    def _fix_tags(line):
        out, seen = [], set()
        for t in line.split():
            if t.startswith('#'):
                t = '#' + TAG_MAP.get(t[1:].lower(), t[1:])
                if t.lower() in seen: continue
                seen.add(t.lower())
            out.append(t)
        return ' '.join(out)
    _lines = text.rstrip().split('\n')
    if _lines and _lines[-1].lstrip().startswith('#'):
        _lines[-1] = _fix_tags(_lines[-1]); text = '\n'.join(_lines)
    if '#de_csnews' not in text:
        lines = text.rstrip().split('\n')
        if lines and lines[-1].lstrip().startswith('#'):
            lines[-1] = lines[-1].rstrip() + ' #de_csnews'
            text = '\n'.join(lines)
        else:
            text = text.rstrip() + '\n\n#de_csnews'
    if a.no_source:
        body = text
    else:
        body = f'{text}\n\n<i>Источник: {html.escape(label)}</i>'
    image = (item.get("image") or "").strip()
    n = visible_len(body)
    limit = 1024 if image else 4096
    if n > limit:
        sys.exit(f"Text too long: {n} visible chars > {limit} (incl. source link). Shorten it.")

    local_image = bool(image) and os.path.isfile(image)
    if image and not local_image and not re.match(r"^https?://", image):
        sys.exit(f"--image {image!r}: not an existing file and not an http(s) URL")
    if local_image:
        validate_image(image)
    if image:
        method, payload = "sendPhoto", {"chat_id": a.chat, "photo": image, "caption": body, "parse_mode": "HTML"}
    else:
        method, payload = "sendMessage", {"chat_id": a.chat, "text": body, "parse_mode": "HTML",
                                          "link_preview_options": json.dumps({"is_disabled": True})}

    if local_image:
        payload = {k: v for k, v in payload.items() if k != "photo"}
        payload_show = dict(payload, photo=f"<upload {image} ({os.path.getsize(image)} bytes)>")
    else:
        payload_show = payload
    def _refuse_if_dup():
        d = find_dup(body, a.chat, news=not a.no_source)
        if not d:
            return
        sys.exit(f"DUPLICATE: similar post already sent (message {d.get('message_id')}, {d.get('first')!r}). "
                 f"Not sent. Use --force-dup only if it is really a different post.")

    if a.dry_run:
        if not a.force_dup:
            _refuse_if_dup()
        print(json.dumps({"method": method, "visible_chars": n, "payload": payload_show,
                          "fallback": "sendMessage" if image else None}, ensure_ascii=False, indent=2))
        print("\n--- caption/text as written ---\n" + body)
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN not set")
    # dedup guard (lesson 21): hold an exclusive lock across check+send+log so two parallel runs can't both send
    import fcntl, time
    _log_path = sent_log_path()
    _lk = open(_log_path + ".lock", "w"); fcntl.flock(_lk, fcntl.LOCK_EX)
    if not a.force_dup:
        _refuse_if_dup()
    # burst guard (lesson 23): >= 75 s between channel posts (lock is held, so queued senders line up)
    if not a.no_gap:
        try:
            _gap = int(os.environ.get("LIVE_GAP_SEC", "75"))
            _last = max([float(r.get("ts") or 0) for r in json.load(open(_log_path)) if str(r.get("chat")) == str(a.chat)] or [0])
            _w = min(_gap, _last + _gap - time.time())
            if _w > 0:
                print(f"gap: waiting {_w:.0f}s after previous post", file=sys.stderr); time.sleep(_w)
        except Exception:
            pass
    if local_image:
        with open(image, "rb") as f:
            res = tg("sendPhoto", token, payload, files={"photo": (os.path.basename(image), f)})
    else:
        res = tg(method, token, payload)
    if not res.get("ok") and local_image:
        print(f"photo upload failed: {res.get('description')}; falling back to sendMessage", file=sys.stderr)
        res = tg("sendMessage", token, {"chat_id": a.chat, "text": body, "parse_mode": "HTML"})
    elif not res.get("ok") and image:
        print(f"sendPhoto by URL failed: {res.get('description')}; retrying with upload", file=sys.stderr)
        path = download(image)
        if path:
            try:
                validate_image(path)
            except SystemExit:
                os.unlink(path)
                raise
            with open(path, "rb") as f:
                p2 = {k: v for k, v in payload.items() if k != "photo"}
                res = tg("sendPhoto", token, p2, files={"photo": ("image.jpg", f)})
            os.unlink(path)
        if not res.get("ok"):
            print(f"photo upload failed: {res.get('description')}; falling back to sendMessage", file=sys.stderr)
            res = tg("sendMessage", token, {"chat_id": a.chat, "text": body, "parse_mode": "HTML"})
    if not res.get("ok"):
        sys.exit(f"Telegram error: {res.get('description')}")
    msg = res["result"]
    words, scores = fingerprint(body)
    # Keep 48 h so a news story posted this morning is still visible tonight and tomorrow.
    log = [r for r in load_json(_log_path, []) if time.time() - r.get("ts", 0) < NEWS_WINDOW_HOURS * 3600]
    log.append({"ts": time.time(), "chat": str(a.chat), "message_id": msg.get("message_id"),
                "first": (re.sub(r"<[^>]+>", "", body).strip().splitlines() or [""])[0], "words": words, "scores": scores})
    with open(_log_path + ".tmp", "w") as f:
        json.dump(log, f, ensure_ascii=False)
    os.replace(_log_path + ".tmp", _log_path)
    try:
        record_sent(
            message_id=msg.get("message_id"),
            chat=a.chat,
            msg_chat=msg.get("chat"),
            item_id=a.item_id,
            body=body,
            item=item,
            text_file=a.text_file,
            telegram_date=msg.get("date"),
        )
    except Exception as exc:
        print(f"daily digest: {exc}", file=sys.stderr)
    print(json.dumps({"ok": True, "message_id": msg.get("message_id"), "chat": msg.get("chat", {}).get("username") or a.chat}))
    if not a.no_mark:
        st = load_state(); mark(st, item, "posted"); save_state(st)


if __name__ == "__main__":
    main()
