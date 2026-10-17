#!/usr/bin/env python3
"""Branded 1280x720 image cards for @de_csnews (channel style: charcoal grid, orange crosshair, Exo 2 Black Italic).

Usage:
  python3 card.py TEMPLATE --json data.json -o out.jpg      # '-' = read JSON from stdin; .png or .jpg by extension
  python3 card.py TEMPLATE --json data.json -o out.jpg --no-logos
Templates and JSON fields (field names match matches.py output, so its matches can be passed directly):
  result       {event, stage?, format?, team1, team2, score:"2:1" | score1,score2, maps:[{map, score:"13:9"}],
                team1_logo?, team2_logo?, date?}
  day_results  {date?, title?, groups:[{event, matches:[{team1, team2, score, maps?}]}]}
               or {matches:[... matches.py results ...]} (grouped by event automatically)
  announce     {date?, title?, matches:[{start_msk|time, team1, team2, format?, event, stage?}]}  (grouped by event)
  match_start  {team1, team2, event, stage?, format?, team1_logo?, team2_logo?, team1_rank?, team2_rank?, maps?:[..]}
  poll_result  {question, options:[{text, votes}] | results:{text: votes}, total?, correct?: "<option text>"}
  map_result   {team1, team2, score (series, e.g. 1:0), event, stage?, format?, map_number?, maps:[{map, score, winner} | {map, upcoming:true}], logos?}
  half_time    {team1, team2, half_score, map, series?, team1_side? (CT|T), event, stage?, format?, logos?}
  map_start    {team1, team2, score (series so far), map, map_number, maps:[{map, score, winner}|{map, current:true}|{map, upcoming:true}], event, stage?, format?}
  weekly       {title?, period?, lines:[up to 5 strings]}
  tournament   {name, start_date, end_date | dates, prize | prize_usd, teams | teams_count, location?, tier?, logo?}
Team/event logos are downloaded (bo3.gg image URLs) into cards/logo_cache; failures fall back to text only.
"""
import argparse, hashlib, io, json, math, os, re, subprocess, sys
from datetime import datetime, date
from PIL import Image, ImageDraw, ImageFont, ImageFilter

BASE = os.path.dirname(os.path.abspath(__file__))
FONT_IT = os.path.join(BASE, "fonts", "Exo2-Italic-VariableFont_wght.ttf")
FONT_UP = os.path.join(BASE, "fonts", "Exo2-VariableFont_wght.ttf")
LOGO_CACHE = os.path.join(BASE, "cards", "logo_cache")
W, H = 1280, 720
BG = (22, 24, 28)
GRID = (32, 35, 41)
ORANGE = (255, 106, 0)
ORANGE_D = (150, 64, 6)
WHITE = (245, 245, 245)
GREY = (150, 155, 165)
DIM = (95, 100, 110)
RED = (235, 40, 40)
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря"]
_fc = {}


def font(size, weight=900, italic=True):
    k = (size, weight, italic)
    if k not in _fc:
        f = ImageFont.truetype(FONT_IT if italic else FONT_UP, size)
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
        _fc[k] = f
    return _fc[k]


def tw(d, text, f):
    b = d.textbbox((0, 0), text, font=f)
    return b[2] - b[0]


def fit(d, text, size, max_w, weight=900, italic=True, min_size=None):
    """Largest font <= size that fits max_w; truncates with … if even min_size (default 60% of size) is too wide."""
    if min_size is None:
        min_size = max(16, int(size * 0.6))
    s = size
    while s > min_size and tw(d, text, font(s, weight, italic)) > max_w:
        s -= 2
    f = font(s, weight, italic)
    if tw(d, text, f) > max_w:
        while len(text) > 1 and tw(d, text + "…", f) > max_w:
            text = text[:-1]
        text = text.rstrip() + "…"
    return text, f


def team_block(d, name, cx, y, size, max_w, fill=WHITE):
    """Team name centred at cx; shrinks to 70%, then wraps to 2 lines. Returns bottom y."""
    t, f = fit(d, name, size, max_w, min_size=int(size * 0.7))
    if not t.endswith("…") or " " not in name:
        d.text((cx - tw(d, t, f) // 2, y), t, font=f, fill=fill)
        return y + size
    f = font(int(size * 0.7), 900)
    lines = wrap(d, name, f, max_w, 2)
    for i, ln in enumerate(lines):
        d.text((cx - tw(d, ln, f) // 2, y + i * int(size * 0.8)), ln, font=f, fill=fill)
    return y + len(lines) * int(size * 0.8)


def ru_date(v=None):
    if v is None:
        v = datetime.now().date()
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v[:10]).date()
        except ValueError:
            return v
    return f"{v.day} {MONTHS[v.month - 1]}"


def split_score(x):
    if x.get("score1") is not None:
        return str(x["score1"]), str(x.get("score2", ""))
    s = str(x.get("score") or "")
    if ":" in s:
        a, b = s.split(":", 1)
        return a.strip(), b.strip()
    return "–", "–"


# ---------------- background / brand ----------------
def base(accent_circle=True, alpha=1.0):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    for x in range(0, W, 40):
        d.line([(x, 0), (x, H)], fill=GRID, width=1)
    for y in range(0, H, 40):
        d.line([(0, y), (W, y)], fill=GRID, width=1)
    # vignette
    v = Image.new("L", (W, H), 0)
    vd = ImageDraw.Draw(v)
    vd.ellipse([-260, -300, W + 260, H + 300], fill=255)
    v = v.filter(ImageFilter.GaussianBlur(140))
    dark = Image.new("RGB", (W, H), (12, 13, 16))
    im = Image.composite(im, dark, v)
    d = ImageDraw.Draw(im)
    if accent_circle:  # big crosshair on the right, partially off-canvas
        cx, cy, r = 1130, 360, 300
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        A = lambda v: ORANGE + (int(v * alpha),)
        ld.ellipse([cx - r, cy - r, cx + r, cy + r], outline=A(70), width=6)
        ld.ellipse([cx - r + 40, cy - r + 40, cx + r - 40, cy + r - 40], outline=A(28), width=2)
        ld.line([(cx, cy - r - 40), (cx, cy - r + 70)], fill=A(90), width=5)
        ld.line([(cx, cy + r - 70), (cx, cy + r + 40)], fill=A(90), width=5)
        ld.line([(cx - r - 40, cy), (cx - r + 70, cy)], fill=A(90), width=5)
        for i in range(1, 6):
            ld.line([(cx - 10, cy - r + 70 + i * 22), (cx + 10, cy - r + 70 + i * 22)], fill=A(50), width=2)
        im = Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB")
        d = ImageDraw.Draw(im)
    # corner brackets
    L, M = 46, 26
    for (x, y, sx, sy) in [(M, M, 1, 1), (W - M, M, -1, 1), (M, H - M, 1, -1), (W - M, H - M, -1, -1)]:
        d.line([(x, y), (x + sx * L, y)], fill=ORANGE, width=5)
        d.line([(x, y), (x, y + sy * L)], fill=ORANGE, width=5)
    return im, d


def wordmark(im, d, x_right=W - 60, y_bottom=H - 44, size=34):
    f = font(size, 900)
    a, b = "de_cs", "news"
    wa, wb = tw(d, a, f), tw(d, b, f)
    x = x_right - wa - wb - 4
    y = y_bottom - size
    # tiny crosshair icon
    r = size // 2 - 2
    cx, cy = x - r - 16, y + size // 2 + 3
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ORANGE, width=3)
    d.line([(cx, cy - r - 5), (cx, cy - r + 5)], fill=ORANGE, width=3)
    d.line([(cx, cy + r - 5), (cx, cy + r + 5)], fill=ORANGE, width=3)
    d.line([(cx - r - 5, cy), (cx - r + 5, cy)], fill=ORANGE, width=3)
    d.line([(cx + r - 5, cy), (cx + r + 5, cy)], fill=ORANGE, width=3)
    d.text((x, y), a, font=f, fill=ORANGE)
    d.text((x + wa + 4, y), b, font=f, fill=WHITE)


def header(d, title, sub=None, x=70, y=58, title_size=58, live=False, max_w=860):
    tx = x
    if live:
        f = font(30, 900)
        lw = tw(d, "LIVE", f) + 64
        d.rounded_rectangle([x, y + 12, x + lw, y + 56], radius=8, fill=RED)
        d.ellipse([x + 14, y + 25, x + 32, y + 43], fill=WHITE)
        d.text((x + 42, y + 14), "LIVE", font=f, fill=WHITE)
        tx = x + lw + 22
    d.polygon([(tx, y + 6), (tx + 14, y + 6), (tx + 4, y + title_size + 8), (tx - 10, y + title_size + 8)], fill=ORANGE)
    t, f = fit(d, title.upper(), title_size, max_w - (tx - x) - 30)
    d.text((tx + 26, y), t, font=f, fill=WHITE)
    if sub:
        s, f2 = fit(d, sub, 30, max_w, weight=700)
        d.text((x, y + title_size + 26), s, font=f2, fill=ORANGE)


def footer(im, d, left=None):
    if left:
        t, f = fit(d, left, 24, 640, weight=600)
        d.text((70, H - 44 - 26), t, font=f, fill=GREY)
    wordmark(im, d)


# ---------------- logos ----------------
def get_logo(url, box, enabled=True):
    if not url or not enabled:
        return None
    os.makedirs(LOGO_CACHE, exist_ok=True)
    path = os.path.join(LOGO_CACHE, hashlib.md5(url.encode()).hexdigest()[:16])
    if not os.path.exists(path):
        p = subprocess.run(["curl", "-sL", "--max-time", "20", "-A", "Mozilla/5.0 (compatible; de_csnews-bot/1.0)",
                            "-o", path + ".tmp", "-w", "%{http_code}", url], capture_output=True, text=True)
        if p.stdout.strip() != "200" or not os.path.exists(path + ".tmp") or os.path.getsize(path + ".tmp") < 200:
            try: os.unlink(path + ".tmp")
            except FileNotFoundError: pass
            return None
        os.replace(path + ".tmp", path)
    try:
        lg = Image.open(path).convert("RGBA")
    except Exception:
        return None
    bb = lg.getbbox()
    if bb:
        lg = lg.crop(bb)
    lg.thumbnail((box, box), Image.LANCZOS)
    return lg


def paste_center(im, lg, cx, cy):
    # soft dark disc so dark logos stay visible
    disc = Image.new("RGBA", im.size, (0, 0, 0, 0))
    dd = ImageDraw.Draw(disc)
    r = int(max(lg.size) * 0.72)
    dd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(48, 52, 60, 255), outline=ORANGE + (255,), width=3)
    im.paste(Image.alpha_composite(im.convert("RGBA"), disc).convert("RGB"))
    im.paste(lg, (cx - lg.size[0] // 2, cy - lg.size[1] // 2), lg)


# ---------------- templates ----------------
def t_result(x, logos=True):
    im, d = base()
    ev = x.get("event") or ""
    header(d, x.get("title") or "Результат матча", " · ".join(v for v in (ev, x.get("stage"), x.get("format")) if v))
    s1, s2 = split_score(x)
    maps = x.get("maps") or []
    cy = 360 if maps else 395
    l1 = get_logo(x.get("team1_logo"), 110, logos)
    l2 = get_logo(x.get("team2_logo"), 110, logos)
    # score centre
    fs = font(150, 900)
    sc = f"{s1}:{s2}"
    sw = tw(d, sc, fs)
    scx = 640
    d.text((scx - sw // 2, cy - 100), sc, font=fs, fill=WHITE)
    if x.get("_score_colors"):
        c1, c2 = x["_score_colors"]
        x0s = scx - sw // 2
        d.text((x0s, cy - 100), s1, font=fs, fill=c1)
        d.text((x0s + tw(d, f"{s1}:", fs), cy - 100), s2, font=fs, fill=c2)
    # colour winner's digits orange
    try:
        if x.get("_score_colors"):
            raise ValueError
        a, b = int(s1), int(s2)
        if a != b:
            win_txt = s1 if a > b else s2
            pre = "" if a > b else f"{s1}:"
            d.text((scx - sw // 2 + tw(d, pre, fs) if pre else scx - sw // 2, cy - 100), win_txt, font=fs, fill=ORANGE)
    except ValueError:
        pass
    side_w = 400
    for i, (team, lg) in enumerate(((x.get("team1", "TBD"), l1), (x.get("team2", "TBD"), l2))):
        colx = 255 if i == 0 else 1025
        ty = cy - 20
        if lg:
            paste_center(im, lg, colx, cy - 70)
            d = ImageDraw.Draw(im)
            ty = cy + 20
        team_block(d, team, colx, ty - (20 if not lg and len(team) > 14 else 0), 64 if not lg else 54, side_w - 70)
    # maps
    if maps:
        y = cy + 125
        n = len(maps)
        cw = min(230, (W - 200) // n)
        x0 = 640 - (cw * n) // 2
        for i, m in enumerate(maps):
            bx = x0 + i * cw
            d.rounded_rectangle([bx + 8, y, bx + cw - 8, y + 92], radius=10, fill=(30, 33, 39), outline=(60, 64, 72), width=2)
            mn, f = fit(d, str(m.get("map", "")).upper(), 26, cw - 36, weight=800)
            d.text((bx + cw // 2 - tw(d, mn, f) // 2, y + 10), mn, font=f, fill=GREY)
            ms = str(m.get("score", ""))
            f2 = font(38, 900)
            if x.get("_series"):
                col = GREY if m.get("upcoming") else ORANGE
            else:
                col = ORANGE if m.get("winner") == x.get("winner") and x.get("winner") else WHITE
            d.text((bx + cw // 2 - tw(d, ms, f2) // 2, y + 40), ms, font=f2, fill=col)
    footer(im, d, ru_date(x.get("date") or x.get("date_msk")))
    return im


def group_by_event(matches):
    out, idx = [], {}
    for m in matches:
        ev = m.get("event") or "Другие матчи"
        if ev not in idx:
            idx[ev] = len(out)
            out.append({"event": ev, "matches": []})
        out[idx[ev]]["matches"].append(m)
    return out


def rows_layout(groups, top, bottom, max_rows=None):
    """Return (row_h, header_h, groups_trimmed, hidden_count) so everything fits between top and bottom."""
    total_h = bottom - top
    nm = sum(len(g["matches"]) for g in groups)
    ng = len(groups)
    for row_h in (64, 58, 52, 48, 44):
        gh = int(row_h * 0.85)
        if nm * row_h + ng * gh + (ng - 1) * 10 <= total_h:
            return row_h, gh, groups, 0
    # trim from the end
    row_h, gh = 44, 34
    out, used, hidden = [], 0, 0
    room = total_h - 40  # reserve a line for "+N"
    for g in groups:
        if used + gh + row_h > room:
            hidden += len(g["matches"]); continue
        used += gh + (10 if out else 0)
        keep = []
        for m in g["matches"]:
            if used + row_h <= room:
                keep.append(m); used += row_h
            else:
                hidden += 1
        out.append({"event": g["event"], "matches": keep})
    return row_h, gh, out, hidden


def list_card(title, sub, groups, row_fn, foot=None):
    im, d = base(alpha=0.45)
    header(d, title, sub)
    top, bottom = 200, H - 90
    row_h, gh, groups, hidden = rows_layout(groups, top, bottom)
    y = top
    for gi, g in enumerate(groups):
        if gi:
            y += 10
        t, f = fit(d, g["event"].upper(), int(gh * 0.62), 1000, weight=800)
        d.rectangle([70, y + gh // 2 - 2, 80, y + gh // 2 + 2], fill=ORANGE)
        d.text((92, y + (gh - int(gh * 0.62)) // 2 - 4), t, font=f, fill=ORANGE)
        y += gh
        for i, m in enumerate(g["matches"]):
            if i % 2 == 0:
                d.rectangle([70, y + 2, 1210, y + row_h - 2], fill=(28, 31, 37))
            row_fn(d, m, y, row_h)
            y += row_h
    if hidden:
        f = font(26, 700)
        d.text((92, y + 6), f"+ ещё {hidden} матч(ей)", font=f, fill=GREY)
    footer(im, d, foot)
    return im


def _teams_row(d, m, y, row_h, x_left, x_right, mid_text, mid_color=WHITE, winner=None):
    fsz = int(row_h * 0.52)
    mid_f = font(int(row_h * 0.6), 900)
    mw = max(tw(d, mid_text, mid_f), 70)
    cx = (x_left + x_right) // 2
    d.text((cx - tw(d, mid_text, mid_f) // 2, y + (row_h - int(row_h * 0.6)) // 2 - 6), mid_text, font=mid_f, fill=mid_color)
    half = cx - mw // 2 - 24 - x_left
    t1, f1 = fit(d, m.get("team1", "TBD"), fsz, half, weight=800)
    t2, f2 = fit(d, m.get("team2", "TBD"), fsz, half, weight=800)
    c1 = ORANGE if winner and winner == m.get("team1") else WHITE
    c2 = ORANGE if winner and winner == m.get("team2") else WHITE
    ty = y + (row_h - fsz) // 2 - 5
    d.text((cx - mw // 2 - 24 - tw(d, t1, f1), ty), t1, font=f1, fill=c1)
    d.text((cx + mw // 2 + 24, ty), t2, font=f2, fill=c2)


def t_day_results(x, logos=True):
    groups = x.get("groups") or group_by_event(x.get("matches") or [])

    def row(d, m, y, row_h):
        s1, s2 = split_score(m)
        _teams_row(d, m, y, row_h, 90, 820, f"{s1}:{s2}", WHITE, m.get("winner") or _winner(m))
        mp = [p for p in (m.get("maps") or []) if re.fullmatch(r"\d+:\d+", str(p.get("score", "")))]
        if mp:
            txt = "  ".join(f"{p.get('map', '')} {p.get('score', '')}" for p in mp)
            t, f = fit(d, txt, int(row_h * 0.38), 370, weight=600)
            d.text((835, y + (row_h - int(row_h * 0.38)) // 2 - 4), t, font=f, fill=GREY)
        elif m.get("format"):
            f = font(int(row_h * 0.38), 600)
            d.text((835, y + (row_h - int(row_h * 0.38)) // 2 - 4), m["format"], font=f, fill=GREY)
    return list_card(x.get("title") or "Итоги дня", x.get("subtitle") or ru_date(x.get("date")), groups, row,
                     x.get("footer") or "Все результаты дня")


def _winner(m):
    s1, s2 = split_score(m)
    try:
        a, b = int(s1), int(s2)
        return m.get("team1") if a > b else m.get("team2") if b > a else None
    except ValueError:
        return None


def t_announce(x, logos=True):
    groups = x.get("groups") or group_by_event(x.get("matches") or [])

    def row(d, m, y, row_h):
        tm = m.get("time") or m.get("start_msk") or ""
        f = font(int(row_h * 0.5), 900)
        d.text((92, y + (row_h - int(row_h * 0.5)) // 2 - 5), tm, font=f, fill=ORANGE)
        _teams_row(d, m, y, row_h, 230, 960, "vs", GREY)
        extra = " · ".join(v for v in (m.get("format"), m.get("stage_short") or m.get("stage")) if v)
        if extra:
            t, f2 = fit(d, extra, int(row_h * 0.36), 230, weight=600)
            d.text((1195 - tw(d, t, f2), y + (row_h - int(row_h * 0.36)) // 2 - 4), t, font=f2, fill=GREY)
    return list_card(x.get("title") or "Матчи дня", x.get("subtitle") or ru_date(x.get("date") or x.get("date_msk")),
                     groups, row, x.get("footer") or "Время московское")


def t_match_start(x, logos=True):
    im, d = base()
    header(d, "Матч начался", " · ".join(v for v in (x.get("event"), x.get("stage")) if v), live=True)
    l1 = get_logo(x.get("team1_logo"), 130, logos)
    l2 = get_logo(x.get("team2_logo"), 130, logos)
    cy = 400
    fv = font(110, 900)
    d.text((640 - tw(d, "VS", fv) // 2, cy - 70), "VS", font=fv, fill=ORANGE)
    for i, (team, lg, rk) in enumerate(((x.get("team1", "TBD"), l1, x.get("team1_rank")),
                                        (x.get("team2", "TBD"), l2, x.get("team2_rank")))):
        colx = 290 if i == 0 else 990
        ty = cy - 30
        if lg:
            paste_center(im, lg, colx, cy - 70)
            d = ImageDraw.Draw(im)
            ty = cy + 30
        yb = team_block(d, team, colx, ty, 72 if not lg else 58, 430)
        if rk:
            rf = font(26, 700)
            rt = f"#{rk} в мире"
            d.text((colx - tw(d, rt, rf) // 2, yb + 10), rt, font=rf, fill=GREY)
    fmt = x.get("format")
    if fmt:
        f = font(40, 900)
        bw = tw(d, fmt, f) + 50
        d.rounded_rectangle([640 - bw // 2, cy + 70, 640 + bw // 2, cy + 128], radius=10, outline=ORANGE, width=3)
        d.text((640 - tw(d, fmt, f) // 2, cy + 74), fmt, font=f, fill=WHITE)
    maps = x.get("maps") or []
    if maps:
        names = [m["map"] if isinstance(m, dict) else str(m) for m in maps]
        t, f = fit(d, "Карты: " + " · ".join(names), 28, 900, weight=700)
        d.text((640 - tw(d, t, f) // 2, cy + 150), t, font=f, fill=GREY)
    from common import POLLS_ENABLED
    footer(im, d, x.get("footer") or ("Голосуй за победителя в опросе ниже" if POLLS_ENABLED else None))
    return im


def t_poll_result(x, logos=True):
    im, d = base(accent_circle=False)
    header(d, "Итоги опроса", None)
    q = x.get("question") or ""
    opts = x.get("options")
    if not opts and x.get("results"):
        opts = [{"text": k, "votes": v} for k, v in x["results"].items()]
    opts = opts or []
    total = x.get("total") or x.get("total_voters") or sum(int(o.get("votes") or 0) for o in opts)
    # question (wrap to 2 lines)
    qf = font(38, 800)
    words, lines, cur = q.split(), [], ""
    for w_ in words:
        t = (cur + " " + w_).strip()
        if tw(d, t, qf) <= 1100:
            cur = t
        else:
            lines.append(cur); cur = w_
    if cur:
        lines.append(cur)
    if len(lines) > 2:
        lines = lines[:2]; lines[1], _ = fit(d, lines[1] + " …", 38, 1100, weight=800)
    y = 160
    for ln in lines:
        d.text((70, y), ln, font=qf, fill=ORANGE)
        y += 50
    n = max(len(opts), 1)
    top = y + 30
    avail = H - 110 - top
    bar_h = min(76, int(avail / n * 0.62))
    gap = min(44, (avail - bar_h * n) / max(n, 1))
    top += max(0, (avail - n * (bar_h + gap)) / 2)
    mx = max([int(o.get("votes") or 0) for o in opts] or [0])
    correct = x.get("correct")
    for i, o in enumerate(opts[:8]):
        v = int(o.get("votes") or 0)
        pct = o.get("percent")
        if pct is None:
            pct = round(100 * v / total) if total else 0
        pct = int(round(float(pct)))
        by = int(top + i * (bar_h + gap) + gap / 2)
        x0, x1 = 70, 1210
        d.rounded_rectangle([x0, by, x1, by + bar_h], radius=10, fill=(32, 35, 42))
        fw = int((x1 - x0) * pct / 100)
        lead = (v == mx and mx > 0)
        col = ORANGE if (correct and o.get("text") == correct) or (not correct and lead) else (88, 92, 102)
        if fw > 20:
            d.rounded_rectangle([x0, by, x0 + fw, by + bar_h], radius=10, fill=col)
        lf = int(bar_h * 0.55)
        pt = f"{pct}%"
        pf = font(lf, 900)
        is_ok = bool(correct and o.get("text") == correct)
        t, f = fit(d, str(o.get("text", "")), lf, 1140 - tw(d, pt, pf) - 60 - (lf if is_ok else 0), weight=800)
        d.text((x0 + 22, by + (bar_h - lf) // 2 - 5), t, font=f, fill=WHITE)
        if is_ok:  # check mark (font has no ✓ glyph)
            cx0, cy0, k = x0 + 22 + tw(d, t, f) + 18, by + bar_h // 2, lf * 0.42
            d.line([(cx0, cy0), (cx0 + k * 0.6, cy0 + k * 0.6), (cx0 + k * 1.5, cy0 - k * 0.7)], fill=WHITE, width=6, joint="curve")
        d.text((x1 - 22 - tw(d, pt, pf), by + (bar_h - lf) // 2 - 5), pt, font=pf, fill=WHITE)
    foot = x.get("footer") or (f"Проголосовало: {total}" if total else None)
    if x.get("guessed_percent") is not None:
        foot = f"{x['guessed_percent']}% угадали победителя · голосов: {total}"
    footer(im, d, foot)
    return im


def wrap(d, text, f, max_w, max_lines=2):
    words, out, cur = str(text).split(), [], ""
    for w_ in words:
        t = (cur + " " + w_).strip()
        if tw(d, t, f) <= max_w or not cur:
            cur = t
        else:
            out.append(cur); cur = w_
    if cur:
        out.append(cur)
    if len(out) > max_lines:
        last = " ".join(out[max_lines - 1:])
        while len(last) > 1 and tw(d, last + "…", f) > max_w:
            last = last[:-1]
        out = out[:max_lines - 1] + [last.rstrip(" ,.;:") + "…"]
    return out


def t_weekly(x, logos=True):
    im, d = base(alpha=0.45)
    header(d, x.get("title") or "Итоги недели", x.get("period") or x.get("subtitle"))
    lines = [str(v) for v in (x.get("lines") or [])[:5]]
    top, bottom, max_w = 210, H - 100, 1070
    for fs in range(38, 21, -2):
        f = font(fs, 700)
        wrapped = [wrap(d, ln, f, max_w) for ln in lines]
        lh, gap = int(fs * 1.22), int(fs * 0.7)
        total = sum(len(w_) for w_ in wrapped) * lh + gap * (len(lines) - 1)
        if total <= bottom - top:
            break
    y = top + max(0, (bottom - top - total) // 3)
    for wl in wrapped:
        d.polygon([(76, y + 9), (98, y + fs // 2 + 7), (76, y + fs + 5)], fill=ORANGE)
        for j, o in enumerate(wl):
            d.text((118, y + j * lh), o, font=f, fill=WHITE)
        y += len(wl) * lh + gap
    footer(im, d, x.get("footer") or "#de_csnews")
    return im


def _money(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return "$" + f"{int(v):,}".replace(",", " ")
    return str(v)


def t_tournament(x, logos=True):
    im, d = base()
    tier = x.get("tier")
    header(d, "Скоро турнир", f"{tier}-tier" if tier and len(str(tier)) == 1 else tier)
    lg = get_logo(x.get("logo") or x.get("image_url"), 150, logos)
    name = x.get("name") or x.get("event") or ""
    nx = 70
    if lg:
        paste_center(im, lg, 160, 300)
        d = ImageDraw.Draw(im)
        nx = 290
    # name, up to 2 lines
    f = font(70, 900)
    max_w = 1130 - nx
    words, lines, cur = name.split(), [], ""
    for w_ in words:
        t = (cur + " " + w_).strip()
        if tw(d, t, f) <= max_w:
            cur = t
        else:
            lines.append(cur); cur = w_
    if cur:
        lines.append(cur)
    if len(lines) > 2:
        lines = [lines[0], fit(d, " ".join(lines[1:]), 70, max_w)[0]]
    y = 300 - (len(lines) * 80) // 2 - 6
    for ln in lines:
        t, f2 = fit(d, ln, 70, max_w)
        d.text((nx, y), t, font=f2, fill=WHITE)
        y += 80
    dates = x.get("dates")
    if not dates and x.get("start_date"):
        s, e = x["start_date"][:10], (x.get("end_date") or x["start_date"])[:10]
        ds, de = datetime.fromisoformat(s), datetime.fromisoformat(e)
        dates = (f"{ds.day}–{de.day} {MONTHS[de.month - 1]}" if ds.month == de.month
                 else f"{ds.day} {MONTHS[ds.month - 1]} – {de.day} {MONTHS[de.month - 1]}")
    facts = [("ДАТЫ", dates), ("ПРИЗОВОЙ ФОНД", _money(x.get("prize") or x.get("prize_usd"))),
             ("КОМАНД", x.get("teams") if isinstance(x.get("teams"), (int, str)) else x.get("teams_count")),
             ("ГДЕ", x.get("location"))]
    facts = [(k, str(v)) for k, v in facts if v not in (None, "", 0)]
    if facts:
        n, total_w, gap = len(facts), 1140, 16
        need = [max(tw(d, v, font(44, 900)) + 60, tw(d, k, font(24, 800)) + 50) for k, v in facts]
        avail = total_w - gap * (n - 1)
        if sum(need) <= avail:
            extra = (avail - sum(need)) / n
            widths = [w_ + extra for w_ in need]
        else:   # shrink proportionally, but keep at least 190 px per box
            widths = [max(190, w_ * avail / sum(need)) for w_ in need]
            k_ = (avail - 190 * n) / max(1, sum(w_ - 190 for w_ in widths)) if sum(widths) > avail else 1
            widths = [190 + (w_ - 190) * min(1, k_) for w_ in widths]
        y, bx = 450, 70
        for (k, v), cw in zip(facts, widths):
            cw = int(cw)
            d.rounded_rectangle([bx, y, bx + cw, y + 130], radius=12, fill=(30, 33, 39), outline=(60, 64, 72), width=2)
            d.rectangle([bx, y + 18, bx + 6, y + 112], fill=ORANGE)
            kt, kf = fit(d, k, 24, cw - 40, weight=800)
            d.text((bx + 26, y + 18), kt, font=kf, fill=GREY)
            t, vf = fit(d, v, 44, cw - 44)
            d.text((bx + 26, y + 58 + (44 - vf.size) // 2), t, font=vf, fill=WHITE)
            bx += cw + gap
    footer(im, d, x.get("footer"))
    return im


def t_map_result(x, logos=True):
    """Series score after a map. maps: played maps [{map, score, winner}] + optional upcoming [{map, upcoming:true}]."""
    y = dict(x)
    n = y.get("map_number") or len([m for m in (y.get("maps") or []) if not m.get("upcoming")])
    y.setdefault("title", f"Счёт после {n}-й карты")
    y["_series"] = True
    y["maps"] = [dict(m, score=m.get("score") or ("—" if m.get("upcoming") else "")) for m in (y.get("maps") or [])]
    return t_result(y, logos)


CT_BLUE = (93, 155, 255)
T_GOLD = (234, 190, 84)


def t_half_time(x, logos=True):
    """Half-time score. x: team1, team2, half_score '9:3', map, series '1:0', team1_side 'CT'|'T', event, stage, format."""
    y = dict(x)
    y.setdefault("title", "Счёт после 1-й половины")
    y["score"] = x.get("half_score") or x.get("score")
    y["_series"] = True
    boxes = [] if is_placeholder_map(x.get("map")) else [{"map": "Карта", "score": str(x.get("map", "")).capitalize()}]
    if x.get("team1_side"):
        ct1 = x["team1_side"].upper() == "CT"
        y["_score_colors"] = (CT_BLUE, T_GOLD) if ct1 else (T_GOLD, CT_BLUE)
    if x.get("series"):
        boxes.append({"map": "Серия", "score": x["series"]})
    y["maps"] = boxes
    return t_result(y, logos)


def is_placeholder_map(name):
    """True for empty/generic map names like 'Карта 3', 'Map 2', 'TBA'."""
    import re as _re
    n = str(name or "").strip().lower()
    return (not n) or bool(_re.fullmatch(r"(карта|map)\s*\d*|tba|tbd|—|-", n))


def t_map_start(x, logos=True):
    """Map start. x: team1, team2, score (series before this map), map, map_number, pick?, maps (played/current/upcoming)."""
    y = dict(x)
    n = x.get("map_number") or 1
    mname = str(x.get("map") or "").strip()
    if is_placeholder_map(mname):
        # cybersport.ru gives no map names ("Карта 3") -> don't print "Старт 3-й карты: Карта 3"
        bo = str(x.get("format") or "").upper()
        decider = (bo == "BO3" and n == 3) or (bo == "BO5" and n == 5)
        y.setdefault("title", "Старт решающей карты" if decider else f"Старт {n}-й карты")
    else:
        y.setdefault("title", f"Старт {n}-й карты: {mname}")
    y["_series"] = True
    maps = []
    for m in (x.get("maps") or []):
        m = dict(m)
        if m.get("current") or (str(m.get("map", "")).lower() == str(x.get("map", "")).lower() and not m.get("score")):
            m["score"] = "LIVE"; m.pop("upcoming", None)
        elif m.get("upcoming"):
            m["score"] = "—"
        maps.append(m)
    y["maps"] = maps
    return t_result(y, logos)


TEMPLATES = {"result": t_result, "map_result": t_map_result, "map_start": t_map_start, "half_time": t_half_time, "day_results": t_day_results, "announce": t_announce, "match_start": t_match_start,
             "poll_result": t_poll_result, "weekly": t_weekly, "tournament": t_tournament}


def render(template, data, out, logos=True):
    from common import POLLS_ENABLED
    if template == "poll_result" and not POLLS_ENABLED:
        raise SystemExit("polls are disabled (common.POLLS_ENABLED=False): poll_result card not rendered")
    im = TEMPLATES[template](data, logos)
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    # atomic write: never leave a half-written JPEG that another run could upload (lesson 24)
    tmp = out + ".tmp"
    try:
        if out.lower().endswith(".png"):
            im.save(tmp, "PNG", optimize=True)
        else:
            im.save(tmp, "JPEG", quality=92, subsampling=0)
        os.replace(tmp, out)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("template", choices=sorted(TEMPLATES))
    ap.add_argument("--json", required=True, help="data file, or - for stdin")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--no-logos", action="store_true")
    a = ap.parse_args()
    data = json.load(sys.stdin if a.json == "-" else open(a.json, encoding="utf-8"))
    print(render(a.template, data, a.out, not a.no_logos))


if __name__ == "__main__":
    main()
