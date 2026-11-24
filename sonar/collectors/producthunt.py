"""Product Hunt GraphQL API v2. Needs PRODUCTHUNT_TOKEN (a free developer token).

PH's `website` field is a producthunt.com/r/... redirect, so for new launches we follow it once
to learn the real domain (stored in meta), which gives the entity key site:<domain>.
"""
import os
import urllib.request
from datetime import datetime, timedelta, timezone

from .. import USER_AGENT
from ..entities import entity_key
from .base import Collector

API = "https://api.producthunt.com/v2/api/graphql"
QUERY = """query($after: DateTime!, $first: Int!) {
  posts(order: VOTES, postedAfter: $after, first: $first) {
    edges { node { id name tagline slug url website votesCount commentsCount createdAt
                   topics(first: 3) { edges { node { name } } } } }
  }
}"""


class ProductHunt(Collector):
    def fetch(self):
        token = os.environ.get("PRODUCTHUNT_TOKEN")
        if not token:
            raise RuntimeError("PRODUCTHUNT_TOKEN is not set in .env")
        p = self.params
        after = datetime.now(timezone.utc) - timedelta(days=p.get("days", 2))
        data, _ = self.request(API, {"Authorization": f"Bearer {token}", "Accept": "application/json"},
                               {"query": QUERY, "variables": {"after": after.isoformat(), "first": p.get("limit", 50)}})
        if data.get("errors"):
            raise RuntimeError(f"Product Hunt API error: {data['errors'][0].get('message')}")
        known = self.known_websites()
        budget = p.get("resolve_per_run", 20)
        results = []
        for edge in data["data"]["posts"]["edges"]:
            post = edge["node"]
            website = known.get(post["id"])
            if website is None and budget > 0 and post.get("website"):
                budget -= 1
                website = self.resolve(post["website"])
            results.append(to_result(post, website))
        return results

    def known_websites(self) -> dict:
        rows = self.conn.execute(
            "SELECT external_id, json_extract(meta_json, '$.website') AS w FROM items WHERE source = ?",
            (self.id,))
        return {r["external_id"]: r["w"] for r in rows if r["w"]}

    def resolve(self, url: str) -> str | None:
        """Follow PH's redirect to the product's real site. Failure just means no cross-linking."""
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as r:
                final = r.geturl()
            return None if "producthunt.com" in final else final
        except Exception as e:
            self.log.debug("could not resolve %s: %s", url, e)
            return None


def to_result(post: dict, website: str | None):
    topics = [e["node"]["name"] for e in post.get("topics", {}).get("edges", [])]
    item = {
        "kind": "product",
        "external_id": post["id"],
        "title": f"{post['name']}: {post.get('tagline', '')}".rstrip(": "),
        "url": post["url"],
        "category": topics[0] if topics else "product",
        "meta": {"slug": post.get("slug"), "topics": topics, "website": website,
                 "created_at": post.get("createdAt")},
    }
    if website:
        item["entity_key"] = entity_key(website)
    return item, {"votes": post.get("votesCount", 0), "comments": post.get("commentsCount", 0)}


def run(conn, node):
    return ProductHunt(node).run(conn)
