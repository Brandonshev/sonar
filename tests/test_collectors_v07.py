"""YouTube, Google Trends and Wikipedia against recorded responses (YouTube: a hand-built sample in
the documented API shape, since recording needs a key). No live calls."""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

import pytest

from sonar import db
from sonar.collectors import google_trends, google_trends_rising, wikipedia, youtube
from sonar.collectors.base import Collector
from sonar.entities import alias_key
from sonar.scoring.heat import score_all

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn():
    return db.connect(":memory:")


def route(monkeypatch, routes, cls=Collector):
    calls = []

    def request(self, url, headers=None, data=None, raw=False):
        calls.append(url)
        for part, resp in routes:
            if part in url:
                resp = resp(url) if callable(resp) else resp
                return resp, {}
        raise AssertionError(f"unexpected live call: {url}")
    monkeypatch.setattr(cls, "request", request)
    return calls


# ---------- YouTube ----------

def video(vid, views, title="A video", channel="Chan"):
    return {"id": vid, "snippet": {"title": title, "channelTitle": channel, "channelId": "c1",
                                   "publishedAt": "2026-11-23T10:00:00Z"},
            "statistics": {"viewCount": str(views), "likeCount": "10", "commentCount": "2"}}


def test_youtube_charts_search_tracked_and_quota(conn, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    monkeypatch.setattr(youtube, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: datetime(2026, 11, 24, 13, tzinfo=tz))}))
    db.upsert_item(conn, "youtube", {"kind": "video", "external_id": "old1"})  # tracked from an earlier run
    calls = route(monkeypatch, [
        ("chart=mostPopular", lambda u: {"items": [video("AU1" if "regionCode=AU" in u else "US1", 1000)]}),
        ("/search?", {"items": [{"id": {"videoId": "S1"}}, {"id": {"kind": "youtube#channel"}}]}),
        ("/videos?", lambda u: {"items": [video(v, 50) for v in u.split("id=")[1].split("&")[0].split("%2C")]}),
    ])
    node = {"id": "youtube", "params": {"regions": ["AU", "US"], "topics": ["ai tools"], "search_rounds_per_day": 4}}
    assert youtube.run(conn, node) == 4  # AU1, US1, S1, old1
    meta = json.loads(conn.execute("SELECT meta_json FROM items WHERE external_id='S1'").fetchone()[0])
    assert meta["seen_in"] == ["topic:ai tools"]
    units = dict(conn.execute("SELECT source, units FROM api_usage").fetchall())
    assert units["youtube"] == 1 + 1 + 100 + 1  # 2 charts, 1 search, 1 stats call
    assert units["youtube.search_slot2"] == 1  # 13:00 Pacific = third 6-hour slot
    # a second run in the same 6-hour window does not search again
    youtube.run(conn, node)
    assert sum("/search?" in c for c in calls) == 1


def test_youtube_stops_at_budget(conn, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    route(monkeypatch, [("chart=mostPopular", {"items": [video("A", 5)]})])
    node = {"id": "youtube", "params": {"regions": ["AU", "US", "GB"], "daily_budget": 2, "topics": []}}
    assert youtube.run(conn, node) == 1  # third chart call would pass the budget: stops cleanly, keeps data


def test_youtube_needs_key(conn, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="YOUTUBE_API_KEY"):
        youtube.run(conn, {"id": "youtube"})


# ---------- Google Trends ----------

def test_google_trends_rss(conn, monkeypatch):
    xml = (FIX / "google_trends_au.xml").read_bytes()
    route(monkeypatch, [("trending/rss?geo=AU", xml)])
    assert google_trends.run(conn, {"id": "google_trends", "params": {"geos": ["AU"]}}) == 3
    row = conn.execute("SELECT * FROM items WHERE title='vaibhav sooryavanshi'").fetchone()
    assert row["category"] == "Search AU" and row["alias_key"] == "name:vaibhavsooryavanshi"
    assert json.loads(row["meta_json"])["news_url"].startswith("https://")
    vals = {r[0]: r[1] for r in conn.execute("SELECT i.title, s.value FROM snapshots s JOIN items i ON i.id=s.item_id")}
    assert vals["vaibhav sooryavanshi"] == 500


@pytest.mark.parametrize("text,n", [("200+", 200), ("2K+", 2000), ("1,000+", 1000), ("1M+", 1_000_000), ("", 0), (None, 0)])
def test_traffic_parse(text, n):
    assert google_trends.traffic(text) == n


def test_google_trends_empty_feed_is_an_error(conn, monkeypatch):
    route(monkeypatch, [("trending/rss", b"<rss><channel></channel></rss>")])
    with pytest.raises(RuntimeError, match="no items"):
        google_trends.run(conn, {"id": "google_trends", "params": {"geos": ["AU"]}})


def test_google_trends_rising(conn, monkeypatch):
    fx = json.loads((FIX / "google_trends_rising.json").read_text())
    monkeypatch.setattr(google_trends_rising.time, "sleep", lambda s: None)
    route(monkeypatch, [("/trends/?geo", b""), ("/explore?", fx["explore"].encode()),
                        ("/relatedsearches?", fx["related"].encode())], cls=google_trends_rising.GoogleTrendsRising)
    n = google_trends_rising.run(conn, {"id": "google_trends_rising", "params": {"topics": ["AI app"], "per_topic": 3}})
    assert n == 3
    rows = conn.execute("SELECT title, category FROM items").fetchall()
    assert all(r["category"] == "Rising: AI app" for r in rows)


def test_backoff_retries_then_succeeds(monkeypatch):
    import urllib.request
    tries, slept = [], []

    def fake_urlopen(req, timeout):
        tries.append(1)
        if len(tries) < 3:
            raise HTTPError(req.full_url, 429, "Too Many", {}, None)

        class R:
            headers = {}
            def read(self): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return R()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("sonar.collectors.base.time.sleep", slept.append)
    c = google_trends.GoogleTrends({"id": "google_trends"})
    assert c.request("https://example.com", raw=True)[0] == b"ok"
    assert slept == [20, 40]


# ---------- Wikipedia ----------

def wiki_routes(per_article=None, query=None):
    top = json.loads((FIX / "wiki_top.json").read_text())
    pa = json.loads((FIX / "wiki_per_article.json").read_text())
    return [("/top/en.wikipedia/", top),
            ("/per-article/", per_article or pa),
            ("w/api.php", query or (lambda u: json.loads((FIX / "wiki_query_taylorswift.json").read_text())))]


def test_wikipedia_top_articles_backfilled_as_running_totals(conn, monkeypatch):
    route(monkeypatch, wiki_routes())
    n = wikipedia.run(conn, {"id": "wikipedia", "params": {"top": 3}})
    titles = [r[0] for r in conn.execute("SELECT external_id FROM items ORDER BY id")]
    assert n == 3 and titles == ["Ramayana:_Part_1", "Grand_Theft_Auto_VI", "Jensen_Huang"]  # namespaces skipped
    pa = json.loads((FIX / "wiki_per_article.json").read_text())["items"]
    vals = [r[0] for r in conn.execute(
        "SELECT value FROM snapshots s JOIN items i ON i.id=s.item_id WHERE i.external_id='Jensen_Huang' ORDER BY ts")]
    assert len(vals) == len(pa) and vals[-1] == sum(d["views"] for d in pa)  # cumulative, one per day
    first_seen = conn.execute("SELECT first_seen FROM items WHERE external_id='Jensen_Huang'").fetchone()[0]
    assert first_seen == wikipedia.day_end(datetime.strptime(pa[0]["timestamp"][:8], "%Y%m%d"))  # backdated


def test_wikipedia_incremental_second_run(conn, monkeypatch):
    calls = route(monkeypatch, wiki_routes())
    wikipedia.run(conn, {"id": "wikipedia", "params": {"top": 1}})
    route(monkeypatch, wiki_routes(per_article={"items": []}))
    wikipedia.run(conn, {"id": "wikipedia", "params": {"top": 1}})
    n = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert n == len(json.loads((FIX / "wiki_per_article.json").read_text())["items"])  # nothing duplicated


def q(name):
    return json.loads((FIX / f"wiki_query_{name}.json").read_text())


@pytest.mark.parametrize("fixture,software,expect", [
    ("taylorswift", False, "Taylor_Swift"),               # a search term: any real article
    ("taylorswift", True, None),                          # a repo called that: not software, refuse
    ("ladybird", True, None),                             # 'Ladybird' redirects to the beetle article
    ("radar", True, None),                                # a repo called 'radar' is not the radio system
    ("ladybirdwebbrowser", True, "Ladybird_(web_browser)"),
])
def test_wikipedia_match_avoids_false_links(monkeypatch, fixture, software, expect):
    route(monkeypatch, [("w/api.php", q(fixture))])
    w = wikipedia.Wikipedia({"id": "wikipedia"})
    assert w.match("x", require_software=software) == expect


def test_wikipedia_links_climbing_names_and_scorer_gives_cross_source_bonus(conn, monkeypatch):
    now = int(datetime.now(timezone.utc).timestamp())
    # a Google Trends search that Wikipedia also has an article for
    db.upsert_item(conn, "google_trends", {"kind": "search", "external_id": "AU:taylor swift", "title": "Taylor Swift",
                                           "alias_key": alias_key("Taylor Swift")}, now=now - 3600)
    route(monkeypatch, [("/top/en.wikipedia/", {"items": [{"articles": []}]}),
                        ("/per-article/", json.loads((FIX / "wiki_per_article.json").read_text())),
                        ("w/api.php", q("taylorswift"))])
    wikipedia.run(conn, {"id": "wikipedia", "params": {}})
    wiki = conn.execute("SELECT * FROM items WHERE source='wikipedia'").fetchone()
    assert wiki["external_id"] == "Taylor_Swift" and wiki["alias_key"] == "name:taylorswift"
    assert conn.execute("SELECT result FROM lookups").fetchone()[0] == "Taylor_Swift"
    # looked up once only
    route(monkeypatch, [("/top/en.wikipedia/", {"items": [{"articles": []}]}), ("/per-article/", {"items": []})])
    wikipedia.run(conn, {"id": "wikipedia", "params": {}})


def test_scorer_links_by_alias_key(conn):
    from tests.test_heat import NOW, add, daily
    a = add(conn, "github", "acme/radarly", daily(40, 2, jump=300), url="https://github.com/acme/radarly")
    b = add(conn, "hackernews", "hn", daily(0, 5, jump=300), url="https://radarly.app/")
    conn.execute("UPDATE items SET alias_key='name:radarly' WHERE id IN (?, ?)", (a, b))
    score_all(conn, {"id": "heat", "params": {"metrics": {"github": {"stars": 5}, "hackernews": {"points": 10}}}}, now=NOW)
    reasons = json.loads(conn.execute("SELECT reasons_json FROM scores WHERE item_id=?", (a,)).fetchone()[0])
    assert any("also climbing on hackernews" in r for r in reasons)


@pytest.mark.parametrize("name,min_len,key", [
    ("Radarly: Spot trends early", 5, "name:radarly"), ("Radar (radio)", 5, "name:radar"), ("radar", 6, None),
    ("Dashboard", 5, None), ("2026", 5, None), ("Taylor Swift", 5, "name:taylorswift"), ("", 5, None),
])
def test_alias_key(name, min_len, key):
    assert alias_key(name, min_len) == key
