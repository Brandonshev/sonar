"""News: company mentions in Google News and other RSS/Atom feeds, plus Hacker News and Reddit titles.

For every company in companies.json this keeps three running totals, counting each article or post
once: mentions (news feeds), hn_mentions and reddit_mentions. Running totals mean the heat maths
("mentions in the last 24h vs the usual daily rate") works unchanged. The very first run only
records what's already in the feeds, so day one isn't one giant fake spike.
"""
import json
import re
import time
import xml.etree.ElementTree as ET
from html import unescape

from .. import companies as company_list
from .. import db
from ..entities import normalize_url
from .base import Collector

TAG = re.compile(r"<[^>]+>")
METRICS = ("mentions", "hn_mentions", "reddit_mentions")


def entries(xml: bytes) -> list[tuple[str, str, str]]:
    """(title, link, summary text) for every RSS <item> or Atom <entry>."""
    out = []
    for el in ET.fromstring(xml).iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        fields = {c.tag.split("}")[-1]: c for c in el}
        title = (fields["title"].text or "").strip() if "title" in fields else ""
        link_el = fields.get("link")
        link = ((link_el.text or "").strip() or link_el.get("href", "")) if link_el is not None else ""
        summary = next((fields[k].text or "" for k in ("description", "summary", "content") if k in fields), "")
        out.append((title, link, unescape(TAG.sub(" ", unescape(summary)))))
    return out


class News(Collector):
    retries = 2

    def run(self, conn) -> int:
        self.conn = conn
        p, now = self.params, int(time.time())
        comps = company_list.load(p.get("companies_file") or company_list.ROOT / "companies.json")
        first_run = conn.execute("SELECT 1 FROM news_seen LIMIT 1").fetchone() is None
        new = {c["slug"]: dict.fromkeys(METRICS, 0) for c in comps}
        headlines: dict[str, list] = {}
        seen: list[str] = []

        def count(key, text, metric, headline=None):
            if key in seen or conn.execute("SELECT 1 FROM news_seen WHERE key=?", (key,)).fetchone():
                return
            seen.append(key)
            for s in company_list.mentioned(text, comps):
                if not first_run:
                    new[s][metric] += 1
                if headline:
                    headlines.setdefault(s, []).append(headline)

        feeds = p.get("feeds", [])
        failed = 0
        for url in feeds:
            try:
                body, _ = self.request(url, raw=True)
                for title, link, summary in entries(body):
                    count(f"url:{normalize_url(link) or title}", f"{title} {summary}", "mentions",
                          {"title": title, "url": link})
            except Exception as e:
                failed += 1
                self.log.warning("feed failed: %s (%s)", url.split("?")[0], e)
        if feeds and failed == len(feeds):
            raise RuntimeError(f"all {failed} news feeds failed")
        for item_id, title, source in conn.execute(
                "SELECT id, title, source FROM items WHERE source IN ('hackernews', 'reddit') AND first_seen >= ?",
                (now - 2 * 86400,)).fetchall():
            count(f"item:{item_id}", title, "hn_mentions" if source == "hackernews" else "reddit_mentions")

        with conn:
            conn.executemany("INSERT OR IGNORE INTO news_seen (key, ts) VALUES (?, ?)", [(k, now) for k in seen])
            for c in comps:
                old = conn.execute("SELECT id, meta_json FROM items WHERE source=? AND external_id=?",
                                   (self.id, c["slug"])).fetchone()
                totals = {m: self.last(old["id"], m) if old else 0 for m in METRICS}
                recent = (headlines.get(c["slug"], []) + (json.loads(old["meta_json"]).get("headlines", [])
                                                          if old else []))[:5]
                item_id = db.upsert_item(conn, self.id, {
                    "kind": "company_mentions", "external_id": c["slug"], "title": f"{c['name']} mentions",
                    "url": f"https://news.google.com/search?q={c['name'].replace(' ', '+')}", "category": "news",
                    "entity_key": c["key"], "meta": {"headlines": recent}}, now)
                db.add_snapshots(conn, item_id, {m: totals[m] + new[c["slug"]][m] for m in METRICS}, now)
        n = sum(sum(v.values()) for v in new.values())
        self.log.info("%d new mentions from %d articles/posts%s", n, len(seen), " (first run: baseline only)" if first_run else "")
        return n

    def last(self, item_id: int, metric: str) -> float:
        row = self.conn.execute("SELECT value FROM snapshots WHERE item_id=? AND metric=? ORDER BY ts DESC LIMIT 1",
                                (item_id, metric)).fetchone()
        return row[0] if row else 0


def run(conn, node):
    return News(node).run(conn)
