"""Companies: matching, news counting, the roll-up score, chained scorers and the Companies tab."""
import json
import types

import pytest

from sonar import companies, db, run, watchlist
from sonar.collectors import news
from sonar.collectors.base import Collector
from sonar.scoring import companies as company_scorer

DAY = 86400
COMPANIES = {"companies": [
    {"name": "Apple", "aliases": ["Apple", "iPhone"], "domain": "apple.com", "github": "apple", "wikipedia": "Apple_Inc."},
    {"name": "Meta", "aliases": ["Meta Platforms", "Meta's"], "domain": "meta.com"},
    {"name": "Radarly", "aliases": ["Radarly"], "domain": "radarly.app"},
]}
RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Apple unveils iPhone 18</title><link>https://example.com/a?utm_source=x</link>
<description>&lt;b&gt;Big&lt;/b&gt; day</description></item>
<item><title>Meta-analysis of sleep</title><link>https://example.com/b</link></item>
<item><title>Why Meta's AI bet matters, and what Apple thinks</title><link>https://example.com/c</link></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Radarly raises seed round</title><link href="https://example.org/r"/><summary>Apple alumni</summary></entry>
</feed>"""


@pytest.fixture
def cfile(tmp_path):
    f = tmp_path / "companies.json"
    f.write_text(json.dumps(COMPANIES))
    companies.load.cache_clear()
    return str(f)


@pytest.fixture
def conn():
    return db.connect(":memory:")


def test_matching_is_careful(cfile):
    cs = companies.load(cfile)
    assert companies.mentioned("Apple unveils iPhone", cs) == {"apple"}
    assert companies.mentioned("apple pie, pineapple and Snapple", cs) == set()
    assert companies.mentioned("Meta-analysis", cs) == set()
    assert companies.mentioned("Meta's new glasses", cs) == {"meta"}
    assert cs[0]["key"] == "wiki:Apple_Inc." and cs[2]["key"] == "company:radarly"


def test_real_company_list_loads():
    companies.load.cache_clear()
    cs = companies.load()
    assert len(cs) > 40 and len({c["slug"] for c in cs}) == len(cs)


def test_rss_and_atom_entries():
    assert news.entries(RSS)[0] == ("Apple unveils iPhone 18", "https://example.com/a?utm_source=x", " Big  day")
    assert news.entries(ATOM)[0][:2] == ("Radarly raises seed round", "https://example.org/r")


def run_news(conn, monkeypatch, cfile, feeds):
    monkeypatch.setattr(Collector, "request", lambda self, url, headers=None, data=None, raw=False: (feeds[url], {}))
    return news.run(conn, {"id": "news", "params": {"companies_file": cfile, "feeds": list(feeds)}})


def totals(conn, slug):
    item = conn.execute("SELECT id FROM items WHERE source='news' AND external_id=?", (slug,)).fetchone()[0]
    return {m: conn.execute("SELECT value FROM snapshots WHERE item_id=? AND metric=? ORDER BY ts DESC LIMIT 1",
                            (item, m)).fetchone()[0] for m in news.METRICS}


def test_news_first_run_is_baseline_then_counts_new_only(conn, monkeypatch, cfile):
    assert run_news(conn, monkeypatch, cfile, {"f1": RSS}) == 0  # first run: just remember what's there
    assert totals(conn, "apple")["mentions"] == 0
    hn = db.upsert_item(conn, "hackernews", {"kind": "story", "external_id": "1", "title": "Ask HN: Is Apple's iPhone worth it?"})
    db.upsert_item(conn, "reddit", {"kind": "post", "external_id": "2", "title": "Meta Platforms layoffs"})
    conn.commit()
    n = run_news(conn, monkeypatch, cfile, {"f1": RSS, "f2": ATOM})  # RSS already seen; ATOM is new
    assert n == 4  # radarly + apple (atom summary) from the new feed, apple from HN, meta from Reddit
    t = totals(conn, "apple")
    assert t == {"mentions": 1, "hn_mentions": 1, "reddit_mentions": 0}
    assert totals(conn, "meta")["reddit_mentions"] == 1
    assert run_news(conn, monkeypatch, cfile, {"f1": RSS, "f2": ATOM}) == 0  # nothing counted twice
    heads = json.loads(conn.execute("SELECT meta_json FROM items WHERE external_id='radarly'").fetchone()[0])["headlines"]
    assert heads[0]["title"] == "Radarly raises seed round"
    assert hn


def test_news_all_feeds_failing_raises(conn, monkeypatch, cfile):
    def boom(self, url, headers=None, data=None, raw=False):
        raise OSError("down")
    monkeypatch.setattr(Collector, "request", boom)
    with pytest.raises(RuntimeError, match="all 2 news feeds failed"):
        news.run(conn, {"id": "news", "params": {"companies_file": cfile, "feeds": ["a", "b"]}})


def series(conn, source, ext, metric, per_day, jump, key=None):
    item = db.upsert_item(conn, source, {"kind": "x", "external_id": ext, "entity_key": key}, now=NOW - 10 * DAY)
    for d in range(10, 0, -1):
        db.add_snapshots(conn, item, {metric: per_day * (10 - d)}, now=NOW - d * DAY)
    db.add_snapshots(conn, item, {metric: per_day * 9 + jump}, now=NOW)
    return item


NOW = 1_800_000_000


def test_company_rollup_breadth_and_breakdown(conn, cfile):
    conn.execute("INSERT INTO items (source, kind, external_id, first_seen) VALUES ('x','x','x',0)")
    conn.execute("INSERT INTO scores VALUES (1, ?, 0, 0, '[]')", (NOW,))  # the heat batch this joins
    for metric, per_day, jump in [("mentions", 3, 30), ("hn_mentions", 1, 6)]:
        item = conn.execute("SELECT id FROM items WHERE external_id='apple'").fetchone()
        if not item:
            series(conn, "news", "apple", metric, per_day, jump, key="wiki:Apple_Inc.")
        else:
            for d in range(10, 0, -1):
                db.add_snapshots(conn, item[0], {metric: per_day * (10 - d)}, now=NOW - d * DAY)
            db.add_snapshots(conn, item[0], {metric: per_day * 9 + jump}, now=NOW)
    series(conn, "wikipedia", "Apple_Inc.", "views", 50_000, 52_000)  # flat: about 1x
    repo = db.upsert_item(conn, "github", {"kind": "repo", "external_id": "apple/swift-x", "url": "https://github.com/apple/swift-x"})
    conn.execute("INSERT INTO scores VALUES (?, ?, 4.0, 40, '[]')", (repo, NOW))
    series(conn, "news", "meta", "mentions", 5, 5)  # Meta: normal day
    conn.commit()

    company_scorer.run(conn, {"id": "companies", "params": {"companies_file": cfile}}, now=NOW)
    rows = {r["title"]: r for r in conn.execute(
        "SELECT i.title, i.meta_json, s.heat, s.score, s.reasons_json, s.ts FROM scores s JOIN items i ON i.id=s.item_id "
        "WHERE i.source='companies'")}
    apple = rows["Apple"]
    signals = json.loads(apple["meta_json"])["signals"]
    assert signals["news"] == pytest.approx(10.0) and signals["hn"] == pytest.approx(6.0) and signals["github"] == 4.0
    assert apple["heat"] == pytest.approx(10.0 * 1.5)  # best 10x, +25% each for hn and github also >= 2x
    reasons = json.loads(apple["reasons_json"])
    assert reasons[0].startswith("news: 30 mentions in 24h, 10.0x normal") and "3 signals at once" in reasons[-1]
    assert apple["ts"] == NOW and rows["Meta"]["score"] < apple["score"]
    assert "Apple_Inc." in watchlist.values(conn, "wikipedia") and "apple" in watchlist.values(conn, "github_orgs")
    events = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM events WHERE type='trend.found'")]
    assert [e["item"]["title"] for e in events] == ["Apple"]


def test_scorer_chain_runs_in_one_pass(conn):
    data = {"version": 1, "nodes": [
        {"id": "src", "type": "source.x", "enabled": True, "every": "1h"},
        {"id": "heat", "type": "scorer.heat", "enabled": True},
        {"id": "companies", "type": "scorer.companies", "enabled": True},
        {"id": "out", "type": "output.x", "enabled": True}],
        "edges": [["src", "heat"], ["heat", "companies"], ["companies", "out"]]}
    order = []
    mods = {n: types.SimpleNamespace(run=lambda c, node, n=n: order.append(n) or 0) for n in ("src", "heat", "companies", "out")}
    run.run_all_due(conn, data, now=NOW, modules=mods)
    assert order == ["src", "heat", "companies", "out"]


def test_companies_tab_shows_breakdown_and_disclaimer(tmp_path):
    from sonar import pipeline, web
    f = tmp_path / "s.db"
    c = db.connect(f)
    item = db.upsert_item(c, "companies", {"kind": "company", "external_id": "apple", "title": "Apple", "category": "company",
                                           "meta": {"signals": {"news": 10.0, "github": 4.0}}})
    c.execute("INSERT INTO scores VALUES (?, ?, 15, 90, ?)", (item, NOW, json.dumps(["news: 30 mentions in 24h, 10.0x normal"])))
    c.commit()
    page = web.Site(pipeline.load(), db_path=f, digests_dir=tmp_path).page_top(NOW, "Companies")
    assert "news 10.0×" in page and "github 4.0×" in page and "Attention signals only, not financial advice" in page
