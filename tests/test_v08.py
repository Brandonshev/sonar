"""TikTok, Instagram (with a fake instaloader), watchlists, pacing and auto-disable. No live calls."""
import json
import sys
import types
from pathlib import Path

import pytest

from sonar import db, run, watchlist
from sonar.collectors import instagram, tiktok, youtube
from sonar.collectors.base import Collector

FIX = Path(__file__).parent / "fixtures"
NOW = 1_800_000_000


@pytest.fixture
def conn():
    return db.connect(":memory:")


def route(monkeypatch, fn):
    calls = []

    def request(self, url, headers=None, data=None, raw=False):
        calls.append((url, headers))
        return fn(url), {}
    monkeypatch.setattr(Collector, "request", request)
    return calls


BOOKTOK = json.loads((FIX / "tiktok_booktok.json").read_text())
MISSING = {"statusCode": 10205, "status_code": 0}


def test_tiktok_seeds_and_unknown_tags(conn, monkeypatch):
    calls = route(monkeypatch, lambda u: BOOKTOK if "booktok" in u else MISSING)
    n = tiktok.run(conn, {"id": "tiktok", "params": {"hashtags": ["#BookTok", "zzqxnotreal"]}})
    assert n == 1
    row = conn.execute("SELECT * FROM items").fetchone()
    assert (row["title"], row["category"], row["alias_key"]) == ("#booktok", "social.video", "tag:booktok")
    snaps = dict(conn.execute("SELECT metric, value FROM snapshots").fetchall())
    assert snaps["videos"] == int(BOOKTOK["challengeInfo"]["statsV2"]["videoCount"])
    assert "Mozilla" in calls[0][1]["User-Agent"]


def test_tiktok_all_failing_raises(conn, monkeypatch):
    def boom(u):
        raise OSError("blocked")
    route(monkeypatch, boom)
    with pytest.raises(RuntimeError, match="likely blocked"):
        tiktok.run(conn, {"id": "tiktok", "params": {"hashtags": ["a1b", "c2d", "e3f", "g4h"]}})


def test_tiktok_discovers_from_youtube_titles(conn, monkeypatch):
    import time
    for i, title in enumerate(["Morning routine #GRWM #shorts", "Outfit #grwm #fyp", "Reading #booktok", "#booktok haul"]):
        db.upsert_item(conn, "youtube", {"kind": "video", "external_id": str(i), "title": title}, now=int(time.time()))
    route(monkeypatch, lambda u: BOOKTOK)
    tiktok.run(conn, {"id": "tiktok", "params": {}})
    assert set(watchlist.values(conn, "tiktok")) == {"grwm", "booktok"}  # generic #shorts/#fyp skipped, 1-mention tags skipped


def test_tiktok_pushes_climbers_to_instagram_and_youtube(conn, monkeypatch):
    item = db.upsert_item(conn, "tiktok", {"kind": "hashtag", "external_id": "booktok"})
    conn.execute("INSERT INTO scores VALUES (?, ?, 6.0, 70, '[]')", (item, NOW))
    route(monkeypatch, lambda u: BOOKTOK)
    tiktok.run(conn, {"id": "tiktok", "params": {"hashtags": ["booktok"]}})
    assert watchlist.values(conn, "instagram") == ["booktok"]
    assert watchlist.values(conn, "youtube") == ["#booktok"]


def test_watchlist_cap_replaces_oldest(conn):
    for i in range(5):
        watchlist.add(conn, "instagram", f"t{i}", "tiktok", NOW + i, cap=3)
    assert watchlist.values(conn, "instagram") == ["t4", "t3", "t2"]
    assert watchlist.values(conn, "instagram", seeds=["seed", "t4"]) == ["seed", "t4", "t3", "t2"]
    watchlist.expire(conn, "instagram", NOW + 10 + 86400, ttl_days=1)
    assert watchlist.values(conn, "instagram") == []


def test_youtube_searches_pushed_hashtags_and_links_them(conn, monkeypatch):
    watchlist.add(conn, "youtube", "#booktok", "tiktok", NOW, cap=4)
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    vid = {"id": "V1", "snippet": {"title": "t", "channelTitle": "c"}, "statistics": {"viewCount": "5"}}
    calls = route(monkeypatch, lambda u: {"items": [{"id": {"videoId": "V1"}}]} if "/search?" in u else {"items": [vid]})
    youtube.run(conn, {"id": "youtube", "params": {"regions": [], "topics": []}})
    assert any("q=%23booktok" in u for u, _ in calls)
    assert conn.execute("SELECT alias_key FROM items WHERE external_id='V1'").fetchone()[0] == "tag:booktok"
    # a later re-check (no hashtag context) keeps the link
    youtube.to_result(vid, set())
    db.upsert_item(conn, "youtube", youtube.to_result(vid, set())[0])
    assert conn.execute("SELECT alias_key FROM items WHERE external_id='V1'").fetchone()[0] == "tag:booktok"


# ---------- Instagram with a fake instaloader ----------

def fake_instaloader(counts, fail_login=False):
    exc = types.SimpleNamespace(QueryReturnedNotFoundException=type("NF", (Exception,), {}),
                                LoginRequiredException=type("LR", (Exception,), {}),
                                ConnectionException=type("CE", (Exception,), {}))

    class L:
        def __init__(self, **kw):
            self.context = object()

        def load_session_from_file(self, user, path):
            if fail_login:
                raise FileNotFoundError(path)

    class Hashtag:
        @staticmethod
        def from_name(ctx, name):
            if name not in counts:
                raise exc.QueryReturnedNotFoundException()
            if counts[name] == "blocked":
                raise exc.ConnectionException("429")
            return types.SimpleNamespace(mediacount=counts[name])
    return types.SimpleNamespace(Instaloader=L, Hashtag=Hashtag, exceptions=exc)


def test_instagram(conn, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "instaloader", fake_instaloader({"booktok": 12345678}))
    monkeypatch.setenv("IG_USERNAME", "throwaway")
    watchlist.add(conn, "instagram", "booktok", "tiktok", NOW, cap=5)
    n = instagram.run(conn, {"id": "instagram", "params": {"hashtags": ["ghosttag"], "session_file": str(tmp_path / "s")}})
    assert n == 1
    row = conn.execute("SELECT * FROM items").fetchone()
    assert (row["alias_key"], row["category"]) == ("tag:booktok", "social.instagram")


def test_instagram_blocked_and_missing_setup(conn, monkeypatch, tmp_path):
    monkeypatch.setenv("IG_USERNAME", "throwaway")
    monkeypatch.setitem(sys.modules, "instaloader", fake_instaloader({"a": "blocked"}))
    with pytest.raises(RuntimeError, match="Instagram refused"):
        instagram.run(conn, {"id": "instagram", "params": {"hashtags": ["a"], "session_file": str(tmp_path / "s")}})
    monkeypatch.setitem(sys.modules, "instaloader", fake_instaloader({}, fail_login=True))
    with pytest.raises(RuntimeError, match="no Instagram session"):
        instagram.run(conn, {"id": "instagram", "params": {"hashtags": ["a"]}})
    monkeypatch.delenv("IG_USERNAME")
    with pytest.raises(RuntimeError, match="IG_USERNAME"):
        instagram.run(conn, {"id": "instagram", "params": {}})


def test_instagram_without_library(conn, monkeypatch):
    monkeypatch.setitem(sys.modules, "instaloader", None)  # import fails
    with pytest.raises(RuntimeError, match="not installed"):
        instagram.run(conn, {"id": "instagram", "params": {}})


# ---------- pacing and auto-disable ----------

def test_pace_waits_min_interval_plus_jitter(monkeypatch):
    clock, slept = [100.0], []
    monkeypatch.setattr("sonar.collectors.base.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("sonar.collectors.base.time.sleep", slept.append)
    monkeypatch.setattr("sonar.collectors.base.random.uniform", lambda a, b: b)  # max jitter
    c = Collector({"id": "x", "params": {"min_interval_seconds": 5, "jitter_seconds": 2}})
    c.pace()
    clock[0] = 101.0
    c.pace()
    assert slept == [6.0]  # 100 + 5 + 2 - 101


def test_auto_disable_after_three_failures_with_one_alert(conn):
    sent = []
    node = {"id": "tiktok", "label": "TikTok", "params": {"max_failures": 3}}
    bad = types.SimpleNamespace(run=lambda c, n: (_ for _ in ()).throw(RuntimeError("blocked")))
    for i in range(4):
        run.run_node(conn, node, bad, now=NOW + i, notify=lambda *a, **k: sent.append(a))
    h = db.health_row(conn, "tiktok")
    assert h["disabled_at"] == NOW + 2 and h["fail_streak"] == 3   # 4th run was skipped
    assert len(sent) == 1 and "switched off" in sent[0][0]
    assert [r[0] for r in conn.execute("SELECT type FROM events")].count("source.disabled") == 1
    from sonar import health
    assert health.status({"enabled": True}, h, NOW) == "DISABLED"
    # --enable turns it back on and clears the streak
    db.set_disabled(conn, "tiktok", False)
    assert run.run_node(conn, node, types.SimpleNamespace(run=lambda c, n: 1), now=NOW + 9)
    assert db.health_row(conn, "tiktok")["fail_streak"] == 0


def test_success_resets_streak(conn):
    node = {"id": "x", "params": {"max_failures": 3}}
    bad = types.SimpleNamespace(run=lambda c, n: (_ for _ in ()).throw(RuntimeError("x")))
    good = types.SimpleNamespace(run=lambda c, n: 1)
    for m in (bad, bad, good, bad, bad):
        run.run_node(conn, node, m, notify=lambda *a, **k: None)
    assert db.health_row(conn, "x")["disabled_at"] is None


def test_dashboard_banner_shows_broken_sources(tmp_path):
    from sonar import web
    f = tmp_path / "s.db"
    c = db.connect(f)
    db.record_run(c, "tiktok", ok=False, error="blocked")
    db.set_disabled(c, "tiktok", True)
    c.commit()
    data = {"nodes": [{"id": "tiktok", "type": "source.tiktok", "label": "TikTok", "enabled": True, "every": "6h"}]}
    site = web.Site(data, db_path=f, digests_dir=tmp_path)
    assert "TikTok (disabled)" in site.page_digests()
    assert "TikTok" not in site.page_login()  # health stays private before sign-in
