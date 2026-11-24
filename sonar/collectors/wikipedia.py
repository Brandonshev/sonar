"""Wikipedia pageviews (Wikimedia REST API: free, official, no key). A strong early-attention signal.

Tracks:
  - the daily top-viewed English articles
  - articles for things Sonar already knows: Google Trends searches, Product Hunt products and
    GitHub repos that are climbing. A name only links to an article when the title matches exactly
    (after Wikipedia's own redirects), it is not a disambiguation page, and (for products and repos)
    the article's short description sounds like software, a product or a company.

Pageviews are daily counts and arrive a day late, so each article is stored as a running total with
one back-dated snapshot per day (ts = end of that UTC day). That way heat's "growth in 24h vs normal
daily growth" maths works unchanged, and history can be backfilled for new articles.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import quote, urlencode

from .. import db
from ..entities import alias_key, words
from .base import Collector

PV = "https://wikimedia.org/api/rest_v1/metrics/pageviews"
WIKI_API = "https://en.wikipedia.org/w/api.php"
DAY = 86400
SKIP_PREFIXES = ("Main_Page", "Special:", "Wikipedia:", "Portal:", "File:", "Help:", "Talk:", "User:",
                 "Template:", "Category:", "Draft:", "-")
SOFTWARE_WORDS = ("software", "app", "application", "company", "startup", "service", "platform", "website",
                  "programming", "framework", "library", "open-source", "open source", "language model",
                  "artificial intelligence", "video game", "browser", "operating system", "engine", "editor",
                  "brand", "product", "manufacturer", "technology", "cryptocurrency", "social network")


def day_end(d: datetime) -> int:
    return int(d.replace(tzinfo=timezone.utc).timestamp()) + DAY


class Wikipedia(Collector):
    retries = 2

    def fetch(self):
        raise NotImplementedError  # run() is overridden: this source writes back-dated snapshots

    def run(self, conn) -> int:
        self.conn = conn
        p = self.params
        now = int(time.time())
        top = self.top_articles(p.get("top", 100))
        linked = self.link_known_names(now, p.get("lookups_per_run", 25))
        tracked = self.tracked(now, p.get("track_days", 14), p.get("max_tracked", 300))

        articles: dict[str, dict] = {}
        for title in tracked:
            articles[title] = {}
        for title in top:
            articles.setdefault(title, {})
        for title, keys in linked.items():
            articles[title] = keys

        starts = {t: self.since(t, now, p.get("backfill_days", 10)) for t in articles}  # DB reads stay on this thread

        def series(title):
            try:
                return title, self.daily_views(title, starts[title], now)
            except HTTPError as e:
                if e.code == 404:  # no views recorded (new or renamed article)
                    return title, []
                raise

        with ThreadPoolExecutor(p.get("workers", 4)) as pool:
            results = list(pool.map(series, articles))

        stored = 0
        with conn:
            for title, days in results:
                if not days:
                    continue
                keys = articles[title]
                old = self.existing(title)
                item = {"kind": "article", "external_id": title, "title": title.replace("_", " "),
                        "url": f"https://en.wikipedia.org/wiki/{quote(title)}", "category": "Wikipedia",
                        "entity_key": keys.get("entity_key") or (old and old["entity_key"]) or f"wiki:{title}",
                        "alias_key": keys.get("alias_key") or (old and old["alias_key"]) or alias_key(title.replace("_", " "), min_words=2)}
                item_id = db.upsert_item(conn, self.id, item, now=days[0][0])
                total = self.last_total(item_id)
                for ts, views in days:
                    total += views
                    db.add_snapshots(conn, item_id, {"views": total}, now=ts)
                stored += 1
        self.log.info("stored %d articles (%d top, %d newly linked)", stored, len(top), len(linked))
        return stored

    # --- what to track ---

    def top_articles(self, n: int) -> list[str]:
        for back in (1, 2):  # yesterday's list may not be published yet
            d = datetime.now(timezone.utc) - timedelta(days=back)
            try:
                data = self.get_json(f"{PV}/top/en.wikipedia/all-access/{d:%Y/%m/%d}")
            except HTTPError as e:
                if e.code == 404:
                    continue
                raise
            arts = data["items"][0]["articles"]
            return [a["article"] for a in arts if not a["article"].startswith(SKIP_PREFIXES)][:n]
        return []

    def tracked(self, now: int, days: int, limit: int) -> list[str]:
        """Articles we already follow that are still recent or linked to another source."""
        rows = self.conn.execute(
            """SELECT external_id FROM items WHERE source = ? AND (first_seen >= ? OR entity_key NOT LIKE 'wiki:%')
               ORDER BY first_seen DESC LIMIT ?""", (self.id, now - (days + 10) * DAY, limit))
        return [r[0] for r in rows]

    def link_known_names(self, now: int, budget: int) -> dict[str, dict]:
        """Look up Wikipedia articles for names on other sources. Returns title -> keys to share."""
        rows = self.conn.execute(
            """SELECT i.source, i.alias_key, i.entity_key, i.title, MAX(s.score) AS score FROM items i
               LEFT JOIN scores s ON s.item_id = i.id AND s.ts >= ?
               WHERE i.alias_key IS NOT NULL AND i.source IN ('google_trends', 'google_trends_rising', 'producthunt', 'github')
                 AND i.first_seen >= ? AND i.alias_key NOT IN (SELECT key FROM lookups)
               GROUP BY i.alias_key ORDER BY score DESC NULLS LAST LIMIT ?""",
            (now - DAY, now - 3 * DAY, budget * 4)).fetchall()
        out = {}
        for r in rows:
            if budget <= 0:
                break
            is_search = r["source"].startswith("google_trends")
            if not is_search and (r["score"] or 0) < self.params.get("lookup_min_score", 20):
                continue  # only spend lookups on products/repos that are actually climbing
            budget -= 1
            name = r["title"].split(":")[0] if r["source"] == "producthunt" else \
                r["title"].split("/")[-1] if r["source"] == "github" else r["title"]
            title = self.match(name, search=is_search)
            with self.conn:
                self.conn.execute("INSERT OR REPLACE INTO lookups (key, result, ts) VALUES (?, ?, ?)",
                                  (r["alias_key"], title, now))
            if title:
                out[title] = {"alias_key": r["alias_key"], "entity_key": None if is_search else r["entity_key"]}
        return out

    def match(self, name: str, search: bool) -> str | None:
        """The article for this name, or None when unsure. Rules that avoid false links:
          - never a missing or disambiguation page
          - searches: at least two words, and every word must still be in the title after
            Wikipedia's redirects ('storm watch' -> 'Storm warning' is refused)
          - products/repos: the article's short description must sound like software or a company
        Wikipedia titles are case-sensitive, so 'rob lowe' is also tried as 'Rob Lowe'."""
        if search and len(words(name)) < 2:
            return None
        for candidate in dict.fromkeys([name, name.title()]):
            data = self.get_json(f"{WIKI_API}?" + urlencode({
                "action": "query", "format": "json", "formatversion": 2, "redirects": 1,
                "prop": "pageprops|description", "titles": candidate}))
            pages = data.get("query", {}).get("pages", [])
            if not pages or pages[0].get("missing"):
                continue
            page = pages[0]
            if "disambiguation" in page.get("pageprops", {}):
                return None
            if search and not set(words(name)) <= set(words(page["title"])):
                return None
            if not search and not any(w in (page.get("description") or "").lower() for w in SOFTWARE_WORDS):
                return None
            return page["title"].replace(" ", "_")
        return None

    # --- pageviews ---

    def existing(self, title: str):
        return self.conn.execute("SELECT * FROM items WHERE source=? AND external_id=?", (self.id, title)).fetchone()

    def last_total(self, item_id: int) -> float:
        row = self.conn.execute("SELECT value FROM snapshots WHERE item_id=? ORDER BY ts DESC LIMIT 1", (item_id,)).fetchone()
        return row[0] if row else 0.0

    def since(self, title: str, now: int, backfill_days: int) -> datetime:
        """First day to fetch: the day after the last stored snapshot, or backfill_days ago."""
        row = self.conn.execute(
            """SELECT MAX(s.ts) FROM snapshots s JOIN items i ON i.id = s.item_id
               WHERE i.source = ? AND i.external_id = ?""", (self.id, title)).fetchone()
        # A stored snapshot's ts is the end of its day = midnight starting the next, unstored day.
        start = row[0] if row and row[0] else now - backfill_days * DAY
        return datetime.fromtimestamp(start, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

    def daily_views(self, title: str, start: datetime, now: int) -> list[tuple[int, int]]:
        """[(end-of-day ts, views)] for complete days from start up to yesterday."""
        end = datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        if start.date() > end.date():
            return []
        data = self.get_json(f"{PV}/per-article/en.wikipedia/all-access/user/{quote(title, safe='')}"
                             f"/daily/{start:%Y%m%d}00/{end:%Y%m%d}00")
        return [(day_end(datetime.strptime(it["timestamp"][:8], "%Y%m%d")), it["views"]) for it in data.get("items", [])]


def run(conn, node):
    return Wikipedia(node).run(conn)
