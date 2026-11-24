"""Auto-discovered watchlists: hashtags found on one platform get watched on others (capped).

Seeds live in pipeline.json (node params); this table only holds what Sonar discovered, so it can
expire old discoveries without ever touching your config.
"""


def values(conn, platform: str, seeds=()) -> list[str]:
    """Seeds first, then discoveries newest first, no duplicates."""
    found = [r[0] for r in conn.execute("SELECT value FROM watchlist WHERE platform=? ORDER BY added DESC", (platform,))]
    return list(dict.fromkeys([*seeds, *found]))


def add(conn, platform: str, value: str, origin: str, now: int, cap: int) -> bool:
    """Add a discovery; when the platform is at its cap, the oldest discovery makes room. True if new."""
    if conn.execute("SELECT 1 FROM watchlist WHERE platform=? AND value=?", (platform, value)).fetchone():
        return False
    if cap <= 0:
        return False
    n = conn.execute("SELECT COUNT(*) FROM watchlist WHERE platform=?", (platform,)).fetchone()[0]
    if n >= cap:
        conn.execute("""DELETE FROM watchlist WHERE rowid IN (SELECT rowid FROM watchlist WHERE platform=?
                        ORDER BY added LIMIT ?)""", (platform, n - cap + 1))
    conn.execute("INSERT INTO watchlist (platform, value, origin, added) VALUES (?, ?, ?, ?)",
                 (platform, value, origin, now))
    return True


def expire(conn, platform: str, now: int, ttl_days: float) -> None:
    conn.execute("DELETE FROM watchlist WHERE platform=? AND added < ?", (platform, now - ttl_days * 86400))
