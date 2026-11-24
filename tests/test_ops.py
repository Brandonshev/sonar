from sonar import db, health, housekeeping

DAY = 86400
NOW = 200 * DAY


def test_prune_keeps_one_per_day_after_90_days():
    conn = db.connect(":memory:")
    item = db.upsert_item(conn, "github", {"kind": "repo", "external_id": "a/b"})
    old_day = NOW - 100 * DAY
    for h in range(24):  # hourly snapshots on an old day, and on a recent one
        db.add_snapshots(conn, item, {"stars": h, "forks": 1}, now=old_day - old_day % DAY + h * 3600)
        db.add_snapshots(conn, item, {"stars": h}, now=NOW - DAY + h * 3600)
    assert housekeeping.prune(conn, NOW) == 46  # 2 metrics x 24 old rows, minus 1 kept each
    old = conn.execute("SELECT metric, value FROM snapshots WHERE ts < ? ORDER BY metric", (NOW - 90 * DAY,)).fetchall()
    assert [tuple(r) for r in old] == [("forks", 1.0), ("stars", 23.0)]  # last of the day
    assert conn.execute("SELECT COUNT(*) FROM snapshots WHERE ts >= ?", (NOW - 2 * DAY,)).fetchone()[0] == 24


def test_vacuum_weekly():
    conn = db.connect(":memory:")
    assert housekeeping.vacuum_if_due(conn, NOW)
    assert not housekeeping.vacuum_if_due(conn, NOW + 3 * DAY)
    assert housekeeping.vacuum_if_due(conn, NOW + 8 * DAY)


def test_health_report_statuses():
    conn = db.connect(":memory:")
    data = {"nodes": [
        {"id": "github", "type": "source.github", "enabled": True, "every": "3h"},
        {"id": "reddit", "type": "source.reddit", "enabled": False, "every": "1h"},
        {"id": "producthunt", "type": "source.producthunt", "enabled": True, "every": "2h"},
        {"id": "hackernews", "type": "source.hackernews", "enabled": True, "every": "1h"},
        {"id": "heat", "type": "scorer.heat", "enabled": True},
    ]}
    db.record_run(conn, "github", ok=True, items=200, now=NOW - 600)
    db.record_run(conn, "producthunt", ok=False, error="RuntimeError: no token", now=NOW - 60)
    db.record_run(conn, "hackernews", ok=True, items=5, now=NOW - 10 * 3600)
    out = health.report(conn, data, now=NOW)
    line = {l.split()[0]: l for l in out.splitlines()[2:] if l.strip()}
    assert "OK" in line["github"] and "200" in line["github"]
    assert "OFF" in line["reddit"]
    assert "FAILING" in line["producthunt"] and "no token" in line["producthunt"]
    assert "STALE" in line["hackernews"]
    assert "WAITING" in line["heat"]


def test_prune_keeps_two_weeks_of_scores():
    conn = db.connect(":memory:")
    item = db.upsert_item(conn, "github", {"kind": "repo", "external_id": "a/b"})
    for days in (20, 13, 1):
        conn.execute("INSERT INTO scores VALUES (?, ?, 1, 1, '[]')", (item, NOW - days * DAY))
    housekeeping.prune(conn, NOW)
    assert [r[0] for r in conn.execute("SELECT (? - ts) / 86400 FROM scores ORDER BY ts", (NOW,))] == [13, 1]


def test_health_hides_errors_that_were_fixed():
    conn = db.connect(":memory:")
    data = {"nodes": [{"id": "youtube", "type": "source.youtube", "enabled": True, "every": "3h"}]}
    db.record_run(conn, "youtube", ok=False, error="RuntimeError: YOUTUBE_API_KEY is not set", now=NOW - 3600)
    assert "not set" in health.rows(conn, data, NOW)[0]["error"]
    db.record_run(conn, "youtube", ok=True, items=243, now=NOW - 600)
    row = health.rows(conn, data, NOW)[0]
    assert row["status"] == "OK" and row["error"] == ""
