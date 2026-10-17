#!/usr/bin/env python3
"""Read public Telegram channel previews (https://t.me/s/<channel>, no login) and extract score lines for a match.
Read-only; nothing is posted. Channel list: tg_channels.json.

  python3 tg_scan.py --teams Vitality MOUZ [--minutes 90] [--all]          # posts mentioning the teams / scores
  python3 tg_scan.py --teams Vitality MOUZ --confirm 7:5 [--minutes 30]    # cross-check a score (exit 0 if confirmed)

A post is "relevant" if it names a team (Latin or Russian alias) or contains a score (N:N / N-N, 0..30).
--confirm X:Y: confirmed when one reliable channel or >= 2 distinct channels contain that score (either order)
in relevant posts of the window. Telegram posts are informal (e.g. '5:1 ➡️ 5:7') - never use them as the only
source of a score unless confirmed; Liquipedia (lp_live.py) is the primary half-time source.
"""
import argparse, html, json, os, re, subprocess, sys
from datetime import datetime, timedelta, timezone
from common import UA, BASE, MSK

RU = {"vitality": ["виталити", "пчел", "пчёл"], "mouz": ["мауз", "мышей", "мыши"], "spirit": ["спирит", "драконы"],
      "natus vincere": ["нави", "navi"], "navi": ["нави"], "faze": ["фейз", "фейзы"], "g2": ["г2"],
      "aurora": ["аврора"], "falcons": ["фалконс", "соколы"], "furia": ["фурия"], "the mongolz": ["монголы", "mongolz"],
      "parivision": ["паривижн"], "virtus.pro": ["вп", "vp", "виртус"]}
SCORE = re.compile(r"(?<![\d.:])([0-2]?\d)\s?[:\-–]\s?([0-2]?\d)(?![\d:])")


def fetch(ch):
    p = subprocess.run(["curl", "-s", "--compressed", "--max-time", "15", "-A", UA, f"https://t.me/s/{ch}"],
                       capture_output=True, text=True)
    out = []
    for b in p.stdout.split('class="tgme_widget_message_wrap')[1:]:
        pid = re.search(r'data-post="([^"]+)"', b)
        dt = re.search(r'datetime="([^"]+)"', b)
        t = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', b, re.S)
        if not (pid and dt):
            continue
        tx = t.group(1) if t else ""
        tx = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", "\n", tx))).strip()
        out.append({"channel": ch, "url": "https://t.me/" + pid.group(1), "time": datetime.fromisoformat(dt.group(1)),
                    "text": tx})
    return out


def aliases(team):
    k = team.lower()
    return [k] + RU.get(k, [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", nargs=2, required=True)
    ap.add_argument("--minutes", type=int, default=90)
    ap.add_argument("--confirm")
    ap.add_argument("--all", action="store_true", help="show every post in the window")
    a = ap.parse_args()
    chans = json.load(open(os.path.join(BASE, "tg_channels.json"), encoding="utf-8"))
    since = datetime.now(timezone.utc) - timedelta(minutes=a.minutes)
    al = [x for t in a.teams for x in aliases(t)]
    rows = []
    for c in chans:
        for p in fetch(c["channel"]):
            if p["time"] < since:
                continue
            low = p["text"].lower()
            team_hit = any(x in low for x in al)
            scores = [f"{x}:{y}" for x, y in SCORE.findall(p["text"]) if int(x) <= 30 and int(y) <= 30]
            if not (a.all or team_hit or scores):
                continue
            rows.append({"channel": c["channel"], "reliable": c["reliable"], "url": p["url"],
                         "time_msk": p["time"].astimezone(MSK).strftime("%H:%M"), "team_hit": team_hit,
                         "scores": scores, "text": p["text"][:300]})
    if a.confirm:
        x, y = a.confirm.split(":")
        want = {f"{x}:{y}", f"{y}:{x}"}
        hits = [r for r in rows if want & set(r["scores"])]
        chs = {r["channel"] for r in hits}
        ok = any(r["reliable"] for r in hits) or len(chs) >= 2
        print(json.dumps({"score": a.confirm, "confirmed": ok, "channels": sorted(chs), "posts": hits},
                         ensure_ascii=False, indent=1))
        sys.exit(0 if ok else 1)
    print(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
