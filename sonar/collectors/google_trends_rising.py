"""Google Trends rising related queries for seed topics ("what's rising around 'AI app'?").

There is no official API, so this talks to the same internal endpoints the pytrends family uses:
explore (to get a widget token), then widgetdata/relatedsearches. Google rate-limits these hard,
especially from cloud servers, so: one cookie warm-up, a pause between topics, retries with backoff,
and any failure only marks this node unhealthy.
"""
import http.cookiejar
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

from .. import USER_AGENT
from ..entities import alias_key
from .base import Collector
from .google_trends import quote

BASE = "https://trends.google.com/trends/api"


def strip_prefix(body: bytes):
    """Google prefixes these JSON responses with junk like )]}' to stop JSON hijacking."""
    text = body.decode()
    return json.loads(text[text.index("{"):])


class GoogleTrendsRising(Collector):
    retries = 3
    backoff_seconds = 60

    def request(self, url, headers=None, data=None, raw=False):
        # Same retry logic as the base class, but through a cookie-keeping opener.
        if not hasattr(self, "opener"):
            self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            self.opener.addheaders = [("User-Agent", USER_AGENT), ("Accept-Language", "en-AU,en")]
        for attempt in range(self.retries + 1):
            try:
                with self.opener.open(url, timeout=self.timeout) as r:
                    return r.read(), r.headers
            except urllib.error.HTTPError as e:
                if attempt == self.retries or not (e.code == 429 or e.code >= 500):
                    raise
                time.sleep(self.backoff_seconds * 2 ** attempt)

    def fetch(self):
        p = self.params
        geo, tz = p.get("geo", "AU"), p.get("tz_offset_minutes", -600)
        self.request(f"https://trends.google.com/trends/?geo={geo}")  # sets the NID cookie Google expects
        out = []
        for i, kw in enumerate(p.get("topics", [])):
            if i:
                time.sleep(p.get("pause_seconds", 5))
            req = {"comparisonItem": [{"keyword": kw, "geo": geo, "time": p.get("timeframe", "now 7-d")}],
                   "category": 0, "property": ""}
            body, _ = self.request(f"{BASE}/explore?{urlencode({'hl': 'en-AU', 'tz': tz, 'req': json.dumps(req)})}")
            widget = next((w for w in strip_prefix(body)["widgets"] if w["id"] == "RELATED_QUERIES"), None)
            if not widget:
                continue
            body, _ = self.request(f"{BASE}/widgetdata/relatedsearches?" + urlencode(
                {"hl": "en-AU", "tz": tz, "req": json.dumps(widget["request"]), "token": widget["token"]}))
            ranked = strip_prefix(body)["default"]["rankedList"]
            rising = ranked[1]["rankedKeyword"] if len(ranked) > 1 else []
            out += [to_result(r, kw, geo) for r in rising[:p.get("per_topic", 10)]]
        return out


def to_result(r: dict, topic: str, geo: str):
    q = r["query"]
    breakout = r.get("formattedValue") == "Breakout"
    item = {
        "kind": "rising_query",
        "external_id": f"{geo}:{topic.lower()}:{q.lower()}",
        "title": q,
        "url": f"https://trends.google.com/trends/explore?q={quote(q)}&geo={geo}",
        "category": f"Rising: {topic}",
        "alias_key": alias_key(q),
        "meta": {"topic": topic, "geo": geo, "breakout": breakout},
    }
    # "Breakout" means growth over 5000%; Google reports it as a number too, but cap it so it compares sanely.
    return item, {"rise_pct": min(int(r.get("value", 0)), 5000)}


def run(conn, node):
    return GoogleTrendsRising(node).run(conn)
