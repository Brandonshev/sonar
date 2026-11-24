"""Heat scorer on synthetic data."""
import json

import pytest

from sonar import db, run
from sonar.scoring.heat import DAY, score_all, value_at

NOW = 100 * DAY
NODE = {"id": "heat", "type": "scorer.heat", "enabled": True,
        "params": {"metrics": {"github": {"stars": 5}, "hackernews": {"points": 10}}}}


@pytest.fixture
def conn():
    return db.connect(":memory:")


def add(conn, source, ext, series, first_seen=None, url=None, metric=None):
    """series: {hours_before_now: value}. Snapshots are written at those times."""
    metric = metric or {"github": "stars", "hackernews": "points"}[source]
    oldest = NOW - max(series) * 3600
    item_id = db.upsert_item(conn, source, {"kind": "x", "external_id": ext, "title": ext, "url": url},
                             now=first_seen or oldest)
    for h, v in series.items():
        db.add_snapshots(conn, item_id, {metric: v}, now=NOW - h * 3600)
    return item_id


def daily(start, per_day, days=9, jump=None):
    """Snapshots every 24h for `days` days growing per_day, plus an optional jump in the last 24h."""
    s = {d * 24: start + per_day * (days - d) for d in range(days, 0, -1)}
    s[0] = start + per_day * days + (jump if jump is not None else per_day)
    return s


def scores(conn):
    return {r["title"]: dict(r) for r in conn.execute(
        "SELECT i.title, s.heat, s.score, s.reasons_json FROM scores s JOIN items i ON i.id = s.item_id")}


def test_value_at_interpolates():
    pts = [(0, 0), (10, 100)]
    assert value_at(pts, 5) == 50
    assert value_at(pts, -1) is None
    assert value_at(pts, 99) == 100


def test_fast_riser_beats_big_slow_item(conn):
    add(conn, "github", "big/slow", daily(100_000, 500, jump=600))    # 1.2x normal
    add(conn, "github", "small/fast", daily(40, 2, jump=570))         # 56 -> 628, normally +2/day
    s = scores(conn) if score_all(conn, NODE, now=NOW) else {}
    assert s["small/fast"]["score"] > s["big/slow"]["score"]
    assert s["small/fast"]["heat"] > 50 > s["big/slow"]["heat"]
    reason = json.loads(s["small/fast"]["reasons_json"])[0]
    assert reason.startswith("stars 56 -> 628 in 24h") and "x its normal rate" in reason


def test_flat_baseline_no_division_by_zero(conn):
    add(conn, "github", "flat/zero", daily(0, 0, jump=0))   # never grew, b = 0, v = 0
    add(conn, "github", "flat/then-up", daily(5, 0, jump=50))  # b = 0, falls back to floor 5
    score_all(conn, NODE, now=NOW)
    s = scores(conn)
    assert s["flat/zero"]["heat"] == 0 and s["flat/zero"]["score"] == 0
    assert s["flat/then-up"]["heat"] == pytest.approx(50 / 5 * (1 + 1 / 1.778), rel=0.01)


def test_new_item_compared_with_typical_new_items(conn):
    # three ordinary new HN stories (+30/day) and one exploding one, all a few hours old
    for i in range(3):
        add(conn, "hackernews", f"normal{i}", {6: 0, 0: 8})
    add(conn, "hackernews", "rocket", {6: 0, 3: 150, 0: 300})
    add(conn, "hackernews", "just-seen", {0: 50})   # one snapshot only: not enough data
    score_all(conn, NODE, now=NOW)
    s = scores(conn)
    assert "just-seen" not in s
    assert s["rocket"]["heat"] > 5 * s["normal0"]["heat"]
    reasons = json.loads(s["rocket"]["reasons_json"])
    assert "scaled to 24h" in reasons[0] and reasons[1].startswith("new item")


def test_cross_source_bonus(conn):
    url = "https://github.com/acme/widget"
    alone = dict(daily(40, 2, jump=200))
    add(conn, "github", "acme/widget", alone, url=url)
    add(conn, "github", "solo/widget", alone, url="https://github.com/solo/widget")
    add(conn, "hackernews", "hn-widget", daily(0, 5, jump=300), url=url)
    score_all(conn, NODE, now=NOW)
    s = scores(conn)
    assert s["acme/widget"]["heat"] == pytest.approx(s["solo/widget"]["heat"] * 1.25)
    assert any("also climbing on hackernews" in r for r in json.loads(s["acme/widget"]["reasons_json"]))


def test_trend_found_emitted_once_on_crossing(conn):
    add(conn, "github", "small/fast", daily(40, 2, jump=570))
    add(conn, "github", "big/slow", daily(100_000, 500, jump=600))
    score_all(conn, NODE, now=NOW)
    score_all(conn, NODE, now=NOW + 60)  # still hot: no second event
    events = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM events")]
    assert [e["item"]["title"] for e in events] == ["small/fast"]
    e = events[0]
    assert e["type"] == "trend.found" and e["source"] == "github" and e["heat"] >= 5 and e["reasons"]


def test_score_command_prints_table(conn, monkeypatch, capsys):
    add(conn, "github", "small/fast", daily(40, 2, jump=570))
    monkeypatch.setattr(run.db, "connect", lambda: conn)
    monkeypatch.setattr(run.time, "time", lambda: NOW)
    monkeypatch.setattr(run.pipeline, "load", lambda path: {"version": 1, "nodes": [NODE], "edges": []})
    assert run.main(["--score"]) == 0
    out = capsys.readouterr().out
    assert "== github ==" in out and "small/fast" in out
