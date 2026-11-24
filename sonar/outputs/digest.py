"""output.digest: the daily round-up. Scheduled with "at"/"tz" in pipeline.json (7:00 Sydney).

Builds an HTML page (data/digests/YYYY-MM-DD.html, served by the dashboard later) with the top
climbers per source, new entries, broken sources and the Sonar version. Then sends a short ntfy
message linking to it, and emails the page too if SMTP is set up in .env.
"""
import json
import logging
import time
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import ROOT, VERSION
from .notify import email_configured, public_url, send_email, send_ntfy

log = logging.getLogger("sonar.digest")
DAY = 86400
DEFAULTS = {"top_per_source": 10, "new_entries": 10, "email": True}


def gather(conn, now: int, p: dict) -> dict:
    since = now - DAY
    best = [dict(r) for r in conn.execute(
        """SELECT i.id, i.source, i.category, i.title, i.url, i.first_seen,
                  MAX(s.score) AS score, s.heat, s.reasons_json
           FROM scores s JOIN items i ON i.id = s.item_id
           WHERE s.ts >= ? GROUP BY i.id ORDER BY score DESC, s.heat DESC""", (since,))]
    top: dict = {}
    for r in best:
        if r["score"] > 0 and len(top.setdefault(r["source"], [])) < p["top_per_source"]:
            top[r["source"]].append(r)
    new = [r for r in best if r["first_seen"] >= since and r["score"] > 0][:p["new_entries"]]
    broken = [dict(r) for r in conn.execute(
        """SELECT source, last_ok, last_error, last_error_msg FROM source_health
           WHERE last_error >= ? AND (last_ok IS NULL OR last_error > last_ok)""", (since,))]
    trends = conn.execute("SELECT COUNT(*) FROM events WHERE type='trend.found' AND ts >= ?", (since,)).fetchone()[0]
    alerts = conn.execute("SELECT COUNT(*) FROM alerts_sent WHERE ts >= ?", (since,)).fetchone()[0]
    return {"top": top, "new": new, "broken": broken, "trends": trends, "alerts": alerts,
            "scored": len(best)}


def row(r: dict) -> str:
    why = escape(json.loads(r["reasons_json"])[0])
    title = escape(r["title"] or "Untitled")
    link = f'<a href="{escape(r["url"])}">{title}</a>' if r["url"] else title
    return (f"<tr><td class=n>{r['score']:.0f}</td><td class=n>{r['heat']:.1f}</td>"
            f"<td>{link}<div class=why>{why}</div></td><td>{escape(r['category'] or '')}</td></tr>")


def table(rows: list[dict]) -> str:
    if not rows:
        return "<p class=muted>Nothing climbing.</p>"
    head = "<tr><th>Score</th><th>Heat</th><th>Item</th><th>Category</th></tr>"
    return f"<table>{head}{''.join(row(r) for r in rows)}</table>"


def render(d: dict, day: str) -> str:
    sections = "".join(f"<h2>{escape(src)}</h2>{table(rows)}" for src, rows in sorted(d["top"].items()))
    broken = "".join(f"<li><b>{escape(b['source'])}</b>: {escape(b['last_error_msg'] or '')}</li>" for b in d["broken"])
    return f"""<!doctype html><html lang="en-AU"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sonar digest {day}</title>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea; --hot:#d9480f; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111; --fg:#f2f2f7; --muted:#98989d; --line:#2c2c2e; --hot:#ff8a4c; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.45 -apple-system, system-ui, sans-serif;
       max-width:860px; margin:0 auto; padding:24px 16px; }}
h1 {{ margin:0 0 4px; }} h2 {{ margin-top:32px; text-transform:capitalize; }}
table {{ width:100%; border-collapse:collapse; }} th {{ text-align:left; color:var(--muted); font-weight:500; }}
td, th {{ padding:8px 6px; border-bottom:1px solid var(--line); vertical-align:top; }}
td.n {{ font-variant-numeric:tabular-nums; color:var(--hot); width:3.5em; }}
a {{ color:inherit; }} .why, .muted {{ color:var(--muted); font-size:13px; }}
footer {{ margin-top:40px; color:var(--muted); font-size:12px; }}
</style></head><body>
<h1>Sonar digest</h1>
<p class=muted>{day} · {d['trends']} new trends · {d['alerts']} alerts sent · {d['scored']} items scored</p>
<h2>New entries</h2>{table(d['new'])}
{sections}
<h2>Sources that broke</h2>{f'<ul>{broken}</ul>' if broken else '<p class=muted>All sources healthy.</p>'}
<footer>Sonar v{VERSION} · Attention signals only, not financial advice.</footer>
</body></html>"""


def run(conn, node: dict, now: int | None = None, send=send_ntfy, email=send_email) -> int:
    now = now or int(time.time())
    p = {**DEFAULTS, **node.get("params", {})}
    day = datetime.fromtimestamp(now, ZoneInfo(node.get("tz", "UTC"))).strftime("%Y-%m-%d")
    d = gather(conn, now, p)
    html = render(d, day)
    out = ROOT / "data" / "digests" / f"{day}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)

    leaders = sorted((r for rows in d["top"].values() for r in rows), key=lambda r: -r["score"])[:3]
    lines = [f"{d['trends']} new trends, {d['scored']} items scored."]
    lines += [f"• {r['title']} ({r['source']}, {r['score']:.0f})" for r in leaders]
    if d["broken"]:
        lines.append(f"⚠️ Broken: {', '.join(b['source'] for b in d['broken'])}")
    page = public_url(f"digest/{day}")
    if not page:
        lines.append(f"Saved on the server: {out}")
    send(f"Sonar digest {day}", "\n".join(lines), click=page, tags=["newspaper"])
    if p["email"] and email_configured():
        email(f"Sonar digest {day}", html, "\n".join(lines))
    log.info("digest %s: %d sources, %d new entries", day, len(d["top"]), len(d["new"]))
    return d["scored"]
