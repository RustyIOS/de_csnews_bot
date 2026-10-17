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
"""
import argparse, html, json, os, re, sys, subprocess, tempfile
import requests
from common import (ITEMS_FILE, CHANNEL_ID, UA, load_json, load_state, save_state, mark)

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


SENT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_log.json")
DEDUP_HOURS = 3


def fingerprint(body):
    """Headline fingerprint: latin words (teams/maps) + scores from the first line."""
    first = re.sub(r"<[^>]+>", "", html.unescape(body)).strip().splitlines()[0] if body.strip() else ""
    scores = set(re.findall(r"\d+\s*[:\u2013-]\s*\d+", first))
    scores = {re.sub(r"\s|\u2013|-", ":", s).replace("::", ":") for s in scores}
    words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9.\-]{1,}", first)}
    return sorted(words), sorted(scores)


def find_dup(body, chat, hours=DEDUP_HOURS):
    import time
    words, scores = fingerprint(body)
    for r in load_json(SENT_LOG, []):
        if r.get("chat") != str(chat) or time.time() - r.get("ts", 0) > hours * 3600:
            continue
        w2, s2 = set(r.get("words", [])), set(r.get("scores", []))
        if r.get("first") and r["first"] == (re.sub(r"<[^>]+>", "", body).strip().splitlines() or [""])[0]:
            return r
        if scores and set(scores) & s2 and len(set(words) & w2) >= 2:
            return r
    return None


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
    ap.add_argument("--force-dup", action="store_true", help="send even if a similar post went out in the last 3 h")
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
    if a.dry_run:
        print(json.dumps({"method": method, "visible_chars": n, "payload": payload_show,
                          "fallback": "sendMessage" if image else None}, ensure_ascii=False, indent=2))
        print("\n--- caption/text as written ---\n" + body)
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN not set")
    # dedup guard (lesson 21): hold an exclusive lock across check+send+log so two parallel runs can't both send
    import fcntl, time
    _lk = open(SENT_LOG + ".lock", "w"); fcntl.flock(_lk, fcntl.LOCK_EX)
    if not a.force_dup:
        d = find_dup(body, a.chat)
        if d:
            sys.exit(f"DUPLICATE: similar post already sent (message {d.get('message_id')}, {d.get('first')!r}). "
                     f"Not sent. Use --force-dup only if it is really a different post.")
    # burst guard (lesson 23): >= 75 s between channel posts (lock is held, so queued senders line up)
    if not a.no_gap:
        try:
            _gap = int(os.environ.get("LIVE_GAP_SEC", "75"))
            _last = max([float(r.get("ts") or 0) for r in json.load(open(SENT_LOG)) if str(r.get("chat")) == str(a.chat)] or [0])
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
    log = [r for r in load_json(SENT_LOG, []) if time.time() - r.get("ts", 0) < 86400]
    log.append({"ts": time.time(), "chat": str(a.chat), "message_id": msg.get("message_id"),
                "first": (re.sub(r"<[^>]+>", "", body).strip().splitlines() or [""])[0], "words": words, "scores": scores})
    with open(SENT_LOG + ".tmp", "w") as f:
        json.dump(log, f, ensure_ascii=False)
    os.replace(SENT_LOG + ".tmp", SENT_LOG)
    print(json.dumps({"ok": True, "message_id": msg.get("message_id"), "chat": msg.get("chat", {}).get("username") or a.chat}))
    if not a.no_mark:
        st = load_state(); mark(st, item, "posted"); save_state(st)


if __name__ == "__main__":
    main()
