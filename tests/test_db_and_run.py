import json
import types

import pytest

from sonar import db, run
from sonar.collectors.base import Collector


@pytest.fixture
def conn():
    return db.connect(":memory:")


def test_migrations_are_idempotent(conn):
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_upsert_keeps_first_seen_and_id(conn):
    item = {"kind": "repo", "external_id": "a/b", "title": "old", "meta": {"lang": "py"}}
    first = db.upsert_item(conn, "github", item, now=100)
    second = db.upsert_item(conn, "github", {**item, "title": "new"}, now=200)
    assert first == second
    row = conn.execute("SELECT * FROM items").fetchone()
    assert (row["title"], row["first_seen"]) == ("new", 100)
    assert json.loads(row["meta_json"]) == {"lang": "py"}
    # same external_id on another source is a different item
    assert db.upsert_item(conn, "reddit", item) != first


def test_upsert_requires_ids(conn):
    with pytest.raises(ValueError):
        db.upsert_item(conn, "github", {"kind": "repo"})


def test_collector_stores_snapshots(conn):
    class Fake(Collector):
        def fetch(self):
            return [({"kind": "repo", "external_id": "x"}, {"stars": 10, "forks": None})]

    node = {"id": "fake", "type": "source.fake", "enabled": True}
    Fake(node).run(conn)
    Fake(node).run(conn)
    rows = conn.execute("SELECT metric, value FROM snapshots").fetchall()
    assert [tuple(r) for r in rows] == [("stars", 10.0), ("stars", 10.0)]  # None skipped
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_broken_node_is_recorded_and_others_still_run(conn):
    def boom(conn, node):
        raise RuntimeError("api down")

    data = {
        "version": 1,
        "nodes": [
            {"id": "bad", "type": "source.bad", "enabled": True, "every": "1h"},
            {"id": "good", "type": "source.good", "enabled": True, "every": "1h"},
            {"id": "score", "type": "scorer.heat", "enabled": True},
            {"id": "off", "type": "source.off", "enabled": False, "every": "1h"},
        ],
        "edges": [["bad", "score"], ["good", "score"]],
    }
    mods = {
        "bad": types.SimpleNamespace(run=boom),
        "good": types.SimpleNamespace(run=lambda c, n: 3),
        "score": types.SimpleNamespace(run=lambda c, n: 0),
    }
    ran = run.run_all_due(conn, data, now=1000, modules=mods)
    assert ran == {"bad", "good", "score"}
    health = {r["source"]: r for r in conn.execute("SELECT * FROM source_health")}
    assert health["bad"]["last_ok"] is None and "api down" in health["bad"]["last_error_msg"]
    assert health["good"]["items_last_run"] == 3
    assert conn.execute("SELECT type FROM events").fetchone()[0] == "node.failed"
    # nothing is due again 10 minutes later, so the edge-triggered scorer stays idle too
    assert run.run_all_due(conn, data, now=1600, modules=mods) == set()
    assert run.run_all_due(conn, data, now=1000 + 3600, modules=mods) == {"bad", "good", "score"}


def test_emit_event_shape(conn):
    e = db.emit_event(conn, "trend.found", now=5, source="github", item={"id": 1}, heat=7.5)
    stored = json.loads(conn.execute("SELECT payload_json FROM events").fetchone()[0])
    assert stored == e == {"type": "trend.found", "ts": 5, "source": "github", "item": {"id": 1}, "heat": 7.5}
