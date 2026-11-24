"""YouTube Data API v3. Needs YOUTUBE_API_KEY (free, 10,000 quota units a day, reset at midnight Pacific).

Each run: most-popular charts for each region (1 unit each), view-count re-checks on videos seen in
the last `track_days` (1 unit per 50 videos), and, a few times a day, searches for recent popular
videos in each tracked topic (search costs 100 units, so it is rationed by `search_rounds_per_day`).
Every call is counted in the api_usage table and nothing is called past `daily_budget`.
"""
import os
import time
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .. import watchlist
from .base import Collector

API = "https://www.googleapis.com/youtube/v3"
COST = {"videos": 1, "search": 100}
PACIFIC = ZoneInfo("America/Los_Angeles")


class QuotaSpent(Exception):
    pass


class YouTube(Collector):
    def day(self) -> str:
        return datetime.now(PACIFIC).strftime("%Y-%m-%d")

    def used(self, bucket: str | None = None) -> int:
        row = self.conn.execute("SELECT units FROM api_usage WHERE source=? AND day=?",
                                (bucket or self.id, self.day())).fetchone()
        return row[0] if row else 0

    def count(self, units: int, bucket: str | None = None):
        with self.conn:
            self.conn.execute(
                """INSERT INTO api_usage (source, day, units) VALUES (?, ?, ?)
                   ON CONFLICT (source, day) DO UPDATE SET units = units + excluded.units""",
                (bucket or self.id, self.day(), units))

    def call(self, endpoint: str, **params):
        cost = COST[endpoint]
        if self.used() + cost > self.params.get("daily_budget", 8000):
            raise QuotaSpent
        data = self.get_json(f"{API}/{endpoint}?{urlencode({**params, 'key': self.key})}")
        self.count(cost)
        return data

    def fetch(self):
        self.key = os.environ.get("YOUTUBE_API_KEY")
        if not self.key:
            raise RuntimeError("YOUTUBE_API_KEY is not set in .env")
        p = self.params
        found: dict[str, dict] = {}   # video id -> video resource
        tags: dict[str, set] = {}     # video id -> {"chart:AU", "topic:ai", ...}
        try:
            for region in p.get("regions", ["AU", "US", "GB"]):
                data = self.call("videos", part="snippet,statistics", chart="mostPopular",
                                 regionCode=region, maxResults=50)
                for v in data.get("items", []):
                    found[v["id"]] = v
                    tags.setdefault(v["id"], set()).add(f"chart:{region}")

            for vid, t in self.search().items():
                tags.setdefault(vid, set()).update(t)
            tracked = self.tracked(p.get("track_days", 7), p.get("max_tracked", 400))
            want = [vid for vid in dict.fromkeys([*tags, *tracked]) if vid not in found]
            for i in range(0, len(want), 50):  # stats for searched + tracked videos, 50 per unit
                data = self.call("videos", part="snippet,statistics", id=",".join(want[i:i + 50]), maxResults=50)
                for v in data.get("items", []):
                    found[v["id"]] = v
        except QuotaSpent:
            self.log.warning("daily YouTube budget reached (%d units); keeping what was fetched", self.used())
        self.log.info("YouTube quota used today: %d units", self.used())
        return [to_result(v, tags.get(vid, set())) for vid, v in found.items()]

    def search(self) -> dict[str, set]:
        """Topic searches, at most search_rounds_per_day rounds a day, spread across the day.
        Returns video id -> {"topic:..."}."""
        p = self.params
        out: dict[str, set] = {}
        topics = watchlist.values(self.conn, "youtube", p.get("topics", []))  # + hashtags pushed by TikTok
        rounds = p.get("search_rounds_per_day", 4)
        if not topics or not rounds:
            return {}
        # The day is cut into `rounds` slots (4 = every 6h); one search round per slot, never a catch-up burst.
        slot = f"{self.id}.search_slot{datetime.now(PACIFIC).hour * rounds // 24}"
        if self.used(slot):
            return {}
        after = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - p.get("search_days", 2) * 86400))
        self.count(1, slot)  # counted up front so a quota stop can't cause a retry loop
        for topic in topics:
            data = self.call("search", part="id", q=topic, type="video", order="viewCount",
                             publishedAfter=after, maxResults=p.get("search_results", 25))
            for it in data.get("items", []):
                vid = it["id"].get("videoId")
                if vid:
                    out.setdefault(vid, set()).add(f"topic:{topic}")
        return out

    def tracked(self, days: int, limit: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT external_id FROM items WHERE source=? AND first_seen>=? ORDER BY first_seen DESC LIMIT ?",
            (self.id, int(time.time()) - days * 86400, limit))
        return [r[0] for r in rows]


def to_result(v: dict, tags: set):
    s, st = v.get("snippet", {}), v.get("statistics", {})
    hashtag = next((t.removeprefix("topic:#") for t in sorted(tags) if t.startswith("topic:#")), None)
    item = {
        "alias_key": f"tag:{hashtag.lower()}" if hashtag else None,  # found via a TikTok hashtag: link them
        "kind": "video",
        "external_id": v["id"],
        "title": s.get("title"),
        "url": f"https://www.youtube.com/watch?v={v['id']}",
        "category": "social.video",  # video trends; Lookout reads this category
        "meta": {"channel": s.get("channelTitle"), "channel_id": s.get("channelId"), "published": s.get("publishedAt"), "seen_in": sorted(tags)},
    }
    return item, {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                  "comments": int(st.get("commentCount", 0))}


def run(conn, node):
    return YouTube(node).run(conn)
