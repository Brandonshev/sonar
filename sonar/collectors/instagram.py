"""Instagram hashtag post counts via Instaloader, using a THROWAWAY account's saved session.

Limits (be realistic):
  - Instagram shows hashtag data only to logged-in users. Checked live: without a login the
    endpoint returns "Page Not Found".
  - Automated access breaks Instagram's terms. The account can be challenged, rate-limited or banned,
    and cloud IPs like Oracle's are flagged faster. Use a throwaway account only: never your own
    account and never the Lookout account.
  - Only post counts per hashtag are available reliably; no view counts.
So it runs rarely (every 8h), paces each request with jitter, and is switched off automatically after
max_failures in a row.

Setup (once, on the server): log in with Instaloader so only a session file is stored, never a password:
    sudo -u sonar /opt/sonar/.venv/bin/instaloader --login THROWAWAY_USER --sessionfile /opt/sonar/data/ig.session
and set IG_USERNAME=THROWAWAY_USER in .env.
"""
import os

from .. import ROOT, watchlist
from .base import Collector
from .tiktok import norm_tag


class Instagram(Collector):
    def fetch(self):
        try:
            import instaloader
        except ImportError:
            raise RuntimeError("instaloader is not installed (waiting for approval, see README)") from None
        user = os.environ.get("IG_USERNAME")
        if not user:
            raise RuntimeError("IG_USERNAME is not set in .env (use a throwaway account)")
        session = self.params.get("session_file") or str(ROOT / "data" / "ig.session")
        L = instaloader.Instaloader(quiet=True, max_connection_attempts=1, request_timeout=20)
        try:
            L.load_session_from_file(user, session)
        except FileNotFoundError:
            raise RuntimeError(f"no Instagram session at {session}; run instaloader --login (see instagram.py)") from None

        p = self.params
        tags = watchlist.values(self.conn, "instagram", [norm_tag(t) for t in p.get("hashtags", [])])
        out = []
        for tag in tags[:p.get("max_per_run", 25)]:
            self.pace()
            try:
                h = instaloader.Hashtag.from_name(L.context, tag)
                out.append(to_result(tag, h.mediacount))
            except instaloader.exceptions.QueryReturnedNotFoundException:
                continue
            except (instaloader.exceptions.LoginRequiredException, instaloader.exceptions.ConnectionException) as e:
                if not out:
                    raise RuntimeError(f"Instagram refused: {e}") from None
                self.log.warning("stopping early after %d tags: %s", len(out), e)
                break
        return out


def to_result(tag: str, posts: int):
    item = {
        "kind": "hashtag",
        "external_id": tag,
        "title": f"#{tag}",
        "url": f"https://www.instagram.com/explore/tags/{tag}/",
        "category": "social.instagram",
        "entity_key": f"instagram:{tag}",
        "alias_key": f"tag:{tag}",
        "meta": {},
    }
    return item, {"posts": int(posts)}


def run(conn, node):
    return Instagram(node).run(conn)
