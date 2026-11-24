"""SQLite storage. Schema changes go in MIGRATIONS (append only, never edit old ones)."""
import json
import os
import sqlite3
import time
from pathlib import Path

from . import ROOT
from .entities import entity_key

MIGRATIONS = [
    # 1: initial schema
    """
    CREATE TABLE items (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        kind TEXT NOT NULL,
        external_id TEXT NOT NULL,
        title TEXT,
        url TEXT,
        category TEXT,
        first_seen INTEGER NOT NULL,
        meta_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE (source, external_id)
    );
    CREATE TABLE snapshots (
        item_id INTEGER NOT NULL REFERENCES items(id),
        ts INTEGER NOT NULL,
        metric TEXT NOT NULL,
        value REAL NOT NULL
    );
    CREATE INDEX snapshots_item_ts ON snapshots (item_id, ts);
    -- one row per pipeline node (sources, scorers, outputs); "source" is the node id
    CREATE TABLE source_health (
        source TEXT PRIMARY KEY,
        last_run INTEGER,
        last_ok INTEGER,
        last_error INTEGER,
        last_error_msg TEXT,
        items_last_run INTEGER
    );
    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        ts INTEGER NOT NULL,
        type TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE INDEX events_ts ON events (ts);
    CREATE TABLE alerts_sent (
        item_id INTEGER NOT NULL REFERENCES items(id),
        ts INTEGER NOT NULL,
        level TEXT NOT NULL
    );
    CREATE INDEX alerts_sent_item_ts ON alerts_sent (item_id, ts);
    """,
    # 2: cross-source entity key (see entities.py)
    """
    ALTER TABLE items ADD COLUMN entity_key TEXT;
    CREATE INDEX items_entity_key ON items (entity_key);
    """,
    # 3: heat scores
    """
    CREATE TABLE scores (
        item_id INTEGER NOT NULL REFERENCES items(id),
        ts INTEGER NOT NULL,
        heat REAL NOT NULL,
        score REAL NOT NULL,
        reasons_json TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX scores_item_ts ON scores (item_id, ts);
    CREATE INDEX scores_ts ON scores (ts);
    CREATE INDEX snapshots_ts ON snapshots (ts);
    """,
    # 4: remember the score an alert was sent at (for the "jumped another 20" re-alert rule)
    """
    ALTER TABLE alerts_sent ADD COLUMN score REAL;
    """,
    # 5: second, name-based key for careful cross-source linking (see entities.alias_key),
    #    and a per-day API usage counter for quota-limited sources (YouTube)
    """
    ALTER TABLE items ADD COLUMN alias_key TEXT;
    CREATE INDEX items_alias_key ON items (alias_key);
    CREATE TABLE api_usage (source TEXT NOT NULL, day TEXT NOT NULL, units INTEGER NOT NULL DEFAULT 0,
                            PRIMARY KEY (source, day));
    -- remembered name lookups (e.g. "does Wikipedia have an article for name:radarly?") so they run once
    CREATE TABLE lookups (key TEXT PRIMARY KEY, result TEXT, ts INTEGER NOT NULL);
    """,
]

ITEM_FIELDS = ("kind", "external_id", "title", "url", "category")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = path or os.environ.get("SONAR_DB") or ROOT / "data" / "sonar.db"
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for n, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {n}")


def upsert_item(conn: sqlite3.Connection, source: str, item: dict, now: int | None = None) -> int:
    """Insert or update an item by (source, external_id); first_seen never changes. Returns item id."""
    if not item.get("kind") or not item.get("external_id"):
        raise ValueError(f"item needs kind and external_id: {item!r}")
    now = now or int(time.time())
    key = item.get("entity_key") or entity_key(item.get("url"))
    row = conn.execute(
        """INSERT INTO items (source, kind, external_id, title, url, category, first_seen, meta_json, entity_key, alias_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (source, external_id) DO UPDATE SET
             kind = excluded.kind, title = excluded.title, url = excluded.url,
             category = excluded.category, meta_json = excluded.meta_json,
             entity_key = excluded.entity_key, alias_key = excluded.alias_key
           RETURNING id""",
        (source, *(item.get(f) for f in ITEM_FIELDS), now, json.dumps(item.get("meta") or {}), key,
         item.get("alias_key")),
    ).fetchone()
    return row[0]


def add_snapshots(conn: sqlite3.Connection, item_id: int, metrics: dict, now: int | None = None) -> None:
    now = now or int(time.time())
    conn.executemany(
        "INSERT INTO snapshots (item_id, ts, metric, value) VALUES (?, ?, ?, ?)",
        [(item_id, now, m, float(v)) for m, v in metrics.items() if v is not None],
    )


def emit_event(conn: sqlite3.Connection, type_: str, now: int | None = None, **fields) -> dict:
    """Log a standard event: {"type": ..., "ts": ..., **fields}. Returns the event."""
    event = {"type": type_, "ts": now or int(time.time()), **fields}
    conn.execute(
        "INSERT INTO events (ts, type, payload_json) VALUES (?, ?, ?)",
        (event["ts"], type_, json.dumps(event)),
    )
    return event


def record_run(conn: sqlite3.Connection, node_id: str, ok: bool, items: int = 0,
               error: str | None = None, now: int | None = None) -> None:
    now = now or int(time.time())
    conn.execute("INSERT INTO source_health (source) VALUES (?) ON CONFLICT DO NOTHING", (node_id,))
    if ok:
        conn.execute("UPDATE source_health SET last_run=?, last_ok=?, items_last_run=? WHERE source=?",
                     (now, now, items, node_id))
    else:
        conn.execute("UPDATE source_health SET last_run=?, last_error=?, last_error_msg=? WHERE source=?",
                     (now, now, (error or "")[:1000], node_id))


def last_runs(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["source"]: r["last_run"] for r in conn.execute("SELECT source, last_run FROM source_health")}
