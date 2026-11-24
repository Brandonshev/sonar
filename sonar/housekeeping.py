"""python -m sonar.housekeeping: keep the database small. Run daily.

- Snapshots older than 90 days are thinned to one per item, metric and day (the last of that day).
  Heat only needs the last ~10 days at full detail; older data is kept for long-term charts.
- Scores older than 90 days are deleted (they can be recomputed from snapshots).
- VACUUM at most once a week, to give freed space back to the disk.
"""
import logging
import sys
import time

from . import db, load_env, setup_logging

log = logging.getLogger("sonar.housekeeping")
DAY = 86400


def prune(conn, now: int, keep_days: int = 90) -> int:
    cutoff = now - keep_days * DAY
    with conn:
        n = conn.execute(
            """DELETE FROM snapshots WHERE ts < ? AND rowid NOT IN (
                 SELECT MAX(rowid) FROM snapshots WHERE ts < ? GROUP BY item_id, metric, ts / 86400)""",
            (cutoff, cutoff)).rowcount
        conn.execute("DELETE FROM scores WHERE ts < ?", (cutoff,))
    return n


def vacuum_if_due(conn, now: int, every_days: int = 7) -> bool:
    last = db.last_runs(conn).get("vacuum")
    if last and now - last < every_days * DAY:
        return False
    conn.execute("VACUUM")
    with conn:
        db.record_run(conn, "vacuum", ok=True, now=now)
    return True


def main() -> int:
    load_env()
    setup_logging()
    conn = db.connect()
    now = int(time.time())
    try:
        n = prune(conn, now)
        vacuumed = vacuum_if_due(conn, now)
        with conn:
            db.record_run(conn, "housekeeping", ok=True, items=n, now=now)
        log.info("pruned %d old snapshots%s", n, ", vacuumed" if vacuumed else "")
        return 0
    except Exception as e:
        log.exception("housekeeping failed")
        with conn:
            db.record_run(conn, "housekeeping", ok=False, error=str(e), now=now)
        return 1


if __name__ == "__main__":
    sys.exit(main())
