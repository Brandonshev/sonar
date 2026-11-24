"""Sonar dashboard: a small password-protected web app using only the standard library.

  python -m sonar.web                 serve (host/port from the dashboard node in pipeline.json)
  python -m sonar.web --set-password  set the login password (stores a scrypt hash in .env)

Bind to 127.0.0.1 and put HTTPS in front (Cloudflare Tunnel or Caddy); see DEPLOY.md.
No JavaScript: pages are server-rendered HTML with inline SVG charts.
"""
import argparse
from contextlib import closing
import getpass
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from zoneinfo import ZoneInfo

from . import ROOT, VERSION, db, health, load_env, pipeline, setup_logging

log = logging.getLogger("sonar.web")
DAY = 86400
SESSION_DAYS = 30
TZ = ZoneInfo("Australia/Sydney")
DEFAULT_TABS = {"Social": ["reddit", "tiktok", "instagram"], "YouTube": ["youtube"],
                "Tech products": ["producthunt", "hackernews"], "GitHub": ["github"],
                "Companies": ["companies", "news"], "Other": []}


# ---------- auth ----------

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, TypeError):
        return False


def make_session(secret: str, now: int) -> str:
    exp = str(now + SESSION_DAYS * DAY)
    return f"{exp}.{hmac.new(secret.encode(), exp.encode(), 'sha256').hexdigest()}"


def valid_session(token: str, secret: str, now: int) -> bool:
    exp, _, sig = (token or "").partition(".")
    good = hmac.new(secret.encode(), exp.encode(), "sha256").hexdigest()
    return bool(exp.isdigit() and hmac.compare_digest(sig, good) and int(exp) > now)


class Throttle:
    """At most `limit` failed logins per IP in `window` seconds."""
    def __init__(self, limit=5, window=900):
        self.limit, self.window, self.fails, self.lock = limit, window, {}, threading.Lock()

    def blocked(self, ip, now):
        with self.lock:
            self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < self.window]
            return len(self.fails[ip]) >= self.limit

    def fail(self, ip, now):
        with self.lock:
            self.fails.setdefault(ip, []).append(now)


# ---------- small helpers ----------

def ago(ts, now) -> str:
    return health.ago(ts, now) if ts else "never"


def sydney(ts) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%a %-d %b, %-I:%M %p")


def num(x) -> str:
    x = float(x)
    return f"{x / 1e6:.1f}M" if abs(x) >= 1e6 else f"{x / 1e3:.1f}k" if abs(x) >= 1e4 else f"{x:.0f}"


def spark(points: list, w=120, h=32, cls="spark") -> str:
    """Tiny SVG line + area for [(ts, value)]."""
    if len(points) < 2:
        return f'<svg class="{cls}" viewBox="0 0 {w} {h}" aria-hidden="true"></svg>'
    t0, t1 = points[0][0], points[-1][0]
    vs = [v for _, v in points]
    lo, hi = min(vs), max(vs)
    xy = [((t - t0) / ((t1 - t0) or 1) * w, h - 2 - (v - lo) / ((hi - lo) or 1) * (h - 4)) for t, v in points]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    return (f'<svg class="{cls}" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">'
            f'<polygon points="0,{h} {line} {w},{h}" class="area"/><polyline points="{line}" class="line"/></svg>')


def chart(points: list, label: str) -> str:
    """Larger chart with min/max and start/end labels."""
    if len(points) < 2:
        return f'<div class="chart empty">{escape(label)}: not enough history yet</div>'
    lo, hi = min(v for _, v in points), max(v for _, v in points)
    return (f'<figure class="chart"><figcaption>{escape(label)} <span>{num(points[0][1])} → '
            f'{num(points[-1][1])}</span></figcaption>{spark(points, 600, 140, "big")}'
            f'<div class="axis"><span>{sydney(points[0][0])}</span><span>low {num(lo)} · high {num(hi)}</span>'
            f'<span>{sydney(points[-1][0])}</span></div></figure>')


def thin(points: list, buckets: int) -> list:
    """Keep the last point of each time bucket so charts stay light."""
    if len(points) <= buckets:
        return points
    t0, t1 = points[0][0], points[-1][0]
    size = (t1 - t0) / buckets or 1
    out = {}
    for t, v in points:
        out[int((t - t0) / size)] = (t, v)
    return [out[k] for k in sorted(out)]


# ---------- pages ----------

CSS = """
:root{--bg:#f5f7f6;--panel:#fff;--line:#e2e8e5;--fg:#0e1a1f;--muted:#5b6d74;--accent:#0b9467;--hot:#dc4c14;--chip:#eef3f1}
@media (prefers-color-scheme:dark){:root{--bg:#090f12;--panel:#0f181c;--line:#1c2a30;--fg:#e4edf0;--muted:#7d929a;--accent:#3ee0a1;--hot:#ff7d47;--chip:#15232a}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
a{color:inherit;text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.bar{max-width:920px;margin:0 auto;padding:12px 16px 0;display:flex;align-items:center;gap:10px}
.brand{display:flex;align-items:center;gap:8px;font-weight:650;letter-spacing:-.01em;font-size:17px}
.brand svg{width:22px;height:22px;color:var(--accent)}
.bar .meta{margin-left:auto;color:var(--muted);font-size:12px}
nav{max-width:920px;margin:0 auto;padding:8px 12px 0;display:flex;gap:2px;overflow-x:auto;scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
nav a{white-space:nowrap;padding:8px 10px 10px;color:var(--muted);border-bottom:2px solid transparent;font-size:14px}
nav a.on{color:var(--fg);border-color:var(--accent)}nav a:hover{text-decoration:none;color:var(--fg)}
main{max-width:920px;margin:0 auto;padding:16px}
h1{font-size:22px;letter-spacing:-.02em;margin:8px 0 4px}h2{font-size:16px;margin:28px 0 8px}
.sub{color:var(--muted);font-size:13px;margin:0 0 16px}
.list{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}
.row{display:grid;grid-template-columns:52px 1fr 120px;gap:12px;align-items:center;padding:12px 2px;border-bottom:1px solid var(--line)}
.score{font:600 22px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--hot);text-align:right}
.score small{display:block;font-size:11px;color:var(--muted);font-weight:500;margin-top:4px}
.t{font-weight:560;overflow-wrap:anywhere}.why{color:var(--muted);font-size:13px;margin-top:2px}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.chip{font-size:11px;padding:2px 7px;border-radius:99px;background:var(--chip);color:var(--muted)}
.chip.src{color:var(--accent)}
svg.spark{width:120px;height:32px}svg .line{fill:none;stroke:var(--accent);stroke-width:1.6;vector-effect:non-scaling-stroke}
svg .area{fill:var(--accent);opacity:.12}
.chart{margin:12px 0 20px;padding:12px;background:var(--panel);border:1px solid var(--line);border-radius:12px}
.chart figcaption{font-weight:560;margin-bottom:6px}.chart figcaption span{color:var(--muted);font-weight:400;margin-left:6px}
svg.big{width:100%;height:140px;display:block}.chart.empty{color:var(--muted);font-size:14px}
.axis{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:11px;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--muted);font-weight:500}
td,th{padding:9px 6px;border-bottom:1px solid var(--line);vertical-align:top}
.st{font:600 11px ui-monospace,Menlo,monospace;padding:2px 7px;border-radius:6px;background:var(--chip)}
.st.OK{color:var(--accent)}.st.FAILING,.st.STALE{color:#fff;background:var(--hot)}.st.OFF,.st.WAITING{color:var(--muted)}
.err{color:var(--muted);font-size:12px;overflow-wrap:anywhere}td:nth-child(2),td:nth-child(3){white-space:nowrap}
.empty-state{padding:40px 8px;color:var(--muted);text-align:center}
.btn{display:inline-block;padding:10px 16px;border-radius:10px;background:var(--accent);color:var(--bg);font-weight:600;border:0;font-size:15px}
.login{max-width:340px;margin:12vh auto;padding:24px;background:var(--panel);border:1px solid var(--line);border-radius:16px}
.login input{width:100%;font-size:16px;padding:11px 12px;margin:12px 0;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.login .btn{width:100%}.bad{color:var(--hot);font-size:14px}
footer{max-width:920px;margin:24px auto 40px;padding:0 16px;color:var(--muted);font-size:12px}
@media (max-width:560px){.row{grid-template-columns:44px 1fr}.row svg.spark{grid-column:2;width:100%;height:28px}.score{font-size:19px}
.hide-sm{display:none}}
"""

LOGO = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">'
        '<circle cx="12" cy="12" r="9.5" opacity=".35"/><circle cx="12" cy="12" r="5.5" opacity=".6"/>'
        '<path d="M12 12 L19 6"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/>'
        '<circle cx="16.5" cy="8.2" r="1.3" fill="currentColor" stroke="none"/></svg>')


def layout(title: str, body: str, tab: str = "", tabs=(), meta: str = "", nav: bool = True) -> str:
    items = [("Top", "/")] + [(t, f"/tab/{quote(t)}") for t in tabs] + [("Digests", "/digests"), ("Health", "/health")]
    navhtml = "".join(f'<a href="{u}" class="{"on" if n == tab else ""}">{escape(n)}</a>' for n, u in items)
    head = (f'<header><div class="bar"><a class="brand" href="/">{LOGO}Sonar</a>'
            f'<span class="meta">{meta}</span></div><nav>{navhtml}</nav></header>') if nav else ""
    return (f'<!doctype html><html lang="en-AU"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<meta name="color-scheme" content="light dark"><title>{escape(title)} · Sonar</title>'
            f'<link rel="icon" href="/favicon.svg"><style>{CSS}</style></head><body>{head}'
            f'<main>{body}</main><footer>Sonar v{VERSION} · information only, not financial advice</footer></body></html>')


class Site:
    """All page rendering; one instance shared by request threads (each request opens its own DB connection)."""

    def __init__(self, data: dict, db_path=None, digests_dir: Path | None = None):
        self.data = data
        self.db_path = db_path
        self.digests = digests_dir or ROOT / "data" / "digests"
        dash = next((n for n in data["nodes"] if n["type"] == "output.dashboard"), {})
        self.params = dash.get("params", {})
        self.tabs = self.params.get("tabs", DEFAULT_TABS)
        heat = next((n for n in data["nodes"] if n["type"] == "scorer.heat"), {})
        self.metric = {src: next(iter(m)) for src, m in heat.get("params", {}).get("metrics", {}).items() if m}

    def conn(self):
        return db.connect(self.db_path)

    def sources_for(self, tab: str) -> tuple[list, bool]:
        """(sources, negate). 'Other' (an empty list) means every source no other tab lists."""
        srcs = self.tabs.get(tab)
        if srcs:
            return srcs, False
        return [s for v in self.tabs.values() for s in v], True

    def top(self, conn, now, tab=None, limit=40):
        latest = conn.execute("SELECT MAX(ts) FROM scores").fetchone()[0]
        if latest is None:
            return [], None
        sql = """SELECT i.*, s.heat, s.score, s.reasons_json FROM scores s JOIN items i ON i.id = s.item_id
                 WHERE s.ts = ? AND s.score > 0"""
        args: list = [latest]
        if tab:
            srcs, neg = self.sources_for(tab)
            sql += f" AND i.source {'NOT ' if neg else ''}IN ({','.join('?' * len(srcs)) or 'NULL'})"
            args += srcs
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY s.score DESC, s.heat DESC LIMIT ?", (*args, limit))]
        keys = {r["entity_key"] for r in rows if r["entity_key"]}
        also = {}
        if keys:
            for k, s in conn.execute(f"SELECT entity_key, source FROM items WHERE entity_key IN ({','.join('?' * len(keys))})",
                                     tuple(keys)):
                also.setdefault(k, set()).add(s)
        for r in rows:
            r["sources"] = sorted(also.get(r["entity_key"], {r["source"]}))
            m = self.metric.get(r["source"])
            pts = conn.execute("SELECT ts, value FROM snapshots WHERE item_id=? AND metric=? AND ts>=? ORDER BY ts",
                               (r["id"], m, now - 7 * DAY)).fetchall() if m else []
            r["spark"] = thin([tuple(p) for p in pts], 42)
        return rows, latest

    def list_html(self, rows) -> str:
        if not rows:
            return ('<div class="empty-state">Nothing climbing yet. Sonar needs about a day of history '
                    'before scores mean much.</div>')
        out = []
        for r in rows:
            why = json.loads(r["reasons_json"])[0]
            chips = "".join(f'<span class="chip src">{escape(s)}</span>' for s in r["sources"])
            if r["category"]:
                chips += f'<span class="chip">{escape(r["category"])}</span>'
            out.append(
                f'<li class="row"><div class="score">{r["score"]:.0f}<small>{r["heat"]:.1f}×</small></div>'
                f'<div><a class="t" href="/item/{r["id"]}">{escape(r["title"] or "Untitled")}</a>'
                f'<div class="why">{escape(why)}</div><div class="chips">{chips}</div></div>'
                f'{spark(r["spark"])}</li>')
        return f'<ul class="list">{"".join(out)}</ul>'

    def meta(self, latest, now):
        return f"scored {ago(latest, now)}" if latest else "no scores yet"

    def page_top(self, now, tab=None):
        with closing(self.conn()) as conn:
            rows, latest = self.top(conn, now, tab, self.params.get("top_n", 40))
        title = tab or "Top climbers"
        if not tab:
            sub = "Everything, ranked by score"
        else:
            srcs, neg = self.sources_for(tab)
            sub = "Every source not in another tab" if neg else "Sources: " + ", ".join(srcs)
        body = f"<h1>{escape(title)}</h1><p class='sub'>{escape(sub)} · score 0–100, × = heat</p>{self.list_html(rows)}"
        return layout(title, body, tab or "Top", self.tabs, self.meta(latest, now))

    def page_item(self, now, item_id: int):
        with closing(self.conn()) as conn:
            item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not item:
                return None
            same = [dict(r) for r in conn.execute(
                "SELECT * FROM items WHERE entity_key = ? ORDER BY source", (item["entity_key"],))] if item["entity_key"] else [dict(item)]
            ids = [s["id"] for s in same]
            q = ",".join("?" * len(ids))
            charts = []
            for s in same:
                for (m,) in conn.execute("SELECT DISTINCT metric FROM snapshots WHERE item_id=?", (s["id"],)):
                    pts = conn.execute("SELECT ts, value FROM snapshots WHERE item_id=? AND metric=? AND ts>=? ORDER BY ts",
                                       (s["id"], m, now - 30 * DAY)).fetchall()
                    charts.append(chart(thin([tuple(p) for p in pts], 120), f"{s['source']} {m}"))
            score = conn.execute("SELECT * FROM scores WHERE item_id=? ORDER BY ts DESC LIMIT 1", (item_id,)).fetchone()
            hist = conn.execute("SELECT ts, score FROM scores WHERE item_id=? AND ts>=? ORDER BY ts",
                                (item_id, now - 7 * DAY)).fetchall()
            alerts = conn.execute(f"SELECT a.ts, a.score, i.source FROM alerts_sent a JOIN items i ON i.id=a.item_id "
                                  f"WHERE a.item_id IN ({q}) ORDER BY a.ts DESC LIMIT 50", ids).fetchall()
        reasons = "".join(f"<li>{escape(x)}</li>" for x in json.loads(score["reasons_json"])) if score else ""
        head = (f'<h1>{escape(item["title"] or "Untitled")}</h1><p class="sub">'
                + (f'score <b>{score["score"]:.0f}</b> · heat {score["heat"]:.1f}× · ' if score else "")
                + f'first seen {sydney(item["first_seen"])}</p>' + (f"<ul class='why'>{reasons}</ul>" if reasons else ""))
        srcs = "".join(
            f'<tr><td><a href="/item/{s["id"]}">{escape(s["source"])}</a></td><td>{escape(s["category"] or "")}</td>'
            f'<td>' + (f'<a href="{escape(s["url"])}" rel="noreferrer" target="_blank">open ↗</a>' if s["url"] else "") + '</td></tr>'
            for s in same)
        alert_rows = "".join(f"<tr><td>{sydney(a['ts'])}</td><td>{escape(a['source'])}</td><td>{(a['score'] or 0):.0f}</td></tr>"
                             for a in alerts) or "<tr><td colspan=3 class='err'>No alerts sent for this item.</td></tr>"
        body = (head + chart([tuple(h) for h in hist], "score, last 7 days") + "<h2>History</h2>" + "".join(charts)
                + f"<h2>On {len(same)} source{'s' if len(same) != 1 else ''}</h2><table>{srcs}</table>"
                + f"<h2>Alerts</h2><table><tr><th>Sent</th><th>Source</th><th>Score</th></tr>{alert_rows}</table>")
        return layout(item["title"] or "Item", body, "", self.tabs)

    def page_digests(self):
        files = sorted(self.digests.glob("????-??-??.html"), reverse=True) if self.digests.exists() else []
        rows = "".join(f'<li class="row" style="grid-template-columns:1fr"><a class="t" href="/digest/{f.stem}">'
                       f'{datetime.strptime(f.stem, "%Y-%m-%d").strftime("%A %-d %B %Y")}</a></li>' for f in files)
        body = "<h1>Digests</h1><p class='sub'>One every morning at 7:00 Sydney time</p>" + (
            f'<ul class="list">{rows}</ul>' if rows else '<div class="empty-state">No digests yet.</div>')
        return layout("Digests", body, "Digests", self.tabs)

    def digest_file(self, day: str):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return None
        f = self.digests / f"{day}.html"
        return f.read_text() if f.is_file() else None

    def page_health(self, now):
        with closing(self.conn()) as conn:
            rows = health.rows(conn, self.data, now)
            totals = conn.execute("SELECT (SELECT COUNT(*) FROM items), (SELECT COUNT(*) FROM snapshots), "
                                  "(SELECT MIN(ts) FROM snapshots)").fetchone()
        trs = "".join(
            f'<tr><td>{escape(r["label"])}</td><td><span class="st {r["status"]}">{r["status"]}</span></td>'
            f'<td>{r["last_ok"]}</td><td class="hide-sm">{r["items"]}</td>'
            f'<td class="err">{escape(r["error"][:300])}</td></tr>' for r in rows)
        since = f" · collecting since {sydney(totals[2])}" if totals[2] else ""
        body = (f"<h1>Health</h1><p class='sub'>{totals[0]:,} items · {totals[1]:,} snapshots{since}</p>"
                f"<table><tr><th>Node</th><th>Status</th><th>Last OK</th><th class='hide-sm'>Items</th><th>Last error</th></tr>{trs}</table>")
        return layout("Health", body, "Health", self.tabs)

    def page_login(self, error=""):
        bad = f'<p class="bad">{escape(error)}</p>' if error else ""
        body = (f'<form class="login" method="post" action="/login"><div class="brand">{LOGO}Sonar</div>{bad}'
                f'<input type="password" name="password" placeholder="Password" autocomplete="current-password" autofocus required>'
                f'<button class="btn">Sign in</button></form>')
        return layout("Sign in", body, nav=False)


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    site: Site
    throttle = Throttle()
    server_version = "Sonar"
    sys_version = ""

    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_ip(), fmt % args)

    def client_ip(self):
        # Only trusted because the server binds to localhost behind the tunnel/proxy.
        return self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For", "").split(",")[0].strip() \
            or self.client_address[0]

    def send(self, code: int, body: str = "", ctype="text/html; charset=utf-8", headers=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def redirect(self, to: str, headers=None):
        self.send(303, "", headers={"Location": to, **(headers or {})})

    def cookie(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sonar_session":
                return v
        return ""

    def authed(self, now) -> bool:
        secret = os.environ.get("SONAR_SECRET", "")
        return bool(secret) and valid_session(self.cookie(), secret, now)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        now = int(time.time())
        path = urlsplit(self.path).path
        if path == "/favicon.svg":
            return self.send(200, LOGO.replace("currentColor", "#3ee0a1"), "image/svg+xml")
        if path == "/login":
            return self.send(200, self.site.page_login())
        if path == "/logout":
            return self.redirect("/login", {"Set-Cookie": "sonar_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"})
        if not self.authed(now):
            return self.redirect("/login")
        try:
            if path == "/":
                return self.send(200, self.site.page_top(now))
            if m := re.fullmatch(r"/tab/([^/]+)", path):
                tab = unquote(m[1])
                if tab in self.site.tabs:
                    return self.send(200, self.site.page_top(now, tab))
            if m := re.fullmatch(r"/item/(\d{1,12})", path):
                page = self.site.page_item(now, int(m[1]))
                if page:
                    return self.send(200, page)
            if path == "/digests":
                return self.send(200, self.site.page_digests())
            if m := re.fullmatch(r"/digest/([0-9-]{10})", path):
                page = self.site.digest_file(m[1])
                if page:
                    return self.send(200, page)
            if path == "/health":
                return self.send(200, self.site.page_health(now))
            self.send(404, layout("Not found", "<h1>Not found</h1>", "", self.site.tabs))
        except Exception:
            log.exception("error rendering %s", path)
            self.send(500, layout("Error", "<h1>Something broke</h1><p class='sub'>It's in the log.</p>", "", self.site.tabs))

    def do_POST(self):
        now = int(time.time())
        if urlsplit(self.path).path != "/login":
            return self.send(404, "")
        ip = self.client_ip()
        if self.throttle.blocked(ip, now):
            return self.send(429, self.site.page_login("Too many attempts. Try again in 15 minutes."))
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = parse_qs(self.rfile.read(length).decode(errors="replace"))
        stored, secret = os.environ.get("SONAR_DASH_PASSWORD_HASH", ""), os.environ.get("SONAR_SECRET", "")
        if not stored or not secret:
            return self.send(503, self.site.page_login("No password set yet. Run: python -m sonar.web --set-password"))
        if not check_password(form.get("password", [""])[0], stored):
            self.throttle.fail(ip, now)
            log.warning("failed login from %s", ip)
            return self.send(401, self.site.page_login("Wrong password."))
        secure = "; Secure" if os.environ.get("SONAR_PUBLIC_URL", "").startswith("https://") else ""
        self.redirect("/", {"Set-Cookie": f"sonar_session={make_session(secret, now)}; Max-Age={SESSION_DAYS * DAY}; "
                                          f"Path=/; HttpOnly; SameSite=Lax{secure}"})


def set_password(env_path: Path = ROOT / ".env") -> int:
    pw = getpass.getpass("New dashboard password: ")
    if len(pw) < 10:
        print("Use at least 10 characters.")
        return 1
    if pw != getpass.getpass("Again: "):
        print("They don't match.")
        return 1
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    lines = [l for l in lines if not l.startswith(("SONAR_DASH_PASSWORD_HASH=", "SONAR_SECRET="))]
    lines += [f"SONAR_DASH_PASSWORD_HASH={hash_password(pw)}", f"SONAR_SECRET={secrets.token_hex(32)}"]
    env_path.write_text("\n".join(lines) + "\n")
    os.chmod(env_path, 0o600)
    print("Password set. Restart the dashboard: sudo systemctl restart sonar-web")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sonar.web")
    ap.add_argument("--set-password", action="store_true")
    args = ap.parse_args(argv)
    if args.set_password:
        return set_password()
    load_env()
    setup_logging()
    data = pipeline.load()
    Handler.site = Site(data)
    p = Handler.site.params
    host, port = p.get("host", "127.0.0.1"), int(p.get("port", 8390))
    if not os.environ.get("SONAR_DASH_PASSWORD_HASH"):
        log.warning("no dashboard password set: nobody can sign in until you run --set-password")
    log.info("Sonar v%s dashboard on http://%s:%d", VERSION, host, port)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
