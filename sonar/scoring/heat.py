"""scorer.heat: how much faster than usual is each item growing?

Per item and metric (e.g. GitHub stars):
  v    = value now - value 24h ago (interpolated between snapshots)
  b    = mean daily growth over the 7 days before that
  heat = v / max(b, floor)
Items under 48h old have no baseline, so b is the typical 24h growth of new items from the same
source and metric. Items Sonar has watched for less than 24h get v estimated from what it has seen.

Then: item heat = best metric's heat, x early bonus (small things count more), x cross-source
bonus (the same entity climbing elsewhere). Score 0-100 is log-scaled per source so sources with
very different numbers (HN points vs GitHub stars) are comparable.
"""
import json
import logging
import math
import statistics
import time

from .. import db

log = logging.getLogger("sonar.heat")
DAY = 86400
DEFAULTS = {
    "baseline_days": 7,
    "new_item_hours": 48,
    "min_observed_hours": 1,     # need at least this much history to estimate v
    "early_weight": 1.0,
    "cross_source_bonus_pct": 25,
    "cross_source_min_heat": 2,  # an entity counts as "climbing" on a source at this heat
    "heat_for_100": 20,          # a heat that earns score 100 even on a quiet day
    "trend_min_heat": 5,         # emit trend.found when heat crosses this
    "metrics": {},               # {"github": {"stars": 5}} = source -> metric -> floor
    "source_weights": {},        # {"hackernews": 0.6} = turn down noisy, high-volume sources
}


def value_at(points: list, t: float):
    """Value at time t, linearly interpolated. None before the first point; last value after the last."""
    if not points or t < points[0][0]:
        return None
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def growth(points: list, now: int, age: float, p: dict):
    """Returns (v, b, info) for one metric's [(ts, value)], or None if there isn't enough data.
    b is None when the item is too new for its own baseline."""
    if not points:
        return None
    cur = points[-1][1]
    first_ts, first_v = points[0]
    t24 = now - DAY
    at24 = value_at(points, t24)
    info = {"now": cur, "then": at24}
    if at24 is None:
        span = now - first_ts
        if span < p["min_observed_hours"] * 3600:
            return None
        v = (cur - first_v) * DAY / span
        info.update(then=first_v, observed_hours=span / 3600)
    else:
        v = cur - at24

    b = None
    if age >= p["new_item_hours"] * 3600 and at24 is not None:
        tb = t24 - p["baseline_days"] * DAY
        vb = value_at(points, tb)
        days = p["baseline_days"]
        if vb is None:  # watched for less than the full baseline: use what we have
            vb, days = first_v, (t24 - first_ts) / DAY
        if days >= 1:
            b = max(0.0, (at24 - vb) / days)
    return v, b, info


def score_all(conn, node: dict, now: int | None = None) -> int:
    now = now or int(time.time())
    p = {**DEFAULTS, **node.get("params", {})}
    since = now - (p["baseline_days"] + 3) * DAY
    items = conn.execute(
        """SELECT * FROM items WHERE id IN (SELECT DISTINCT item_id FROM snapshots WHERE ts >= ?)""",
        (now - DAY - 3600,)).fetchall()

    # Pass 1: raw growth per item and metric.
    rows = []  # (item, metric, floor, v, b, info)
    for item in items:
        floors = p["metrics"].get(item["source"])
        if not floors:
            continue
        age = now - item["first_seen"]
        for metric, floor in floors.items():
            pts = conn.execute(
                "SELECT ts, value FROM snapshots WHERE item_id=? AND metric=? AND ts>=? AND ts<=? ORDER BY ts",
                (item["id"], metric, since, now)).fetchall()
            g = growth([tuple(r) for r in pts], now, age, p)
            if g:
                rows.append((item, metric, floor, *g))

    # Typical 24h growth of new items, per source and metric (median of the positive ones).
    new_v: dict = {}
    for item, metric, _, v, b, _ in rows:
        if b is None and v > 0:
            new_v.setdefault((item["source"], metric), []).append(v)
    typical = {k: statistics.median(vs) for k, vs in new_v.items() if len(vs) >= 3}

    # Pass 2: heat per metric; keep each item's best metric.
    best: dict = {}
    for item, metric, floor, v, b, info in rows:
        is_new = b is None
        base = typical.get((item["source"], metric), floor) if is_new else b
        heat = v / max(base, floor, 1e-9)
        if item["id"] not in best or heat > best[item["id"]]["heat"]:
            best[item["id"]] = dict(item=item, metric=metric, v=v, base=base, heat=heat,
                                    is_new=is_new, **info)

    # Early bonus, then cross-source bonus.
    for r in best.values():
        r["raw_heat"] = r["heat"]
        if r["heat"] > 0:
            r["early_mult"] = 1 + p["early_weight"] / math.log10(max(r["now"], 0) + 10)
            r["heat"] *= r["early_mult"]
    def keys(item):  # same thing = same entity (URL/repo) or same careful name (see entities.alias_key)
        return {k for k in (item["entity_key"], item["alias_key"]) if k}

    climbing: dict = {}
    for r in best.values():
        if r["raw_heat"] >= p["cross_source_min_heat"]:
            for k in keys(r["item"]):
                climbing.setdefault(k, set()).add(r["item"]["source"])
    for r in best.values():
        others = sorted(set().union(*(climbing.get(k, set()) for k in keys(r["item"]))) - {r["item"]["source"]})
        r["others"] = others
        if others and r["heat"] > 0:
            r["heat"] *= 1 + p["cross_source_bonus_pct"] / 100 * len(others)

    # Score 0-100, log-scaled per source.
    by_source: dict = {}
    for r in best.values():
        by_source.setdefault(r["item"]["source"], []).append(r)
    for group in by_source.values():
        heats = sorted(max(r["heat"], 0) for r in group)
        p99 = heats[min(len(heats) - 1, int(len(heats) * 0.99))]
        top = math.log1p(max(p99, p["heat_for_100"]))
        for r in group:
            r["score"] = round(min(100.0, 100 * math.log1p(max(r["heat"], 0)) / top), 1)

    # Source weights come last and scale both heat and score (scores are normalised per source, so
    # weighting heat alone would barely move them). Alerts, the digest and Lookout all see this.
    for r in best.values():
        r["weight"] = p["source_weights"].get(r["item"]["source"], 1.0)
        if r["heat"] > 0:
            r["heat"] *= r["weight"]
            r["score"] = round(r["score"] * r["weight"], 1)

    with conn:
        for r in best.values():
            item = r["item"]
            prev = conn.execute("SELECT heat FROM scores WHERE item_id=? ORDER BY ts DESC LIMIT 1",
                                (item["id"],)).fetchone()
            reasons = explain(r, p)
            conn.execute("INSERT INTO scores (item_id, ts, heat, score, reasons_json) VALUES (?, ?, ?, ?, ?)",
                         (item["id"], now, r["heat"], r["score"], json.dumps(reasons)))
            if r["heat"] >= p["trend_min_heat"] and (prev is None or prev["heat"] < p["trend_min_heat"]):
                db.emit_event(conn, "trend.found", now=now, source=item["source"],
                              item={"id": item["id"], "title": item["title"], "url": item["url"],
                                    "category": item["category"], "entity_key": item["entity_key"]},
                              heat=round(r["heat"], 2), score=r["score"], reasons=reasons)
    log.info("scored %d items", len(best))
    return len(best)


def fmt(x) -> str:
    x = round(x)
    return f"{x / 1000:.1f}k" if abs(x) >= 10000 else str(x)


def explain(r: dict, p: dict) -> list[str]:
    """Plain-English reasons, best first."""
    m = r["metric"]
    window = f"in {r['observed_hours']:.0f}h (scaled to 24h)" if "observed_hours" in r else "in 24h"
    if r["raw_heat"] <= 0:
        return [f"{m} not growing ({fmt(r['then'])} -> {fmt(r['now'])} {window})"]
    out = [f"{m} {fmt(r['then'])} -> {fmt(r['now'])} {window}, {r['raw_heat']:.1f}x its normal rate"]
    if r["is_new"]:
        out.append(f"new item: compared with typical new {r['item']['source']} items (+{fmt(r['base'])}/day)")
    else:
        out.append(f"normal rate: +{fmt(r['base'])} {m}/day over the previous {p['baseline_days']} days")
    if r.get("early_mult", 1) >= 1.25:
        out.append(f"still small ({fmt(r['now'])} {m}): early bonus x{r['early_mult']:.2f}")
    if r.get("weight", 1.0) != 1.0:
        out.append(f"{r['item']['source']} weighted x{r['weight']:g} (high-volume source)")
    if r["others"]:
        out.append(f"also climbing on {', '.join(r['others'])} (+{p['cross_source_bonus_pct'] * len(r['others'])}%)")
    return out


def run(conn, node):
    return score_all(conn, node)
