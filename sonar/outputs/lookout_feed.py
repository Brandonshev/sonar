"""output.lookout_feed: trend events for Lookout (a separate program) to read at GET /api/trends.

After each scoring pass this writes a standard trend.found event (feed "lookout") for every item in
the node's categories whose score reaches min_score. The same item isn't sent again within
cooldown_hours unless its score has risen by rejump_score. The API only serves these stored events,
so Lookout can page with ?since=<ts> and never sees one twice. Schema: API.md.
"""
import json
import time

from .. import VERSION, db

DEFAULTS = {"categories": ["social.video"], "min_score": 60, "cooldown_hours": 24, "rejump_score": 20,
            "max_examples": 5}
FEED = "lookout"


def examples(conn, item, limit: int) -> tuple[list[str], list[str]]:
    """(sources, example URLs): the item plus others that are the same thing (entity or name key)."""
    rows = conn.execute(
        """SELECT i.source, i.url, COALESCE(s.score, 0) AS score FROM items i
           LEFT JOIN scores s ON s.item_id = i.id AND s.ts = (SELECT MAX(ts) FROM scores)
           WHERE i.id = ? OR (? IS NOT NULL AND i.entity_key = ?) OR (? IS NOT NULL AND i.alias_key = ?)
           ORDER BY i.id = ? DESC, score DESC LIMIT 50""",
        (item["id"], item["entity_key"], item["entity_key"], item["alias_key"], item["alias_key"], item["id"])).fetchall()
    sources = sorted({r["source"] for r in rows})
    urls = list(dict.fromkeys(r["url"] for r in rows if r["url"]))[:limit]
    return sources, urls


def run(conn, node: dict, now: int | None = None) -> int:
    p = {**DEFAULTS, **node.get("params", {})}
    now = now or int(time.time())
    batch = conn.execute("SELECT MAX(ts) FROM scores").fetchone()[0]
    if batch is None:
        return 0
    cats = p["categories"]
    rows = conn.execute(
        f"""SELECT i.*, s.heat, s.score, s.reasons_json FROM scores s JOIN items i ON i.id = s.item_id
            WHERE s.ts = ? AND s.score >= ? AND i.category IN ({','.join('?' * len(cats))})
            ORDER BY s.score DESC""", (batch, p["min_score"], *cats)).fetchall()
    sent = 0
    with conn:
        for r in rows:
            last = conn.execute(
                """SELECT ts, json_extract(payload_json, '$.score') FROM events WHERE type = 'trend.found'
                   AND json_extract(payload_json, '$.feed') = ? AND json_extract(payload_json, '$.item.id') = ?
                   ORDER BY ts DESC LIMIT 1""", (FEED, r["id"])).fetchone()
            if last and now - last[0] < p["cooldown_hours"] * 3600 and r["score"] < (last[1] or 0) + p["rejump_score"]:
                continue
            sources, urls = examples(conn, r, p["max_examples"])
            db.emit_event(conn, "trend.found", now=now, feed=FEED, source=r["source"],
                          item={"id": r["id"], "title": r["title"], "url": r["url"], "category": r["category"],
                                "kind": r["kind"], "entity_key": r["entity_key"]},
                          sources=sources, score=r["score"], heat=round(r["heat"], 2),
                          reasons=json.loads(r["reasons_json"]), examples=urls, sonar_version=VERSION)
            sent += 1
    return sent


def read(conn, node: dict, category: str | None, min_score: float | None, since: int, limit: int,
         after_id: int = 0) -> list[dict]:
    """Stored Lookout events after `since` (and after event id `after_id`), oldest first,
    filtered to the node's categories. after_id is the safe cursor when many events share a ts."""
    p = {**DEFAULTS, **node.get("params", {})}
    cats = [category] if category else p["categories"]
    out = []
    for eid, payload in conn.execute(
            f"""SELECT id, payload_json FROM events WHERE type = 'trend.found' AND ts > ? AND id > ?
                AND json_extract(payload_json, '$.feed') = ?
                AND json_extract(payload_json, '$.item.category') IN ({','.join('?' * len(cats))})
                AND json_extract(payload_json, '$.score') >= ?
                ORDER BY ts, id LIMIT ?""", (since, after_id, FEED, *cats, max(min_score or 0, p["min_score"]), limit)):
        out.append({"id": eid, **json.loads(payload)})
    return out
