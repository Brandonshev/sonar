"""python -m sonar.health: one line per pipeline node with its status."""
import sys
import time

from . import VERSION, db, load_env, pipeline


def ago(ts: int | None, now: int) -> str:
    if not ts:
        return "never"
    s = now - ts
    return f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago" if s < 172800 else f"{s // 86400}d ago"


def status(node: dict, h, now: int) -> str:
    if not node["enabled"]:
        return "OFF"
    if h and h["disabled_at"]:
        return "DISABLED"  # switched off automatically after repeated failures
    if not h or not h["last_run"]:
        return "WAITING"
    if h["last_error"] and (not h["last_ok"] or h["last_error"] > h["last_ok"]):
        return "FAILING"
    if "every" in node and now - h["last_run"] > 3 * pipeline.every_seconds(node["every"]):
        return "STALE"  # the timer may have stopped
    return "OK"


def rows(conn, data: dict, now: int) -> list[dict]:
    """One dict per pipeline node plus the housekeeping jobs: id, label, status, last_ok, items, error."""
    health = {r["source"]: r for r in conn.execute("SELECT * FROM source_health")}
    jobs = [{"id": j, "label": j.title(), "enabled": True, "every": "2d" if j == "housekeeping" else "8d"}
            for j in ("housekeeping", "vacuum")]
    out = []
    for node in data["nodes"] + jobs:
        h = health.get(node["id"])
        out.append({
            "id": node["id"], "label": node.get("label", node["id"]), "status": status(node, h, now),
            "last_ok": ago(h and h["last_ok"], now),
            "items": h["items_last_run"] if h and h["items_last_run"] is not None else "-",
            "error": f"{ago(h['last_error'], now)}: {h['last_error_msg']}" if h and h["last_error"] else "",
        })
    return out


def report(conn, data: dict, now: int | None = None) -> str:
    now = now or int(time.time())
    lines = [f"Sonar v{VERSION} health", f"{'node':<12} {'status':<8} {'last ok':<10} {'items':>6}  last error"]
    for r in rows(conn, data, now):
        lines.append(f"{r['id']:<12} {r['status']:<8} {r['last_ok']:<10} {r['items']:>6}  {r['error'][:90]}")
    total = conn.execute("SELECT (SELECT COUNT(*) FROM items), (SELECT COUNT(*) FROM snapshots)").fetchone()
    lines.append(f"\n{total[0]} items, {total[1]} snapshots")
    return "\n".join(lines)


def main() -> int:
    load_env()
    print(report(db.connect(), pipeline.load()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
