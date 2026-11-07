#!/usr/bin/env python3
"""DM a publication failure to one private Telegram chat. One-shot, no daemon.

Copy this file alone onto the Dzen/clips box. It needs the Python standard
library and ``requests`` (already required by this repo). It does not import
the news channel, ``common.py``, or any other project module.

  python3 notify_error.py --platform dzen --stage publish \\
      --title "Заголовок" --error "HTTP 500" --status not_posted
  python3 notify_error.py --platform clips --stage upload \\
      --error "ffmpeg exit 1" --status unknown --logs-url https://example.invalid/job/1
  python3 notify_error.py --report publish_failure.json

Environment:

  TELEGRAM_BOT_TOKEN       existing bot token. Never printed.
  TELEGRAM_ALERT_CHAT_ID   the only recipient. A positive private chat id
                           (the chat where /start was sent). There is no
                           default. A channel id, ``@de_csnews``, an empty
                           value, or any other variable is not used.
  ALERT_DEDUP_PATH         optional JSON file of recent fingerprints
                           (default: ./alert_dedup.json).
  ALERT_DEDUP_MINUTES      suppress the same failure for this many minutes
                           (default 150, max 48 hours).
  NOTIFY_FORBID_NETWORK    if ``1``, do not call Telegram (tests).

The same redacted failure (platform, stage, status, title, reason) is sent
once per 2.5 hours. A failed send is not remembered, so the next call tries
again. Dry-run does not send.

Exit codes: 0 delivered, duplicate, dry-run, or nothing to report; 2 the
recipient or the arguments are unusable; 1 Telegram did not accept the
message. The log line ``alert: delivered`` is printed only after Telegram
returns ``ok: true``.
"""
import argparse, datetime, fcntl, hashlib, html, json, os, re, sys, tempfile
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
# Public @de_csnews. Refusal list only — never a fallback destination.
REFUSED_CHAT_IDS = frozenset({
    "-1003635521376",
    "1003635521376",
    "3635521376",
    "@de_csnews",
    "de_csnews",
})
STATUS_LABEL = {
    "posted": "отправлено",
    "not_posted": "не отправлено",
    "unknown": "неизвестно",
}
SECRET_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "REDDIT_CLIENT_SECRET",
)
SECRET_ASSIGN = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALERT_CHAT_ID",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_CLIENT_ID",
)
_TS = re.compile(r"^\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\]\s*")
_SPECIFIC = (
    "telegram error:",
    "abort:",
    "broken image:",
    "text too long:",
    "nothing posted",
    "nothing sent",
    "every source failed",
    "no draft",
    "did not return json",
    "no post went out",
    "fetch.py exit",
    "fetch.py did not",
    "live gate",
    "telegram_bot_token not set",
    "card failed",
    "publication exited",
    "publication runner failed",
    "publication job did not finish",
)
_POSTED_MARKERS = ('"ok": true', "counting as posted", "counting this as posted")
_OK_EXIT = frozenset({"delivered", "duplicate", "already_delivered", "dry_run", "no_report"})
_CONFIG_EXIT = frozenset({
    "no_target", "invalid_target", "refused_channel", "invalid_input", "no_token",
})


def now_msk(now=None):
    if now is None:
        return datetime.datetime.now(MSK)
    if now.tzinfo is None:
        now = now.replace(tzinfo=MSK)
    return now.astimezone(MSK)


def redact(text):
    """Strip tokens, API keys, auth headers, and bot-token URLs."""
    text = "" if text is None else str(text)
    for name in SECRET_ENV:
        secret = (os.environ.get(name) or "").strip().strip('"').strip("'")
        if len(secret) >= 12:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"https?://api\.telegram\.org/bot[^/\s\"']+", "https://api.telegram.org/bot[redacted]", text, flags=re.I)
    text = re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b", "[redacted]", text)
    text = re.sub(r"sk-or-v1-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[redacted]", text)
    text = re.sub(r"(?i)\b(?:ghp_|github_pat_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9_]+", "[redacted]", text)
    text = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)authorization\s*[:=]\s*\S+", "authorization: [redacted]", text)
    text = re.sub(r"(?i)(x-goog-api-key|x-api-key|api-key)\s*[:=]\s*\S+", r"\1: [redacted]", text)
    names = "|".join(SECRET_ASSIGN)
    text = re.sub(rf"(?i)\b(?:{names})\s*=\s*\S+", lambda m: m.group(0).split("=", 1)[0] + "=[redacted]", text)
    text = re.sub(r"(?i)([?&](?:access_token|api_key|apikey|token|key|secret)=)[^&\s]+", r"\1[redacted]", text)
    return text


def one_line(text, limit):
    text = redact(text)
    text = text.replace("\x00", "")
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def normalize_status(status):
    text = (status or "").strip().lower().replace("-", "_").replace(" ", "_")
    if text == "notposted":
        text = "not_posted"
    return text if text in STATUS_LABEL else ""


def classify_chat(raw):
    """Return (positive chat id, None) or (None, reason). Never returns a channel."""
    if raw is None:
        return None, "no_target"
    text = str(raw).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if not text:
        return None, "no_target"
    folded = text.lower()
    refused = {item.lower() for item in REFUSED_CHAT_IDS}
    if folded in refused:
        return None, "refused_channel"
    if text.startswith("-") or text.startswith("@"):
        return None, "invalid_target"
    digits = text[1:] if text.startswith("+") else text
    if not digits.isdigit() or len(digits) > 15:
        return None, "invalid_target"
    number = int(digits, 10)
    if number <= 0 or str(number) in REFUSED_CHAT_IDS:
        return None, "refused_channel" if str(number) in REFUSED_CHAT_IDS else "invalid_target"
    return number, None


def configured_chat():
    if "TELEGRAM_ALERT_CHAT_ID" not in os.environ:
        return None, "no_target"
    return classify_chat(os.environ.get("TELEGRAM_ALERT_CHAT_ID"))


def report_path():
    override = (os.environ.get("PUBLISH_FAILURE_REPORT") or "").strip()
    if override:
        return override
    return os.path.join(os.getcwd(), "publish_failure.json")


def dedup_path():
    override = (os.environ.get("ALERT_DEDUP_PATH") or "").strip()
    if override:
        return override
    return os.path.join(os.getcwd(), "alert_dedup.json")


def dedup_window():
    raw = (os.environ.get("ALERT_DEDUP_MINUTES") or "").strip()
    try:
        minutes = float(raw) if raw else 150
    except ValueError:
        minutes = 150
    if minutes < 0:
        minutes = 0
    if minutes > 48 * 60:
        minutes = 48 * 60
    return datetime.timedelta(minutes=minutes)


def _atomic_json(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _report_fields(fields):
    title = fields.get("title")
    title = None if title is None or str(title).strip() == "" else one_line(title, 180)
    error = one_line(fields.get("error") or "", 400)
    return {
        "platform": one_line(fields.get("platform") or "", 60),
        "stage": one_line(fields.get("stage") or "", 60),
        "title": title,
        "error": error,
        "status": normalize_status(fields.get("status") or "unknown") or "unknown",
        "delivered": False,
    }


def write_report(fields, path=None):
    path = path or report_path()
    _atomic_json(path, _report_fields(fields))
    return path


def write_pending(stage, platform="telegram", path=None):
    return write_report({
        "platform": platform,
        "stage": stage,
        "title": None,
        "error": "publication job did not finish",
        "status": "unknown",
    }, path=path)


def clear_report(path=None):
    path = path or report_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _mark_delivered(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return
        data["delivered"] = True
        _atomic_json(path, data)
    except Exception as ex:
        print(f"alert: delivered; report flag was not saved ({ex.__class__.__name__})", flush=True)


def default_logs_url():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return None
    server = (os.environ.get("GITHUB_SERVER_URL") or "").rstrip("/")
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    run_id = (os.environ.get("GITHUB_RUN_ID") or "").strip()
    if not server or not repo or not run_id.isdigit():
        return None
    if any(c in repo for c in (" ", "\n", "\\")) or ".." in repo.split("/"):
        return None
    return f"{server}/{repo}/actions/runs/{run_id}"


def clean_logs_url(url):
    if url is None or str(url).strip() == "":
        return default_logs_url()
    text = str(url).strip()
    if text.lower() in ("none", "null"):
        return None
    if not text.startswith(("https://", "http://")):
        return None
    if any(c in text for c in ("\n", "\r", " ", "\t")):
        return None
    if "api.telegram.org" in text.lower():
        return None
    if redact(text) != text:
        return None
    if len(text) > 300:
        return None
    return text


def build_message(*, platform, stage, error, status, title=None, logs_url=None, now=None):
    stamp = now_msk(now).strftime("%Y-%m-%d %H:%M") + " MSK"
    lines = [
        "Сбой публикации",
        f"Площадка: {one_line(platform, 60)}",
        f"Этап: {one_line(stage, 60)}",
        f"Время: {stamp}",
        f"Статус: {STATUS_LABEL[status]}",
    ]
    if title:
        lines.append("Материал: " + one_line(title, 180))
    lines.append("Причина: " + one_line(error, 400))
    if logs_url:
        lines.append("Логи: " + logs_url)
    text = redact("\n".join(lines))
    if len(text) > 3500:
        text = text[:3499].rstrip() + "…"
    return text


def summarize_failure(output, returncode):
    """One short reason and an optional title. Not the raw log."""
    lines = []
    for line in (output or "").splitlines():
        bare = _TS.sub("", line.strip())
        if bare:
            lines.append(bare)
    chosen = None
    for bare in reversed(lines):
        low = bare.lower()
        if any(marker in low for marker in _SPECIFIC):
            chosen = bare
            break
    if not chosen:
        chosen = f"publication exited {returncode}"
    text = output or ""
    if any(marker in text for marker in _POSTED_MARKERS):
        status = "unknown"
    elif not text.strip():
        status = "unknown"
    else:
        status = "not_posted"
    return {
        "error": one_line(chosen, 400) or f"publication exited {returncode}",
        "title": extract_title(text),
        "status": status,
    }


def extract_title(output):
    found = re.findall(r"<b>(.{1,300}?)</b>", output or "", flags=re.I | re.S)
    for raw in reversed(found):
        text = html.unescape(re.sub(r"<[^>]+>", "", raw))
        text = one_line(text, 180)
        if text and not text.startswith("#") and "[redacted]" not in text:
            return text
    return None


def _fingerprint(platform, stage, status, title, error):
    raw = "\n".join([
        (platform or "").strip().lower(),
        (stage or "").strip().lower(),
        status,
        (title or "").strip().lower(),
        re.sub(r"\s+", " ", (error or "").strip().lower()),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_time(value):
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK)
    return parsed


def _recent_duplicate(fp, now):
    path = dedup_path()
    window = dedup_window()

    def read():
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return False
        except Exception:
            print("alert: dedup state unreadable; sending anyway", flush=True)
            return False
        sent = data.get("sent") if isinstance(data, dict) else None
        if not isinstance(sent, list):
            return False
        for item in sent:
            if not isinstance(item, dict) or item.get("fp") != fp:
                continue
            at = _parse_time(item.get("at"))
            if at is not None and now - at < window:
                return True
        return False

    try:
        return _with_dedup_lock(read)
    except Exception:
        print("alert: dedup state unreadable; sending anyway", flush=True)
        return False


def _remember(fp, now):
    path = dedup_path()
    window_keep = datetime.timedelta(days=7)

    def write():
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            data = {"sent": []}
        sent = data.get("sent") if isinstance(data, dict) else None
        if not isinstance(sent, list):
            sent = []
        kept = []
        for item in sent:
            if not isinstance(item, dict) or not item.get("fp") or not item.get("at"):
                continue
            at = _parse_time(item.get("at"))
            if at is None or now - at > window_keep:
                continue
            kept.append({"fp": item["fp"], "at": item["at"]})
        kept.append({"fp": fp, "at": now.isoformat(timespec="seconds")})
        _atomic_json(path, {"sent": kept[-200:]})

    _with_dedup_lock(write)


def _with_dedup_lock(fn):
    path = dedup_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    lock_path = path + ".lock"
    with open(lock_path, "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _result(reason, detail=None):
    if reason == "delivered":
        print("alert: delivered", flush=True)
    elif reason == "duplicate":
        print("alert: suppressed duplicate", flush=True)
    elif reason == "already_delivered":
        print("alert: already delivered for this run; not sending again", flush=True)
    elif reason == "dry_run":
        print("alert: dry-run, not sending", flush=True)
    elif reason == "no_target":
        print("alert: no private chat configured (TELEGRAM_ALERT_CHAT_ID); not sending", flush=True)
    elif reason == "invalid_target":
        print("alert: TELEGRAM_ALERT_CHAT_ID is not a positive private chat id; not sending", flush=True)
    elif reason == "refused_channel":
        print("alert: refused public channel id; not sending", flush=True)
    elif reason == "no_report":
        print("alert: no failure report; not sending", flush=True)
    elif reason == "no_token":
        print("alert: not delivered (TELEGRAM_BOT_TOKEN is not set)", flush=True)
    else:
        extra = one_line(detail or "telegram error", 300) or "telegram error"
        print(f"alert: not delivered ({extra})", flush=True)
        if reason not in ("telegram_error", "invalid_input"):
            reason = "telegram_error"
    return {"delivered": reason == "delivered", "reason": reason}


def deliver(token, chat_id, text):
    """POST sendMessage. Returns (ok, detail). Does not log and does not raise."""
    if os.environ.get("NOTIFY_FORBID_NETWORK") == "1":
        return False, "network forbidden"
    if not isinstance(chat_id, int) or chat_id <= 0 or str(chat_id) in REFUSED_CHAT_IDS:
        return False, "refused chat"
    try:
        import requests
    except ImportError:
        return False, "requests is not installed"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    try:
        response = requests.post(url, json=payload, timeout=15)
    except Exception as ex:
        if isinstance(ex, AssertionError):
            raise
        return False, redact(f"{ex.__class__.__name__}: {ex}")
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict) and response.status_code == 200 and body.get("ok") is True:
        return True, ""
    if isinstance(body, dict) and body.get("description"):
        return False, redact(str(body.get("description")))
    return False, redact(f"HTTP {getattr(response, 'status_code', '?')}")


def notify(*, platform, stage, error, status="unknown", title=None, logs_url=None,
          dry_run=False, now=None, report=None):
    """Send one private alert. Never raises. Never posts to the news channel."""
    if dry_run:
        return _result("dry_run")
    status_n = normalize_status(status)
    platform_s = one_line(platform, 60)
    stage_s = one_line(stage, 60)
    error_s = one_line(error, 400)
    if (error or "").strip() and not error_s:
        error_s = "publication failed"
    title_s = one_line(title, 180) if title else None
    if not platform_s or not stage_s or not error_s or not status_n:
        return _result("invalid_input", "platform, stage, error, and status are required")
    chat_id, chat_reason = configured_chat()
    if chat_reason:
        return _result(chat_reason)
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip().strip('"').strip("'")
    if not token:
        return _result("no_token")
    moment = now_msk(now)
    logs = clean_logs_url(logs_url)
    text = build_message(
        platform=platform_s, stage=stage_s, error=error_s, status=status_n,
        title=title_s, logs_url=logs, now=moment,
    )
    fp = _fingerprint(platform_s, stage_s, status_n, title_s, error_s)
    if _recent_duplicate(fp, moment):
        return _result("duplicate")
    ok, detail = deliver(token, chat_id, text)
    if not ok:
        return _result("telegram_error", detail)
    try:
        _remember(fp, moment)
    except Exception as ex:
        print(f"alert: delivered; dedup state was not saved ({ex.__class__.__name__})", flush=True)
        if report:
            _mark_delivered(report)
        return {"delivered": True, "reason": "delivered"}
    if report:
        _mark_delivered(report)
    return _result("delivered")


def alert_publication(*, platform, stage, output, returncode, now=None, path=None):
    """Write the failure report, then DM. A send error is returned, not raised."""
    summary = summarize_failure(output, returncode)
    fields = {
        "platform": platform,
        "stage": stage,
        "title": summary["title"],
        "error": summary["error"],
        "status": summary["status"],
    }
    report = write_report(fields, path=path)
    return notify(now=now, report=report, **fields)


def _load_report(path):
    if not path or not os.path.isfile(path):
        return None, _result("no_report")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None, _result("telegram_error", "failure report is not valid JSON")
    if not isinstance(data, dict):
        return None, _result("telegram_error", "failure report is not an object")
    return data, None


def notify_report(path, *, dry_run=False, now=None):
    data, failure = _load_report(path)
    if failure:
        return failure
    if data.get("delivered") is True and not dry_run:
        return _result("already_delivered")
    if dry_run or data.get("dry_run") is True:
        return _result("dry_run")
    return notify(
        platform=data.get("platform") or "",
        stage=data.get("stage") or "",
        error=data.get("error") or "",
        status=data.get("status") or "",
        title=data.get("title"),
        logs_url=data.get("logs_url"),
        now=now,
        report=path,
    )


def exit_code(reason):
    if reason in _OK_EXIT:
        return 0
    if reason in _CONFIG_EXIT:
        return 2
    return 1


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--platform", help="telegram, dzen, clips, …")
    parser.add_argument("--stage", help="which step failed (publish, upload, announce, …)")
    parser.add_argument("--title", default="", help="content title, if known")
    parser.add_argument("--error", help="short reason; tokens are redacted")
    parser.add_argument("--status", choices=["posted", "not_posted", "unknown"])
    parser.add_argument("--logs-url", default="", help="link to the job log; omit on the box if you have none")
    parser.add_argument("--dry-run", action="store_true", help="do not send")
    parser.add_argument("--report", help="JSON failure report from a publication job")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    field_set = any([args.platform, args.stage, args.error, args.status, args.title, args.logs_url])
    if args.report and field_set:
        result = _result("invalid_input", "pass either --report or the fields, not both")
        return exit_code(result["reason"])
    if args.report:
        result = notify_report(args.report, dry_run=args.dry_run)
        return exit_code(result["reason"])
    if args.dry_run:
        result = _result("dry_run")
        return exit_code(result["reason"])
    result = notify(
        platform=args.platform or "",
        stage=args.stage or "",
        error=args.error or "",
        status=args.status or "",
        title=args.title or None,
        logs_url=args.logs_url or None,
    )
    return exit_code(result["reason"])


if __name__ == "__main__":
    sys.exit(main())
