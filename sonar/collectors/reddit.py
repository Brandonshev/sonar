"""Reddit rising + hot posts for the subreddits listed in pipeline.json.

With REDDIT_CLIENT_ID/SECRET set it uses an app-only OAuth token (100 requests/min, and works
from datacenter IPs). Without them it uses the public .json pages, which Reddit limits to about
10 requests/min and often blocks from cloud servers like Oracle, so it waits between requests.
User-Agent follows Reddit's rule: <platform>:<app id>:<version> (by /u/<username>).
"""
import base64
import os
import time
from urllib.parse import urlencode

from .. import VERSION
from .base import Collector

REDDIT_HOSTS = ("reddit.com", "redd.it")


class Reddit(Collector):
    def fetch(self):
        p = self.params
        user = os.environ.get("REDDIT_USERNAME", "sonar-bot")
        self.ua = {"User-Agent": f"python:sonar:{VERSION} (by /u/{user})"}
        token = self.token()
        base = "https://oauth.reddit.com" if token else "https://www.reddit.com"
        headers = {**self.ua, **({"Authorization": f"Bearer {token}"} if token else {})}
        delay = p.get("delay_seconds", 1 if token else 7)

        found: dict[str, tuple] = {}
        first = True
        for sub in p.get("subreddits", []):
            for listing in p.get("listings", ["rising", "hot"]):
                if not first:
                    time.sleep(delay)
                first = False
                data, hdrs = self.request(f"{base}/r/{sub}/{listing}.json?limit={p.get('limit', 50)}&raw_json=1",
                                          headers)
                for child in data["data"]["children"]:
                    post = child["data"]
                    if not post.get("stickied"):
                        found[post["name"]] = to_result(post)
                if float(hdrs.get("x-ratelimit-remaining", 10)) < 2:
                    wait = min(float(hdrs.get("x-ratelimit-reset", 60)), 120)
                    self.log.info("rate limit low, waiting %.0fs", wait)
                    time.sleep(wait)
        return list(found.values())

    def token(self) -> str | None:
        cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
        if not (cid and secret):
            return None
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        data, _ = self.request("https://www.reddit.com/api/v1/access_token",
                               {**self.ua, "Authorization": f"Basic {auth}",
                                "Content-Type": "application/x-www-form-urlencoded"},
                               urlencode({"grant_type": "client_credentials"}).encode())
        return data["access_token"]


def to_result(post: dict):
    permalink = "https://www.reddit.com" + post["permalink"]
    link = post.get("url_overridden_by_dest") or post.get("url")
    external = link and not any(h in link for h in REDDIT_HOSTS)
    item = {
        "kind": "post",
        "external_id": post["name"],
        "title": post.get("title"),
        "url": link if external else permalink,
        "category": post.get("subreddit"),
        "meta": {"permalink": permalink, "created": post.get("created_utc"), "nsfw": post.get("over_18", False)},
    }
    if not external:
        item["entity_key"] = f"reddit:{post['name']}"  # self/image post; links to nothing else
    return item, {"score": post.get("score", 0), "comments": post.get("num_comments", 0)}


def run(conn, node):
    return Reddit(node).run(conn)
