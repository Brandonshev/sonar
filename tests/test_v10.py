"""Lookout feed: emission rules, the /api/trends endpoint, auth, rate limit and cursors."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from sonar import db, web
from sonar.outputs import lookout_feed

NOW = 1_800_000_000
NODE = {"id": "lookout", "type": "output.lookout_feed", "enabled": True,
        "params": {"categories": ["social.video"], "min_score": 60, "rate_limit_per_minute": 100}}


def setup_items(conn, ts=NOW, grwm_score=80):
    tag = db.upsert_item(conn, "tiktok", {"kind": "hashtag", "external_id": "grwm", "title": "#grwm", "category": "social.video",
                                          "url": "https://www.tiktok.com/tag/grwm", "entity_key": "tiktok:grwm", "alias_key": "tag:grwm"})
    vid = db.upsert_item(conn, "youtube", {"kind": "video", "external_id": "V1", "title": "GRWM", "category": "social.video",
                                           "url": "https://www.youtube.com/watch?v=V1", "alias_key": "tag:grwm"})
    repo = db.upsert_item(conn, "github", {"kind": "repo", "external_id": "a/b", "title": "a/b", "category": "Rust",
                                           "url": "https://github.com/a/b"})
    for item, score in ((tag, grwm_score), (vid, 50), (repo, 95)):
        conn.execute("INSERT INTO scores VALUES (?, ?, ?, ?, ?)", (item, ts, score / 8, score, json.dumps([f"reason {score}"])))
    conn.commit()
    return tag


def events(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM events WHERE type='trend.found'")]


def test_emits_only_category_above_threshold_with_examples():
    conn = db.connect(":memory:")
    setup_items(conn)
    assert lookout_feed.run(conn, NODE, now=NOW) == 1  # video at 50 too low, repo wrong category
    e = events(conn)[0]
    assert e["feed"] == "lookout" and e["item"]["title"] == "#grwm" and e["score"] == 80
    assert e["sources"] == ["tiktok", "youtube"]
    assert e["examples"] == ["https://www.tiktok.com/tag/grwm", "https://www.youtube.com/watch?v=V1"]
    assert e["reasons"] == ["reason 80"] and e["sonar_version"]


def test_cooldown_and_rejump():
    conn = db.connect(":memory:")
    setup_items(conn)
    lookout_feed.run(conn, NODE, now=NOW)
    assert lookout_feed.run(conn, NODE, now=NOW + 600) == 0          # same item, still 80: cooldown
    tag = conn.execute("SELECT id FROM items WHERE external_id='grwm'").fetchone()[0]
    conn.execute("INSERT INTO scores VALUES (?, ?, 12, 100, '[]')", (tag, NOW + 900))
    conn.commit()
    assert lookout_feed.run(conn, NODE, now=NOW + 900) == 1          # +20: sent again


@pytest.fixture
def api(tmp_path, monkeypatch):
    f = tmp_path / "s.db"
    conn = db.connect(f)
    setup_items(conn)
    lookout_feed.run(conn, NODE, now=NOW)
    conn.close()
    monkeypatch.setenv("LOOKOUT_TOKEN", "t" * 40)
    web.Handler.site = web.Site({"nodes": [NODE]}, db_path=f, digests_dir=tmp_path)
    web.Handler.api_limit = web.RateLimit()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/api/trends", f
    httpd.shutdown()


def call(url, token="t" * 40):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.load(e), e.headers


def test_api_auth_and_results(api):
    url, _ = api
    assert call(url, token=None)[0] == 401
    assert call(url, token="wrong")[0] == 401
    code, body, h = call(url + "?category=social.video&min_score=60")
    assert code == 200 and h["Content-Type"] == "application/json"
    assert body["count"] == 1 and body["events"][0]["item"]["title"] == "#grwm"
    assert body["next_after_id"] == body["events"][0]["id"] and body["next_since"] == NOW


def test_api_filters_and_cursor(api):
    url, _ = api
    _, body, _ = call(url)
    after = body["next_after_id"]
    assert call(url + f"?after_id={after}")[1]["count"] == 0         # nothing new after the cursor
    assert call(url + f"?since={NOW}")[1]["count"] == 0
    assert call(url + "?min_score=90")[1]["count"] == 0              # can raise the threshold...
    assert call(url + "?min_score=10")[1]["count"] == 1              # ...but never lower it below 60
    assert call(url + "?category=Rust")[0] == 400                    # not a published category
    assert call(url + "?since=abc")[0] == 400


def test_api_rate_limit(api, monkeypatch):
    url, _ = api
    monkeypatch.setitem(web.Handler.site.data["nodes"][0], "params", {**NODE["params"], "rate_limit_per_minute": 3})
    codes = [call(url)[0] for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_api_off_without_token(api, monkeypatch):
    url, _ = api
    monkeypatch.delenv("LOOKOUT_TOKEN")
    assert call(url)[0] == 404


def test_api_skips_dashboard_login_but_pages_still_need_it(api):
    url, _ = api
    from tests.test_web import opener
    try:
        opener.open(url.removesuffix("/api/trends") + "/health")
        assert False, "expected a redirect to /login"
    except urllib.error.HTTPError as e:
        assert e.code == 303 and e.headers["Location"] == "/login"
