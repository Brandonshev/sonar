"""Sonar: a trend radar that spots fast-climbing things early."""
import logging
import os
from pathlib import Path

VERSION = "0.1.0"
USER_AGENT = f"Sonar/{VERSION}"
ROOT = Path(__file__).resolve().parent.parent


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=level or os.environ.get("SONAR_LOG_LEVEL", "INFO"),
        format=f"%(asctime)s sonar v{VERSION} %(levelname)s %(name)s: %(message)s",
    )


def load_env(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader (KEY=value lines); real env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))
