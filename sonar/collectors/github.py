"""GitHub: new repos (created in the last N days, most stars first) plus repos already tracked.

Tracked = any github:owner/repo entity Sonar has seen on any source (so a repo first spotted
on Hacker News gets its stars tracked too). Needs GITHUB_TOKEN (5000 requests/hour);
without one GitHub allows only 60/hour, so tracking is cut short.
"""
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import quote

from .base import Collector

API = "https://api.github.com"


class GitHub(Collector):
    def headers(self):
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token := os.environ.get("GITHUB_TOKEN"):
            h["Authorization"] = f"Bearer {token}"
        return h

    def fetch(self):
        p = self.params
        since = (datetime.now(timezone.utc) - timedelta(days=p.get("created_within_days", 30))).date()
        q = quote(f"created:>{since} stars:>={p.get('min_stars', 10)}")
        found: dict[str, tuple] = {}
        for page in range(1, p.get("search_pages", 2) + 1):
            data, hdrs = self.request(
                f"{API}/search/repositories?q={q}&sort=stars&order=desc&per_page=100&page={page}",
                self.headers())
            for repo in data.get("items", []):
                found[repo["full_name"].lower()] = to_result(repo)
            if int(hdrs.get("X-RateLimit-Remaining", 1)) == 0:
                break  # search allows 30/min; next run picks up the rest

        for name in self.tracked(p.get("max_tracked", 300)):
            if name in found:
                continue
            try:
                repo, hdrs = self.request(f"{API}/repos/{name}", self.headers())
            except HTTPError as e:
                if e.code == 404:  # deleted or renamed; skip
                    continue
                if e.code in (403, 429):
                    self.log.warning("rate limited after %d repos; stopping early", len(found))
                    break
                raise
            found[name] = to_result(repo)
            if int(hdrs.get("X-RateLimit-Remaining", 100)) < p.get("reserve_requests", 50):
                self.log.warning("rate limit nearly used up; stopping tracked refresh early")
                break
            time.sleep(p.get("delay_seconds", 0.2))
        return list(found.values())

    def tracked(self, limit: int) -> list[str]:
        """Most recently seen github: entities on any source, newest first."""
        rows = self.conn.execute(
            """SELECT substr(entity_key, 8) AS name FROM items WHERE entity_key LIKE 'github:%'
               GROUP BY entity_key ORDER BY MAX(first_seen) DESC LIMIT ?""", (limit,))
        return [r["name"] for r in rows]


def to_result(repo: dict):
    item = {
        "kind": "repo",
        "external_id": repo["full_name"].lower(),
        "title": repo["full_name"],
        "url": repo["html_url"],
        "category": repo.get("language") or "unknown",
        "meta": {"description": repo.get("description"), "topics": repo.get("topics", []),
                 "created_at": repo.get("created_at"), "homepage": repo.get("homepage")},
    }
    return item, {"stars": repo["stargazers_count"], "forks": repo["forks_count"]}


def run(conn, node):
    return GitHub(node).run(conn)
