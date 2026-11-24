"""Collectors against recorded/sample responses. No live network calls."""
import json
from pathlib import Path

import pytest

from sonar import db
from sonar.collectors import github, hackernews, producthunt, reddit
from sonar.collectors.base import Collector

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def conn():
    return db.connect(":memory:")


@pytest.fixture
def fake_http(monkeypatch):
    """routes: list of (url substring, response or callable(url)). Records every call."""
    def install(routes, headers=None):
        calls = []

        def request(self, url, headers_=None, data=None):
            calls.append((url, data))
            for part, resp in routes:
                if part in url:
                    return (resp(url) if callable(resp) else resp), (headers or {})
            raise AssertionError(f"unexpected live call: {url}")
        monkeypatch.setattr(Collector, "request", request)
        return calls
    return install


def items(conn):
    return {r["external_id"]: dict(r) for r in conn.execute("SELECT * FROM items")}


def metrics(conn, external_id):
    return {r["metric"]: r["value"] for r in conn.execute(
        "SELECT metric, value FROM snapshots JOIN items ON items.id = item_id WHERE external_id = ?",
        (external_id,))}


def test_github_search_plus_tracked(conn, fake_http, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    # a repo first seen on another source must get tracked by the GitHub collector
    db.upsert_item(conn, "hackernews", {"kind": "story", "external_id": "1",
                                        "url": "https://github.com/Zed-Industries/gpui"})
    repo = {"full_name": "zed-industries/gpui", "html_url": "https://github.com/zed-industries/gpui",
            "language": "Rust", "stargazers_count": 900, "forks_count": 40}
    calls = fake_http([("/search/repositories", fixture("github_search.json")),
                       ("/repos/zed-industries/gpui", repo)])
    n = github.run(conn, {"id": "github", "type": "source.github", "enabled": True,
                          "params": {"search_pages": 1, "delay_seconds": 0}})
    search = fixture("github_search.json")["items"]
    assert n == len(search) + 1
    first = search[0]
    assert metrics(conn, first["full_name"].lower()) == {"stars": first["stargazers_count"],
                                                         "forks": first["forks_count"]}
    keys = {r["entity_key"] for r in conn.execute("SELECT entity_key FROM items")}
    assert "github:zed-industries/gpui" in keys
    assert any("created%3A%3E" in u for u, _ in calls)


def test_github_skips_deleted_tracked_repo(conn, fake_http):
    from urllib.error import HTTPError
    db.upsert_item(conn, "hackernews", {"kind": "story", "external_id": "1", "url": "https://github.com/gone/repo"})

    def gone(url):
        raise HTTPError(url, 404, "Not Found", {}, None)
    fake_http([("/search/repositories", {"items": []}), ("/repos/gone/repo", gone)])
    assert github.run(conn, {"id": "github", "type": "source.github", "enabled": True,
                             "params": {"search_pages": 1}}) == 0


def test_hackernews(conn, fake_http):
    hn = fixture("hackernews.json")
    fake_http([("/topstories.json", hn["ids"]), ("/newstories.json", hn["ids"][:1]),
               ("/beststories.json", []),
               ("/item/", lambda url: hn["items"][url.rsplit("/", 1)[1].removesuffix(".json")])])
    n = hackernews.run(conn, {"id": "hackernews", "type": "source.hackernews", "enabled": True, "params": {}})
    assert n == len(hn["ids"]) - 1  # the job posting is skipped
    got = items(conn)
    assert got["50619103"]["entity_key"] == "github:zed-industries/gpui"
    assert got["50614249"]["entity_key"] == "url:ladybird.org/posts/alpha"
    assert got["90000001"]["entity_key"] == "github:example/radar"  # link pulled from post text
    assert got["90000001"]["category"] == "show"
    assert set(metrics(conn, "50619103")) == {"points", "comments"}


def test_producthunt(conn, fake_http, monkeypatch):
    monkeypatch.setenv("PRODUCTHUNT_TOKEN", "test-token")
    monkeypatch.setattr(producthunt.ProductHunt, "resolve",
                        lambda self, url: "https://www.radarly.app/?ref=producthunt" if "ABC123" in url else None)
    calls = fake_http([("api.producthunt.com", fixture("producthunt.json"))])
    node = {"id": "producthunt", "type": "source.producthunt", "enabled": True, "params": {}}
    assert producthunt.run(conn, node) == 2
    got = items(conn)
    assert got["1010"]["entity_key"] == "site:radarly.app"
    assert got["1010"]["category"] == "Productivity"
    assert got["1011"]["title"] == "Quietdesk"
    assert metrics(conn, "1010") == {"votes": 321, "comments": 45}
    assert "postedAfter" in calls[0][1]["query"]  # GraphQL POST body

    # second run reuses the stored website instead of resolving again
    monkeypatch.setattr(producthunt.ProductHunt, "resolve", lambda self, url: pytest.fail("resolved twice"))
    producthunt.run(conn, {**node, "params": {"resolve_per_run": 0}})
    assert items(conn)["1010"]["entity_key"] == "site:radarly.app"


def test_producthunt_without_token_fails_clearly(conn, monkeypatch):
    monkeypatch.delenv("PRODUCTHUNT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="PRODUCTHUNT_TOKEN"):
        producthunt.run(conn, {"id": "producthunt", "type": "source.producthunt", "enabled": True})


def test_reddit_public(conn, fake_http, monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    calls = fake_http([("/hot.json", fixture("reddit_listing.json")),
                       ("/rising.json", {"data": {"children": []}})])
    node = {"id": "reddit", "type": "source.reddit", "enabled": True,
            "params": {"subreddits": ["programming"], "delay_seconds": 0}}
    assert reddit.run(conn, node) == 3  # sticky skipped
    got = items(conn)
    assert got["t3_aaa111"]["entity_key"] == "github:zed-industries/gpui"
    assert got["t3_bbb222"]["entity_key"] == "url:ladybird.org/posts/alpha"  # utm stripped
    assert got["t3_ccc333"]["entity_key"] == "reddit:t3_ccc333"
    assert got["t3_ccc333"]["category"] == "programming"
    assert all(u.startswith("https://www.reddit.com/r/programming/") for u, _ in calls)


def test_reddit_oauth(conn, fake_http, monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    calls = fake_http([("/api/v1/access_token", {"access_token": "tok"}),
                       ("oauth.reddit.com", fixture("reddit_listing.json"))])
    reddit.run(conn, {"id": "reddit", "type": "source.reddit", "enabled": True,
                      "params": {"subreddits": ["programming"], "listings": ["hot"], "delay_seconds": 0}})
    assert calls[0][1] == b"grant_type=client_credentials"
    assert calls[1][0].startswith("https://oauth.reddit.com/r/programming/hot.json")


def test_cross_source_link(conn, fake_http, monkeypatch):
    """The same repo on HN and Reddit shares one entity key."""
    test_hackernews(conn, fake_http)
    test_reddit_public(conn, fake_http, monkeypatch)
    sources = {r["source"] for r in conn.execute(
        "SELECT source FROM items WHERE entity_key = 'github:zed-industries/gpui'")}
    assert sources == {"hackernews", "reddit"}
