"""output.ntfy_alert: instant push when something big spikes. Runs right after the scorer.

Rules (all in pipeline.json params):
  min_heat / min_score   what counts as big (both must be met)
  cooldown_hours         no repeat alert for the same item within this window...
  rejump_score           ...unless its score has risen by at least this much since the last alert
  max_per_hour           never send more than this many alerts in any rolling hour
  source_caps_per_day    e.g. {"hackernews": 3}: at most this many a day from a high-volume source,
                         best first, so its good ones still get through without flooding you
  quiet_hours            {"start": "22:00", "end": "09:00", "tz": ...}: overnight only a life-changing spike
  quiet_override         ...gets through: {"min_heat": 30, "min_sources": 2}. Everything else waits, and
                         whatever is still climbing at the end of quiet hours is sent then.
  warmup_hours           stay quiet until Sonar has this much history (early scores are guesses)
The same entity climbing on two sources only alerts once per batch.
"""
import json
import logging
import time

from .. import db
from .notify import public_url, quiet_until, send_ntfy

log = logging.getLogger("sonar.alerts")
DEFAULTS = {"min_heat": 5, "min_score": 0, "cooldown_hours": 24, "rejump_score": 20,
            "max_per_hour": 5, "warmup_hours": 24, "source_caps_per_day": {},
            "quiet_hours": None, "quiet_override": {"min_heat": 30, "min_sources": 2}}


def same_title(title) -> str:
    """Comparable form of a title, so re-uploads of one video count as one."""
    return " ".join((title or "").lower().split())


def pick(candidates: list[dict], last_alert: dict, sent_last_hour: int, now: int, p: dict,
         sent_today: dict | None = None, seen_titles=None) -> list[dict]:
    """candidates: dicts with item_id, entity_key, source, heat, score (any order).
    last_alert: item_id -> (ts, score) of its latest alert; sent_today: source -> alerts in the last 24h;
    seen_titles: same_title() of everything alerted within the cooldown.
    Returns the ones to send. Ordered by heat, the real "how much faster than usual", so a source's
    ranking weight never hides a genuinely big spike."""
    out, entities, titles = [], set(), set(seen_titles or ())
    budget = p["max_per_hour"] - sent_last_hour
    left = {s: cap - (sent_today or {}).get(s, 0) for s, cap in p["source_caps_per_day"].items()}
    for c in sorted(candidates, key=lambda c: (-c["heat"], -c["score"])):
        if budget <= 0:
            break
        if c["heat"] < p["min_heat"] or c["score"] < p["min_score"]:
            continue
        prev = last_alert.get(c["item_id"])
        if prev and now - prev[0] < p["cooldown_hours"] * 3600 and c["score"] < (prev[1] or 0) + p["rejump_score"]:
            continue
        if c["entity_key"] and c["entity_key"] in entities:
            continue
        if same_title(c.get("title")) in titles:  # re-uploads: same video, different link
            continue
        if c.get("source") in left:
            if left[c["source"]] <= 0:
                continue
            left[c["source"]] -= 1
        entities.add(c["entity_key"])
        if same_title(c.get("title")):
            titles.add(same_title(c["title"]))
        out.append(c)
        budget -= 1
    return out


def sources_climbing(conn, batch: int, c: dict) -> int:
    """How many sources the same thing (entity or name key) is climbing on (heat >= 2) in this batch.
    Hashtag links (tag:...) don't count: #dance rising on TikTok isn't the same thing as one dance video."""
    keys = [k for k in (c.get("entity_key"), c.get("alias_key")) if k and not k.startswith("tag:")]
    if not keys:
        return 1
    q = ",".join("?" * len(keys))
    n = conn.execute(f"""SELECT COUNT(DISTINCT i.source) FROM scores s JOIN items i ON i.id = s.item_id
                         WHERE s.ts = ? AND s.heat >= 2 AND (i.entity_key IN ({q}) OR i.alias_key IN ({q}))""",
                     (batch, *keys, *keys)).fetchone()[0]
    return max(n, 1)


def message(c: dict) -> tuple[str, str, str | None, list]:
    """(title, body, click url, actions) for one alert."""
    reasons = json.loads(c["reasons_json"])
    title = f"🔥 {c['title'] or 'Untitled'}"[:200]
    body = f"{c['source']} · {c['category'] or '-'} · score {c['score']:.0f} (heat {c['heat']:.1f})\n{reasons[0]}"
    page = public_url(f"item/{c['item_id']}")
    actions = [{"action": "view", "label": "Open", "url": c["url"]}] if c["url"] else []
    if page:
        actions.append({"action": "view", "label": "Sonar", "url": page})
    return title, body, c["url"] or page, actions


def run(conn, node: dict, now: int | None = None, send=send_ntfy) -> int:
    now = now or int(time.time())
    p = {**DEFAULTS, **node.get("params", {})}
    # Sonar's age = its first scoring pass. Not the oldest snapshot: sources like Wikipedia backfill history.
    first = conn.execute("SELECT MIN(ts) FROM scores").fetchone()[0]
    if first is None or now - first < p["warmup_hours"] * 3600:
        log.info("warming up: no alerts until Sonar has %dh of history", p["warmup_hours"])
        return 0
    latest = conn.execute("SELECT MAX(ts) FROM scores").fetchone()[0]
    if latest is None:
        return 0
    candidates = [dict(r) for r in conn.execute(
        """SELECT s.item_id, s.heat, s.score, s.reasons_json, i.title, i.url, i.source, i.category, i.entity_key, i.alias_key
           FROM scores s JOIN items i ON i.id = s.item_id WHERE s.ts = ? AND s.heat >= ?""",
        (latest, p["min_heat"]))]
    last_alert = {r["item_id"]: (r["ts"], r["score"]) for r in conn.execute(
        "SELECT item_id, MAX(ts) AS ts, score FROM alerts_sent GROUP BY item_id")}
    sent_hour = conn.execute("SELECT COUNT(*) FROM alerts_sent WHERE ts > ?", (now - 3600,)).fetchone()[0]
    sent_today = dict(conn.execute(
        "SELECT i.source, COUNT(*) FROM alerts_sent a JOIN items i ON i.id = a.item_id WHERE a.ts > ? GROUP BY i.source",
        (now - 86400,)).fetchall())

    sent = 0
    quiet = quiet_until(now, p["quiet_hours"]) if p["quiet_hours"] else None
    if quiet:  # overnight: only genuinely life-changing spikes
        o = p["quiet_override"]
        candidates = [c for c in candidates if c["heat"] >= o["min_heat"]
                      and sources_climbing(conn, latest, c) >= o["min_sources"]]
    seen_titles = {same_title(r[0]) for r in conn.execute(
        "SELECT i.title FROM alerts_sent a JOIN items i ON i.id = a.item_id WHERE a.ts > ?",
        (now - p["cooldown_hours"] * 3600,)) if r[0]}
    for c in pick(candidates, last_alert, sent_hour, now, p, sent_today, seen_titles):
        title, body, click, actions = message(c)
        if quiet:
            send("🚨 " + title.removeprefix("🔥 "), "Big enough to break quiet hours.\n" + body, click=click,
                 tags=["rotating_light"], priority=5, actions=actions, urgent=True)
        else:
            send(title, body, click=click, tags=["fire"], priority=4, actions=actions)
        with conn:  # record each one as it goes, so a failure midway can't cause repeats
            conn.execute("INSERT INTO alerts_sent (item_id, ts, level, score) VALUES (?, ?, 'big', ?)",
                         (c["item_id"], now, c["score"]))
            db.emit_event(conn, "alert.sent", now=now, source=c["source"], channel="ntfy",
                          item={"id": c["item_id"], "title": c["title"], "url": c["url"]},
                          heat=round(c["heat"], 2), score=c["score"])
        sent += 1
    if sent:
        log.info("sent %d alert(s)", sent)
    return sent
