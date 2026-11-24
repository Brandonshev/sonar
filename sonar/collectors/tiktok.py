"""TikTok hashtags: post count and total views, from TikTok's public web API (no login, no library).

What this can and can't do (checked live, Nov 2026):
  - YES: stats for a hashtag you name (videoCount, viewCount), from a home or cloud IP.
  - NO:  TikTok's trending lists (Creative Center) and sound usage counts. Both need signed requests
         that only a real browser produces; TikTokApi does that with Playwright, which needs
         300-500 MB of RAM and doesn't fit this server.
So the watchlist is: seeds from pipeline.json, plus hashtags discovered in YouTube titles (Shorts
creators tag cross-posts), plus whatever else adds to the "tiktok" watchlist. Climbing TikTok tags
are then pushed to the Instagram and YouTube watchlists (capped) so cross-platform climbs score.

Unofficial and against TikTok's terms for automated access: strict pacing with jitter, and the
runner switches it off after max_failures in a row.
"""
import re
import time
from urllib.parse import quote

from .. import watchlist
from .base import Collector

API = "https://www.tiktok.com/api/challenge/detail/?aid=1988&challengeName={tag}"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0 Safari/537.36")
HASHTAG = re.compile(r"#([A-Za-z][A-Za-z0-9_]{2,29})")
# Tags on nearly everything; they tell you nothing.
GENERIC_TAGS = {"shorts", "short", "youtube", "youtubeshorts", "viral", "trending", "trend", "fyp", "foryou",
                "foryoupage", "subscribe", "tiktok", "reels", "reel", "music", "funny", "explore", "video",
                "love", "new", "live", "official", "shortsfeed", "ytshorts", "comedy", "memes", "meme"}


def norm_tag(tag: str) -> str:
    return tag.lstrip("#").lower()


class TikTok(Collector):
    def fetch(self):
        p, now = self.params, int(time.time())
        with self.conn:
            watchlist.expire(self.conn, "tiktok", now, p.get("discovery_ttl_days", 14))
            self.discover_from_youtube(now)
        tags = watchlist.values(self.conn, "tiktok", [norm_tag(t) for t in p.get("hashtags", [])])
        tags = tags[:p.get("max_per_run", 40)]
        out, failed = [], 0
        for tag in tags:
            try:
                data = self.get_json(API.format(tag=quote(tag)), {"User-Agent": BROWSER_UA, "Accept-Language": "en-AU,en"})
            except Exception as e:
                failed += 1
                self.log.warning("#%s failed: %s", tag, e)
                if failed >= 3 and not out:
                    break  # probably blocked; stop early rather than hammering
                continue
            info = data.get("challengeInfo")
            if not info:  # statusCode 10205 = no such hashtag
                continue
            stats = info.get("statsV2") or info.get("stats") or {}
            out.append(to_result(info["challenge"], stats))
        if tags and not out:
            raise RuntimeError(f"TikTok returned nothing for {len(tags)} hashtags ({failed} errors): likely blocked")
        with self.conn:
            self.push_climbers(now)
        return out

    def discover_from_youtube(self, now: int) -> None:
        """Hashtags that keep turning up in recent YouTube titles join the TikTok watchlist."""
        p = self.params
        counts: dict[str, int] = {}
        for (title,) in self.conn.execute(
                "SELECT title FROM items WHERE source='youtube' AND first_seen >= ?", (now - 2 * 86400,)):
            for tag in {norm_tag(t) for t in HASHTAG.findall(title or "")}:
                if tag not in GENERIC_TAGS:
                    counts[tag] = counts.get(tag, 0) + 1
        best = sorted((t for t, n in counts.items() if n >= p.get("discover_min_mentions", 2)),
                      key=lambda t: -counts[t])[:p.get("discover_per_run", 5)]
        for tag in best:
            watchlist.add(self.conn, "tiktok", tag, "youtube", now, p.get("discover_cap", 40))

    def push_climbers(self, now: int) -> None:
        """TikTok tags with heat >= push_min_heat go on the Instagram and YouTube watchlists."""
        p = self.params
        rows = self.conn.execute(
            """SELECT i.external_id FROM scores s JOIN items i ON i.id = s.item_id
               WHERE i.source = ? AND s.ts = (SELECT MAX(ts) FROM scores) AND s.heat >= ?
               ORDER BY s.heat DESC LIMIT 10""", (self.id, p.get("push_min_heat", 3)))
        for (tag,) in rows:
            watchlist.add(self.conn, "instagram", tag, "tiktok", now, p.get("push_cap_instagram", 15))
            watchlist.add(self.conn, "youtube", f"#{tag}", "tiktok", now, p.get("push_cap_youtube", 4))
        watchlist.expire(self.conn, "instagram", now, p.get("discovery_ttl_days", 14))
        watchlist.expire(self.conn, "youtube", now, p.get("discovery_ttl_days", 14))


def to_result(ch: dict, stats: dict):
    tag = norm_tag(ch.get("title") or "")
    item = {
        "kind": "hashtag",
        "external_id": tag,
        "title": f"#{tag}",
        "url": f"https://www.tiktok.com/tag/{quote(tag)}",
        "category": "social.video",  # video format trends; Lookout reads this category
        "entity_key": f"tiktok:{tag}",
        "alias_key": f"tag:{tag}",   # links the same hashtag on Instagram and YouTube searches
        "meta": {"id": ch.get("id"), "desc": (ch.get("desc") or "")[:300]},
    }
    return item, {"videos": int(stats.get("videoCount", 0)), "views": int(stats.get("viewCount", 0))}


def run(conn, node):
    return TikTok(node).run(conn)
