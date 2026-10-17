"""Shared helpers for announce_auto.py / results_auto.py (cron, no AI). Posts only via post.py (all its guards)."""
import json, os, re, subprocess, sys, datetime, html, fcntl

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "auto_state.json")
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]
TEAM_SHORT = {"Ninjas in Pyjamas": "NiP", "Natus Vincere": "NAVI", "Team Vitality": "Vitality", "Team Spirit": "Spirit",
              "Team Falcons": "Falcons", "Team Liquid": "Liquid", "The MongolZ": "The MongolZ", "FaZe Clan": "FaZe",
              "G2 Esports": "G2", "Virtus.pro": "Virtus.pro", "MOUZ": "MOUZ", "Eternal Fire": "Eternal Fire",
              "SEREGA PIRAT": "Серёга Пират"}
# (regex on event, display name, hashtags)
EVENT_RULES = [
    (r"thunderpick", None, ["#Thunderpick"]),
    (r"betboom streamers battle", "BetBoom Streamers Battle CS {n}", ["#BetBoomStreamersBattle", "#стримеры"]),
    (r"esl pro league", None, ["#ESLProLeague"]),
    (r"\bmajor\b", None, ["#Major"]),
    (r"\biem\b|intel extreme", None, ["#IEM"]),
    (r"blast", None, ["#BLAST"]),
    (r"\bpgl\b", None, ["#PGL"]),
    (r"stake pulse", None, ["#StakePulse"]),
    (r"starladder|starseries", None, ["#StarSeries"]),
    (r"cct\b", None, ["#CCT"]),
]


def log(msg):
    print(f"[{datetime.datetime.now():%F %T}] {msg}", flush=True)


def date_ru(d):
    return f"{d.day} {MONTHS[d.month - 1]}"


def team(name):
    n = (name or "").strip()
    if n in TEAM_SHORT:
        return TEAM_SHORT[n]
    n = re.sub(r"^Team\s+", "", n)
    return TEAM_SHORT.get(n, n)


def event_name(ev):
    ev = (ev or "").strip()
    for rx, disp, _ in EVENT_RULES:
        if disp and re.search(rx, ev, re.I):
            m = re.search(r"#?\s*(\d+)\s*$", ev)
            return disp.format(n=m.group(1) if m else "").strip()
    return re.sub(r"\s+20\d\d$", "", ev)  # "Thunderpick World Championship 2026" -> without year


def event_tags(ev):
    for rx, _, tags in EVENT_RULES:
        if re.search(rx, ev or "", re.I):
            return tags
    words = re.findall(r"[A-Za-z0-9]+", re.sub(r"\s+20\d\d$", "", ev or ""))
    return ["#" + "".join(w[:1].upper() + w[1:] for w in words[:3])] if words else []


def hashtags(events):
    tags = ["#CS2"]
    for ev in events:
        for t in event_tags(ev):
            if t.lower() not in [x.lower() for x in tags]:
                tags.append(t)
    return " ".join(tags[:4])  # 2-4 tags; post.py appends #de_csnews


STAGE_RU = [(r"grand ?final", "гранд-финал"), (r"semi.?final", "полуфинал"), (r"quarter.?final", "четвертьфинал"),
            (r"3rd|third", "матч за 3-е место"), (r"upper bracket|winners'? (bracket|final)", "верхняя сетка"),
            (r"lower bracket|losers'? (bracket|final)", "нижняя сетка"), (r"\bfinal\b", "финал")]


def stage_short(stage):
    s = stage or ""
    m = re.match(r"\s*Group\s+([A-Z0-9]+)", s, re.I)
    if m:
        return f"Группа {m.group(1).upper()}"
    for rx, ru in STAGE_RU:
        if re.search(rx, s, re.I):
            return ru[0].upper() + ru[1:]
    return ""  # unknown -> omit rather than guess


def load_events_cfg():
    try:
        return json.load(open(os.path.join(HERE, "events.json")))
    except Exception:
        return {}


def demoted(m, cfg):
    if m.get("tier") in ("S", "A"):
        return False
    for d in cfg.get("demote_events", []):
        if re.search(d.get("match", "^$"), m.get("event") or "", re.I):
            r = [x for x in (m.get("team1_rank"), m.get("team2_rank")) if isinstance(x, int)]
            return not (r and min(r) <= d.get("min_rank", 0))
    return False


def run_json(args, timeout=240):
    p = subprocess.run([sys.executable] + args, cwd=HERE, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} exit {p.returncode}: {p.stderr[-500:]}")
    return json.loads(p.stdout)


def run(args, timeout=600):
    p = subprocess.run([sys.executable] + args, cwd=HERE, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"announced_dates": [], "recapped_keys": [], "results_dates": []}


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def single_instance(name):
    f = open(os.path.join(HERE, f".{name}.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("BUSY: another run holds the lock"); sys.exit(0)
    return f


def check_and_post(item_id, text_path, card_path, dry_run, url, check_args=()):
    """check_post.py (FAIL -> abort) then post.py --dry-run, then real send unless dry_run."""
    rc, out, err = run(["check_post.py", text_path, *check_args])
    log(f"check_post rc={rc}\n{out}{err}")
    if rc != 0:
        log("ABORT: check_post.py FAIL (see above). Nothing sent."); return False
    base = ["post.py", item_id, "--text-file", text_path, "--url", url, "--source", "bo3.gg",
            "--image", card_path or "", "--no-source", "--no-mark"]
    rc, out, err = run(base + ["--dry-run"])
    log(f"post.py --dry-run rc={rc}\n{out}{err}")
    if rc != 0:
        log("ABORT: dry-run failed."); return False
    if dry_run:
        return True
    rc, out, err = run(base)
    blob = (out or "") + (err or "")
    log(f"post.py SEND rc={rc}\n{blob}")
    # LESSONS 21: a duplicate means the channel already has this post. Count it as done
    # so the next catch-up does not retry it all evening.
    if "DUPLICATE:" in blob:
        log("DUPLICATE: already in the channel; counting this as posted")
        return True
    return rc == 0 and '"ok": true' in (out or "")
