"""Alerts (cooldown, re-jump, cap, warm-up), digest and the daily schedule. No real ntfy/email."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sonar import db, pipeline, run
from sonar.outputs import digest, ntfy_alert

H = 3600
NOW = 1_800_000_000
P = {**ntfy_alert.DEFAULTS}


def cand(i, score, heat=10, key=None):
    return {"item_id": i, "score": score, "heat": heat, "entity_key": key or f"k{i}"}


# --- pick(): pure decision logic ---

def test_threshold():
    picked = ntfy_alert.pick([cand(1, 80, heat=4.9), cand(2, 30, heat=5)], {}, 0, NOW, P)
    assert [c["item_id"] for c in picked] == [2]
    assert ntfy_alert.pick([cand(1, 30)], {}, 0, NOW, {**P, "min_score": 50}) == []


def test_cooldown_blocks_repeat_within_24h():
    last = {1: (NOW - 23 * H, 60)}
    assert ntfy_alert.pick([cand(1, 75)], last, 0, NOW, P) == []        # +15: not enough
    assert ntfy_alert.pick([cand(1, 80)], last, 0, NOW, P) != []        # +20: re-alert
    assert ntfy_alert.pick([cand(1, 60)], {1: (NOW - 25 * H, 60)}, 0, NOW, P) != []  # cooldown over


def test_hourly_cap_and_best_first():
    cands = [cand(i, score=i) for i in range(1, 11)]
    picked = ntfy_alert.pick(cands, {}, 0, NOW, P)
    assert [c["item_id"] for c in picked] == [10, 9, 8, 7, 6]
    assert len(ntfy_alert.pick(cands, {}, 3, NOW, P)) == 2
    assert ntfy_alert.pick(cands, {}, 5, NOW, P) == []


def test_same_entity_on_two_sources_alerts_once():
    picked = ntfy_alert.pick([cand(1, 90, key="github:a/b"), cand(2, 80, key="github:a/b")], {}, 0, NOW, P)
    assert [c["item_id"] for c in picked] == [1]


# --- run(): end to end against a DB ---

@pytest.fixture
def conn():
    c = db.connect(":memory:")
    for i, (title, heat, score) in enumerate([("Rocket", 12, 85), ("Meh", 1.2, 10)], start=1):
        item = db.upsert_item(c, "github", {"kind": "repo", "external_id": title, "title": title,
                                            "url": f"https://github.com/x/{title}", "category": "Rust"},
                              now=NOW - 2 * 86400)
        db.add_snapshots(c, item, {"stars": 1}, now=NOW - 2 * 86400)
        c.execute("INSERT INTO scores VALUES (?, ?, ?, ?, ?)",
                  (item, NOW, heat, score, json.dumps([f"stars 10 -> 500 in 24h, {heat}x its normal rate"])))
    c.execute("INSERT INTO scores VALUES (1, ?, 0, 0, '[]')", (NOW - 2 * 86400,))  # Sonar has been scoring for 2 days
    c.commit()
    return c


def test_warmup_counts_from_first_score_not_backfilled_snapshots():
    c = db.connect(":memory:")
    item = db.upsert_item(c, "wikipedia", {"kind": "article", "external_id": "X"}, now=NOW - 10 * 86400)
    db.add_snapshots(c, item, {"views": 5}, now=NOW - 10 * 86400)  # backfilled 10 days
    c.execute("INSERT INTO scores VALUES (?, ?, 50, 99, '[\"r\"]')", (item, NOW - 3600))  # but scoring began 1h ago
    c.commit()
    sent = []
    assert ntfy_alert.run(c, {"id": "alerts", "params": {}}, now=NOW, send=lambda *a, **k: sent.append(a)) == 0
    assert not sent


def test_run_sends_records_and_respects_cooldown(conn):
    sent = []
    node = {"id": "alerts", "params": {}}
    send = lambda *a, **kw: sent.append((a, kw))
    assert ntfy_alert.run(conn, node, now=NOW, send=send) == 1
    (title, body), kw = sent[0]
    assert title == "🔥 Rocket" and "github · Rust · score 85" in body and "stars 10 -> 500" in body
    assert kw["click"] == "https://github.com/x/Rocket"
    assert ntfy_alert.run(conn, node, now=NOW + 60, send=send) == 0  # cooldown
    assert conn.execute("SELECT type FROM events").fetchone()[0] == "alert.sent"


def test_run_quiet_during_warmup(conn):
    sent = []
    node = {"id": "alerts", "params": {"warmup_hours": 72}}
    assert ntfy_alert.run(conn, node, now=NOW, send=lambda *a, **k: sent.append(a)) == 0 and not sent


def test_failed_send_is_not_recorded(conn):
    def boom(*a, **k):
        raise OSError("ntfy down")
    with pytest.raises(OSError):
        ntfy_alert.run(conn, {"id": "alerts"}, now=NOW, send=boom)
    assert conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0] == 0


def test_digest(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "ROOT", tmp_path)
    monkeypatch.delenv("SMTP_USER", raising=False)
    db.record_run(conn, "producthunt", ok=False, error="RuntimeError: no token", now=NOW - H)
    sent = []
    node = {"id": "digest", "tz": "Australia/Sydney", "params": {}}
    digest.run(conn, node, now=NOW, send=lambda *a, **kw: sent.append((a, kw)),
               email=lambda *a: pytest.fail("email not configured"))
    day = datetime.fromtimestamp(NOW, ZoneInfo("Australia/Sydney")).strftime("%Y-%m-%d")
    html = (tmp_path / "data" / "digests" / f"{day}.html").read_text()
    assert "Rocket" in html and "producthunt" in html and "no token" in html and "Sonar v" in html
    (title, body), _ = sent[0]
    assert title == f"Sonar digest {day}" and "Rocket" in body and "Broken: producthunt" in body


def test_digest_emails_when_configured(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "ROOT", tmp_path)
    for k in ("SMTP_USER", "SMTP_PASSWORD", "DIGEST_TO"):
        monkeypatch.setenv(k, "x")
    mails = []
    digest.run(conn, {"id": "digest", "params": {}}, now=NOW, send=lambda *a, **k: None,
               email=lambda *a: mails.append(a))
    assert mails and "<html" in mails[0][1]


# --- daily "at" schedule ---

def test_at_due_sydney_7am():
    node = {"at": "07:00", "tz": "Australia/Sydney"}
    syd = ZoneInfo("Australia/Sydney")
    t = lambda h, m=0, d=25: int(datetime(2026, 11, d, h, m, tzinfo=syd).timestamp())
    assert pipeline.at_due(node, t(7, 5, d=24), t(6, 59)) is False   # ran yesterday 7:05, not 7am yet
    assert pipeline.at_due(node, t(7, 5, d=24), t(7, 1)) is True     # 7:01: due
    assert pipeline.at_due(node, t(7, 2), t(9, 0)) is False          # already ran today
    assert pipeline.at_due(node, None, t(3, 0)) is True              # never ran


def test_runner_runs_at_nodes_once_a_day():
    import types
    conn = db.connect(":memory:")
    data = {"version": 1, "edges": [], "nodes": [
        {"id": "digest", "type": "output.digest", "enabled": True, "at": "07:00", "tz": "Australia/Sydney"}]}
    mods = {"digest": types.SimpleNamespace(run=lambda c, n: 0)}
    syd = ZoneInfo("Australia/Sydney")
    t = lambda h, d=25: int(datetime(2026, 11, d, h, 5, tzinfo=syd).timestamp())
    assert run.run_all_due(conn, data, now=t(7), modules=mods) == {"digest"}
    assert run.run_all_due(conn, data, now=t(8), modules=mods) == set()
    assert run.run_all_due(conn, data, now=t(7, d=26), modules=mods) == {"digest"}


def test_schema_rejects_bad_schedule():
    base = {"version": 1, "edges": [], "nodes": [{"id": "d", "type": "output.digest", "enabled": True}]}
    for extra in ({"at": "7am"}, {"at": "07:00", "every": "1d"}, {"at": "07:00", "tz": "Mars/Base"}):
        bad = json.loads(json.dumps(base))
        bad["nodes"][0].update(extra)
        with pytest.raises(pipeline.PipelineError):
            pipeline.validate(bad)


def test_test_alert_flag(monkeypatch, capsys):
    from sonar.outputs import notify
    sent = []
    monkeypatch.setattr(notify, "send_ntfy", lambda *a, **k: sent.append(a))
    assert run.main(["--test-alert"]) == 0
    assert sent and sent[0][0] == "Sonar test alert"
