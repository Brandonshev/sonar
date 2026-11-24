"""Collector base class. Subclasses implement fetch(); storage is handled here."""
import json
import logging
import random
import time
import urllib.error
import urllib.request

from .. import USER_AGENT
from .. import db


class Collector:
    """fetch() returns [(item, metrics)] where
    item = {"kind", "external_id", "title"?, "url"?, "category"?, "meta"?}
    metrics = {"stars": 120, "comments": 4, ...}
    """

    timeout = 20
    retries = 0         # extra tries on 429/5xx, with exponential backoff (for fragile sources)
    backoff_seconds = 20

    def __init__(self, node: dict):
        self.node = node
        self.id = node["id"]
        self.params = node.get("params", {})
        self.log = logging.getLogger(f"sonar.{self.id}")

    def fetch(self) -> list[tuple[dict, dict]]:
        raise NotImplementedError

    def pace(self) -> None:
        """Strict rate limit: wait until min_interval_seconds (+ random 0..jitter_seconds) has passed
        since this collector's previous request. Both come from the node's params; default 0."""
        gap = self.params.get("min_interval_seconds", 0) + random.uniform(0, self.params.get("jitter_seconds", 0))
        if gap and getattr(self, "_last_request", None) is not None:
            time.sleep(max(0.0, self._last_request + gap - time.monotonic()))
        self._last_request = time.monotonic()

    def request(self, url: str, headers: dict | None = None, data: bytes | dict | None = None, raw: bool = False):
        """GET (or POST when data is given). Returns (parsed JSON or raw bytes, response headers).
        All HTTP goes through here so tests can swap it for recorded responses."""
        headers = {"User-Agent": USER_AGENT, **(headers or {})}
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers.setdefault("Content-Type", "application/json")
        for attempt in range(self.retries + 1):
            self.pace()
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return (r.read() if raw else json.load(r)), r.headers
            except urllib.error.HTTPError as e:
                if attempt == self.retries or not (e.code == 429 or e.code >= 500):
                    raise
                wait = self.backoff_seconds * 2 ** attempt
                self.log.warning("HTTP %d from %s, retrying in %ds", e.code, url.split("?")[0], wait)
                time.sleep(wait)

    def get_json(self, url: str, headers: dict | None = None):
        return self.request(url, headers)[0]

    def run(self, conn) -> int:
        """Fetch, then store all items and snapshots in one transaction. Returns item count."""
        self.conn = conn  # lets fetch() look up already-tracked items
        results = self.fetch()
        now = int(time.time())
        with conn:
            for item, metrics in results:
                item_id = db.upsert_item(conn, self.id, item, now)
                db.add_snapshots(conn, item_id, metrics, now)
        self.log.info("stored %d items", len(results))
        return len(results)
