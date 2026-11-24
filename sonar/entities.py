"""URL normalisation and entity keys, so the same thing seen on several sources links up.

  github.com/Owner/Repo/issues/3 -> github:owner/repo
  https://www.example.com/        -> site:example.com      (a homepage = a product)
  https://example.com/blog/post   -> url:example.com/blog/post   (an article: exact page only)
"""
from urllib.parse import parse_qsl, urlencode, urlsplit

TRACKING = ("utm_", "ref", "ref_src", "fbclid", "gclid", "source", "via", "mc_cid", "mc_eid")
GITHUB_NOT_REPOS = {"orgs", "topics", "features", "marketplace", "sponsors", "settings", "explore",
                    "trending", "collections", "apps", "about", "pricing", "login", "search", "enterprise"}


def normalize_url(url: str | None) -> str | None:
    """host/path?query, lower-cased host, no scheme, www, fragment, trailing slash or tracking params."""
    if not url:
        return None
    parts = urlsplit(url.strip() if "://" in url else "https://" + url.strip())
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m."):
        host = host.removeprefix(prefix)
    if not host or "." not in host:
        return None
    query = sorted((k, v) for k, v in parse_qsl(parts.query)
                   if not k.lower().startswith(TRACKING))
    out = host + parts.path.rstrip("/")
    return out + ("?" + urlencode(query) if query else "")


def entity_key(url: str | None) -> str | None:
    norm = normalize_url(url)
    if not norm:
        return None
    host, _, rest = norm.partition("/")
    path = rest.split("?")[0]
    if host == "github.com":
        bits = [b for b in path.split("/") if b]
        if len(bits) >= 2 and bits[0].lower() not in GITHUB_NOT_REPOS:
            return f"github:{bits[0].lower()}/{bits[1].lower().removesuffix('.git')}"
        return None
    if not path and "?" not in norm:
        return f"site:{host}"
    return f"url:{norm}"
