"""The company list (companies.json) and mention matching.

Aliases match as whole words and are case-sensitive: 'Apple' matches "Apple unveils..." but not
"apple pie"; ambiguous names ('Meta', 'Arm', 'Notion', 'Block') are listed only in specific forms
("Meta Platforms", "Arm Holdings"...). A company's key is wiki:<article> when it has a Wikipedia
article, so its news counts, Wikipedia views and score all show on one item page.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

from . import ROOT


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@lru_cache(maxsize=4)
def load(path: str | Path = ROOT / "companies.json") -> tuple[dict, ...]:
    data = json.loads(Path(path).read_text())
    out = []
    for c in data["companies"]:
        if not c.get("name") or not c.get("aliases"):
            raise ValueError(f"company needs name and aliases: {c!r}")
        alts = "|".join(re.escape(a) for a in sorted(c["aliases"], key=len, reverse=True))
        out.append({**c, "slug": slug(c["name"]),
                    "key": f"wiki:{c['wikipedia']}" if c.get("wikipedia") else f"company:{slug(c['name'])}",
                    "pattern": re.compile(rf"(?<![\w.-])(?:{alts})(?![\w-])")})
    return tuple(out)


def mentioned(text: str, companies) -> set[str]:
    """Slugs of the companies named in text."""
    return {c["slug"] for c in companies if c["pattern"].search(text or "")}
