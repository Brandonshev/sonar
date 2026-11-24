"""Google Trends daily trending searches, from Google's public RSS feed (no key, no library).

https://trends.google.com/trending/rss?geo=AU lists what is spiking in search right now, with
approximate traffic ("2000+"). Unofficial and can change without notice, so requests retry with
backoff and a parse failure fails this node only.
"""
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

from ..entities import alias_key
from .base import Collector

FEED = "https://trends.google.com/trending/rss?geo={geo}"
HT = "{https://trends.google.com/trending/rss}"


def traffic(text: str | None) -> int:
    """'200+' -> 200, '2K+' -> 2000, '1M+' -> 1000000, '1,000+' -> 1000."""
    m = re.match(r"([\d.,]+)\s*([KkMm]?)", (text or "").strip())
    if not m:
        return 0
    n = float(m[1].replace(",", ""))
    return int(n * {"k": 1e3, "m": 1e6}.get(m[2].lower(), 1))


class GoogleTrends(Collector):
    retries = 3

    def fetch(self):
        out = []
        for geo in self.params.get("geos", ["AU", "US", "GB"]):
            body, _ = self.request(FEED.format(geo=geo), raw=True)
            for el in ET.fromstring(body).iter("item"):
                q = (el.findtext("title") or "").strip()
                if not q:
                    continue
                news = el.find(f"{HT}news_item")
                out.append(to_result(q, geo, traffic(el.findtext(f"{HT}approx_traffic")),
                                     news.findtext(f"{HT}news_item_url") if news is not None else None,
                                     news.findtext(f"{HT}news_item_title") if news is not None else None))
        if not out:
            raise RuntimeError("Google Trends feed returned no items (format may have changed)")
        return out


def to_result(query: str, geo: str, approx: int, news_url: str | None, news_title: str | None):
    item = {
        "kind": "search",
        "external_id": f"{geo}:{query.lower()}",
        "title": query,
        "url": f"https://trends.google.com/trends/explore?q={quote(query)}&geo={geo}&date=now%201-d",
        "category": f"Search {geo}",
        "alias_key": alias_key(query),
        "meta": {"geo": geo, "news_url": news_url, "news_title": news_title},
    }
    return item, {"traffic": approx}


def run(conn, node):
    return GoogleTrends(node).run(conn)
