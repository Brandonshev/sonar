"""Alerts (once-only unless mega, cap, warm-up), digest and the daily schedule. No real ntfy/email."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sonar import db, pipeline, run
from sonar.outputs import digest, ntfy_alert

H = 3600
NOW = 1_800_000_000
P = {**ntfy_alert.DEFAULTS}


def cand(i, score, heat=10, key=None, source="github"):
    return {"item_id": i, "score": score, "heat": heat, "entity_key": key or f"k{i}", "source": source}


# --- pick(): pure decision logic ---

def test_threshold():
    picked = ntfy_alert.pick([cand(1, 80, heat=4.9), cand(2, 30, heat=5)], {}, 0, NOW, P)
    assert [c["item_id"] for c in picked] == [2]
    assert ntfy_alert.pick([cand(1, 30)], {}, 0, NOW, {**P, "min_score": 50}) == []


def test_alerted_once_unless_it_goes_mega():
    last = {1: (NOW - 30 * 86400, 60)}
    assert ntfy_alert.pick([cand(1, 90)], last, 0, NOW, P) == []        # a month later, +30: still no
    assert ntfy_alert.pick([cand(1, 95)], last, 0, NOW, P) != []        # mega: one more alert
    assert ntfy_alert.pick([cand(1, 99)], {1: (NOW - H, 96)}, 0, NOW, P) == []  # already alerted as mega


def test_hourly_cap_and_best_first():
    cands = [cand(i, score=i, heat=10 + i) for i in range(1, 11)]
    picked = ntfy_alert.pick(cands, {}, 0, NOW, P)
    assert [c["item_id"] for c in picked] == [10, 9, 8, 7, 6]
    assert len(ntfy_alert.pick(cands, {}, 3, NOW, P)) == 2
    assert ntfy_alert.pick(cands, {}, 5, NOW, P) == []


def test_source_daily_cap_lets_the_best_through():
    p = {**P, "source_caps_per_day": {"hackernews": 3}}
    hn = [cand(i, score=60, heat=h, source="hackernews") for i, h in enumerate([6, 40, 9, 25, 7], start=1)]
    other = [cand(9, score=50, heat=5.5, source="youtube")]
    picked = ntfy_alert.pick(hn + other, {}, 0, NOW, p, sent_today={"hackernews": 1})
    assert [c["item_id"] for c in picked] == [2, 4, 9]  # 2 HN left today (best heat first), youtube unaffected
    assert ntfy_alert.pick(hn, {}, 0, NOW, p, sent_today={"hackernews": 3}) == []


def test_ranking_weight_does_not_hide_a_big_spike():
    # HN's score is ranked down, but its heat is huge: it still goes first
    picked = ntfy_alert.pick([cand(1, score=55, heat=40, source="hackernews"), cand(2, score=90, heat=6)], {}, 4, NOW, P)
    assert [c["item_id"] for c in picked] == [1]


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
    assert ntfy_alert.run(conn, node, now=NOW + 60, send=send) == 0  # already alerted
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
    monkeypatch.setenv("SONAR_PUBLIC_URL", "https://sonar.example.com")
    mails = []
    digest.run(conn, {"id": "digest", "tz": "Australia/Sydney", "params": {}}, now=NOW, send=lambda *a, **k: None,
               email=lambda *a: mails.append(a))
    subject, html, text = mails[0]
    assert subject.startswith("What's climbing, ") and "<html" in html and "Rocket" in html  # full digest
    assert text.startswith("Morning,") and "1. Rocket" in text and "Rocket\n   on GitHub, about 12 times its usual pace" in text
    assert text.count("http") == 1 and "https://sonar.example.com/digest/" in text  # one link, to our own site
    assert "github.com/x/Rocket" not in text and "score" not in text.lower()


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


class FakeSMTP:
    """Records what send_email does; `tls` = whether the server offers STARTTLS."""
    log: list = []
    tls = True

    def __init__(self, host, port, timeout):
        self.log.append(("connect", host, port))

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def ehlo(self): pass
    def has_extn(self, name): return self.tls
    def starttls(self): self.log.append(("starttls",))
    def login(self, u, p): self.log.append(("login", u))
    def send_message(self, m):
        assert m["Date"] and m["Message-ID"].endswith("@x>") and m["From"] == "Sonar <sonar@x>"
        self.log.append(("send", m["To"]))


@pytest.mark.parametrize("host,tls,expect", [
    ("127.0.0.1", False, ["connect", "login", "send"]),            # Postline on the same server
    ("smtp.example.com", True, ["connect", "starttls", "login", "send"]),
    ("smtp.example.com", False, ["connect"]),                       # refuses: password would go in the clear
])
def test_send_email_tls_rules(monkeypatch, host, tls, expect):
    from sonar.outputs import notify
    FakeSMTP.log, FakeSMTP.tls = [], tls
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    for k, v in {"SMTP_HOST": host, "SMTP_PORT": "2587", "SMTP_USER": "sonar@x", "SMTP_PASSWORD": "p", "DIGEST_TO": "me@x"}.items():
        monkeypatch.setenv(k, v)
    if expect == ["connect"]:
        with pytest.raises(RuntimeError, match="refusing"):
            notify.send_email("s", "<p>h</p>", "t")
    else:
        notify.send_email("s", None, "t")
    assert [e[0] for e in FakeSMTP.log] == expect


# --- quiet hours ---

SYD = ZoneInfo("Australia/Sydney")
QUIET = {"start": "22:00", "end": "09:00", "tz": "Australia/Sydney"}


def syd(h, m=0, d=25):
    return int(datetime(2026, 11, d, h, m, tzinfo=SYD).timestamp())


@pytest.mark.parametrize("when,until", [
    (syd(21, 59), None), (syd(22, 0), syd(9, 0, d=26)), (syd(2, 30), syd(9, 0)), (syd(8, 59), syd(9, 0)), (syd(9, 0), None),
])
def test_quiet_until(when, until):
    from sonar.outputs.notify import quiet_until
    assert quiet_until(when, QUIET) == until


def test_ntfy_holds_non_urgent_messages_overnight(monkeypatch):
    from sonar.outputs import notify
    bodies = []

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b""
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout: bodies.append(json.loads(req.data)) or R())
    monkeypatch.setattr(notify.time, "time", lambda: syd(23))
    monkeypatch.setenv("NTFY_TOPIC", "t")
    notify.configure_quiet(QUIET)
    try:
        notify.send_ntfy("digest", "x")
        notify.send_ntfy("huge", "x", urgent=True)
    finally:
        notify.configure_quiet(None)
    assert bodies[0]["delay"] == str(syd(9, 0, d=26)) and "delay" not in bodies[1]


def quiet_conn(heat, second_source):
    c = db.connect(":memory:")
    item = db.upsert_item(c, "github", {"kind": "repo", "external_id": "a/b", "title": "a/b", "url": "https://github.com/a/b"},
                          now=syd(1) - 3 * 86400)
    c.execute("INSERT INTO scores VALUES (?, ?, ?, 99, '[\"r\"]')", (item, syd(1) - 2 * 86400, 0))  # scoring for 2 days
    c.execute("INSERT INTO scores VALUES (?, ?, ?, 99, '[\"r\"]')", (item, syd(1), heat))
    if second_source:
        hn = db.upsert_item(c, "hackernews", {"kind": "story", "external_id": "1", "url": "https://github.com/a/b"})
        c.execute("INSERT INTO scores VALUES (?, ?, 12, 70, '[]')", (hn, syd(1)))
    c.commit()
    return c


@pytest.mark.parametrize("heat,second,expect", [(40, True, 1), (40, False, 0), (12, True, 0)])
def test_overnight_only_life_changing_alerts(heat, second, expect):
    sent = []
    node = {"id": "alerts", "params": {"quiet_hours": QUIET}}
    n = ntfy_alert.run(quiet_conn(heat, second), node, now=syd(1), send=lambda *a, **k: sent.append((a, k)))
    assert n == expect
    if expect:
        (title, body), kw = sent[0]
        assert title.startswith("🚨") and kw["priority"] == 5 and kw["urgent"] is True


def test_held_alert_goes_out_after_quiet_hours():
    c = quiet_conn(12, True)
    c.execute("UPDATE scores SET ts = ? WHERE ts = ?", (syd(9, 5), syd(1)))
    c.commit()
    sent = []
    ntfy_alert.run(c, {"id": "alerts", "params": {"quiet_hours": QUIET}}, now=syd(9, 5), send=lambda *a, **k: sent.append(k))
    assert len(sent) >= 1 and sent[0]["priority"] == 4  # normal alert, normal priority


def test_reuploads_with_the_same_title_alert_once():
    a, b = cand(1, 96), cand(2, 95)
    a["title"] = "JASMIN AND JAMES VIRAL TIKTOK DANCE TREND"
    b["title"] = "Jasmin and James  viral tiktok dance trend"
    assert [c["item_id"] for c in ntfy_alert.pick([a, b], {}, 0, NOW, P)] == [1]
    # ...and a re-upload in a later batch is blocked by what was already alerted
    assert ntfy_alert.pick([b], {}, 0, NOW, P, seen_titles={ntfy_alert.same_title(a["title"]): 96}) == []


def test_hashtag_link_is_not_a_second_source():
    c = quiet_conn(40, False)
    c.execute("UPDATE items SET alias_key = 'tag:couple'")
    tag = db.upsert_item(c, "tiktok", {"kind": "hashtag", "external_id": "couple", "title": "#couple"})
    c.execute("UPDATE items SET alias_key = 'tag:couple' WHERE id = ?", (tag,))
    c.execute("INSERT INTO scores VALUES (?, ?, 4, 50, '[]')", (tag, syd(1)))
    c.commit()
    assert ntfy_alert.run(c, {"id": "alerts", "params": {"quiet_hours": QUIET}}, now=syd(1), send=lambda *a, **k: None) == 0


@pytest.mark.parametrize("url,expect", [
    ("https://github.com/openai/math/blob/main/preprints/x/paper.pdf",
     "https://github.com/openai/math/raw/main/preprints/x/paper.pdf"),
    ("https://github.com/a/b/blob/main/README.md", "https://github.com/a/b/blob/main/README.md"),
    ("https://example.com/paper.pdf", "https://example.com/paper.pdf"),
    (None, None)])
def test_open_link_sends_github_pdfs_to_the_raw_file(url, expect):
    from sonar.outputs.notify import open_link
    assert open_link(url) == expect


def test_videos_never_alert():
    c = quiet_conn(40, True)
    c.execute("UPDATE items SET category = 'social.video'")
    c.commit()
    node = {"id": "alerts", "params": {"skip_categories": ["social.video"]}}
    assert ntfy_alert.run(c, node, now=syd(12), send=lambda *a, **k: None) == 0
    c.execute("UPDATE items SET category = 'repo'")
    c.execute("UPDATE scores SET ts = ? WHERE heat = 40", (syd(12),))
    c.commit()
    assert ntfy_alert.run(c, node, now=syd(12), send=lambda *a, **k: None) == 1
