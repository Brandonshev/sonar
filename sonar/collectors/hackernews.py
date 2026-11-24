"""Hacker News via the official Firebase API (no key, no published rate limit; we stay polite).

The story's link becomes the item url, so its entity key matches the same repo or product
on other sources. Text posts (Ask/Show HN) fall back to the first GitHub link in the text.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape

from .base import Collector

API = "https://hacker-news.firebaseio.com/v0"
GITHUB_LINK = re.compile(r'https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+')


class HackerNews(Collector):
    def fetch(self):
        p = self.params
        ids: list[int] = []
        for listing, n in (("topstories", p.get("top", 100)), ("newstories", p.get("new", 100)),
                           ("beststories", p.get("best", 50))):
            ids += self.get_json(f"{API}/{listing}.json")[:n]
        ids = list(dict.fromkeys(ids))  # dedupe, keep order
        with ThreadPoolExecutor(p.get("workers", 6)) as pool:
            stories = pool.map(lambda i: self.get_json(f"{API}/item/{i}.json"), ids)
        return [to_result(s) for s in stories if s and s.get("type") == "story"
                and not s.get("dead") and not s.get("deleted")]


def to_result(s: dict):
    hn_url = f"https://news.ycombinator.com/item?id={s['id']}"
    link = s.get("url")
    if not link and (m := GITHUB_LINK.search(unescape(s.get("text") or ""))):
        link = m.group(0)
    item = {
        "kind": "story",
        "external_id": str(s["id"]),
        "title": s.get("title"),
        "url": link or hn_url,
        "category": "show" if (s.get("title") or "").startswith("Show HN") else "story",
        "meta": {"hn_url": hn_url, "by": s.get("by"), "posted": s.get("time")},
    }
    if not link:
        item["entity_key"] = f"hn:{s['id']}"  # discussion only; links to nothing else
    return item, {"points": s.get("score", 0), "comments": s.get("descendants", 0)}


def run(conn, node):
    return HackerNews(node).run(conn)
