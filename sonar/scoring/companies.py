"""scorer.companies: one heat score per company, rolled up from every signal Sonar has on it.

Signals (each is "how many times faster than usual", like item heat):
  news       mentions in news feeds              (source.news running total)
  hn         Hacker News titles naming it        (source.news running total)
  reddit     Reddit titles naming it             (source.news running total)
  wikipedia  daily pageviews of its article      (source.wikipedia)
  launches   its Product Hunt launches' heat     (matched by domain or name)
  github     its GitHub org's repos' heat        (matched by org)
Company heat = the strongest signal, +breadth_bonus_pct for each other signal also climbing, so
attention from several directions at once ranks highest. Scores are written into the same batch as
scorer.heat (same ts), so alerts, the digest and the dashboard treat companies like any other item.
Attention signals only, not financial advice.
"""
import json
import logging
import math
import time

from .. import companies as company_list
from .. import db, watchlist
from ..entities import alias_key
from .heat import growth

log = logging.getLogger("sonar.companies")
DAY = 86400
DEFAULTS = {"floors": {"mentions": 2, "hn_mentions": 1, "reddit_mentions": 1, "views": 2000},
            "baseline_days": 7, "new_item_hours": 48, "min_observed_hours": 1,
            "breadth_bonus_pct": 25, "breadth_min_heat": 2, "heat_for_100": 20, "trend_min_heat": 5}
LABELS = {"mentions": "news", "hn_mentions": "hn", "reddit_mentions": "reddit", "views": "wikipedia"}


def metric_heat(conn, item_id: int, metric: str, floor: float, now: int, p: dict):
    """(heat, 24h growth) for one running-total metric, or None without enough data."""
    first = conn.execute("SELECT first_seen FROM items WHERE id=?", (item_id,)).fetchone()[0]
    pts = [tuple(r) for r in conn.execute(
        "SELECT ts, value FROM snapshots WHERE item_id=? AND metric=? AND ts>=? AND ts<=? ORDER BY ts",
        (item_id, metric, now - (p["baseline_days"] + 3) * DAY, now))]
    g = growth(pts, now, now - first, p)
    if not g:
        return None
    v, b, _ = g
    return v / max(b if b is not None else 0, floor), v


def run(conn, node: dict, now: int | None = None) -> int:
    p = {**DEFAULTS, **node.get("params", {})}
    p["floors"] = {**DEFAULTS["floors"], **p.get("floors", {})}
    now = now or int(time.time())
    comps = company_list.load(p.get("companies_file") or company_list.ROOT / "companies.json")
    # Same batch as scorer.heat, which ran just before this in the same pass.
    batch = conn.execute("SELECT MAX(ts) FROM scores").fetchone()[0] or now

    with conn:  # make sure the collectors follow every company
        for c in comps:
            if c.get("wikipedia"):
                watchlist.add(conn, "wikipedia", c["wikipedia"], "company", now, cap=p.get("wikipedia_cap", 200))
            if c.get("github"):
                watchlist.add(conn, "github_orgs", c["github"], "company", now, cap=p.get("github_cap", 100))

    rows = []
    for c in comps:
        signals: dict[str, tuple[float, str]] = {}  # name -> (heat, plain-English detail)
        news = conn.execute("SELECT id FROM items WHERE source='news' AND external_id=?", (c["slug"],)).fetchone()
        wiki = conn.execute("SELECT id FROM items WHERE source='wikipedia' AND external_id=?",
                            (c.get("wikipedia"),)).fetchone() if c.get("wikipedia") else None
        for item, metric in [(news, "mentions"), (news, "hn_mentions"), (news, "reddit_mentions"), (wiki, "views")]:
            if item and (h := metric_heat(conn, item[0], metric, p["floors"][metric], now, p)) and h[0] > 0:
                unit = "views" if metric == "views" else "mentions"
                signals[LABELS[metric]] = (h[0], f"{LABELS[metric]}: {h[1]:,.0f} {unit} in 24h, {h[0]:.1f}x normal")
        for label, sql, args in [
            ("launches", "i.source='producthunt' AND (i.entity_key=? OR i.alias_key=?)",
             (f"site:{c.get('domain')}", alias_key(c["name"]))),
            ("github", "i.source='github' AND i.entity_key LIKE ?", (f"github:{(c.get('github') or '~').lower()}/%",)),
        ]:
            r = conn.execute(f"SELECT MAX(s.heat), i.title FROM scores s JOIN items i ON i.id=s.item_id "
                             f"WHERE s.ts=? AND {sql}", (batch, *args)).fetchone()
            if r and r[0] and r[0] > 0:
                signals[label] = (r[0], f"{label}: {r[1]} at {r[0]:.1f}x")
        rows.append((c, signals))

    results = []
    for c, signals in rows:
        ordered = sorted(signals.items(), key=lambda kv: -kv[1][0])
        best = ordered[0][1][0] if ordered else 0.0
        breadth = sum(1 for _, (h, _) in ordered if h >= p["breadth_min_heat"])
        heat = best * (1 + p["breadth_bonus_pct"] / 100 * max(0, breadth - 1))
        reasons = [d for _, (_, d) in ordered] or ["no fresh signals"]
        if breadth > 1:
            reasons.append(f"climbing on {breadth} signals at once (+{p['breadth_bonus_pct'] * (breadth - 1)}%)")
        results.append((c, heat, reasons, {k: round(h, 2) for k, (h, _) in ordered}))

    top = math.log1p(max([h for _, h, _, _ in results] + [p["heat_for_100"]]))
    with conn:
        for c, heat, reasons, signals in results:
            item_id = db.upsert_item(conn, node["id"], {
                "kind": "company", "external_id": c["slug"], "title": c["name"],
                "url": f"https://{c['domain']}" if c.get("domain") else None, "category": "company",
                "entity_key": c["key"], "alias_key": alias_key(c["name"]),
                "meta": {"signals": signals, "ticker": c.get("ticker")}}, now)
            score = round(min(100.0, 100 * math.log1p(max(heat, 0)) / top), 1)
            prev = conn.execute("SELECT heat FROM scores WHERE item_id=? AND ts<? ORDER BY ts DESC LIMIT 1",
                                (item_id, batch)).fetchone()
            conn.execute("DELETE FROM scores WHERE item_id=? AND ts=?", (item_id, batch))
            conn.execute("INSERT INTO scores (item_id, ts, heat, score, reasons_json) VALUES (?, ?, ?, ?, ?)",
                         (item_id, batch, heat, score, json.dumps(reasons)))
            if heat >= p["trend_min_heat"] and (prev is None or prev[0] < p["trend_min_heat"]):
                db.emit_event(conn, "trend.found", now=now, source=node["id"],
                              item={"id": item_id, "title": c["name"], "url": f"https://{c.get('domain')}",
                                    "category": "company", "entity_key": c["key"]},
                              heat=round(heat, 2), score=score, reasons=reasons)
    log.info("scored %d companies", len(results))
    return len(results)
