"""Collector base class. Subclasses implement fetch(); storage is handled here."""
import json
import logging
import time
import urllib.request

from .. import USER_AGENT
from .. import db


class Collector:
    """fetch() returns [(item, metrics)] where
    item = {"kind", "external_id", "title"?, "url"?, "category"?, "meta"?}
    metrics = {"stars": 120, "comments": 4, ...}
    """

    timeout = 20

    def __init__(self, node: dict):
        self.node = node
        self.id = node["id"]
        self.params = node.get("params", {})
        self.log = logging.getLogger(f"sonar.{self.id}")

    def fetch(self) -> list[tuple[dict, dict]]:
        raise NotImplementedError

    def get_json(self, url: str, headers: dict | None = None):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def run(self, conn) -> int:
        """Fetch, then store all items and snapshots in one transaction. Returns item count."""
        results = self.fetch()
        now = int(time.time())
        with conn:
            for item, metrics in results:
                item_id = db.upsert_item(conn, self.id, item, now)
                db.add_snapshots(conn, item_id, metrics, now)
        self.log.info("stored %d items", len(results))
        return len(results)
