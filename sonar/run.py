"""Runs pipeline nodes.

  python -m sonar.run --source github   run one node by id
  python -m sonar.run --all-due         run every enabled node whose schedule is due

A node's code lives in a module named by its type: source.github -> sonar.collectors.github,
scorer.x / filter.x -> sonar.scoring.x, output.x -> sonar.outputs.x. Each module exposes
run(conn, node) -> int. A node runs on "every" (interval) or "at" (daily time in "tz");
with neither, it runs when one of its upstream nodes ran.
"""
import argparse
import importlib
import json
import logging
import sys
import time

from . import VERSION, db, load_env, pipeline, setup_logging

log = logging.getLogger("sonar.run")
PACKAGES = {"source": "collectors", "scorer": "scoring", "filter": "scoring", "output": "outputs"}
ORDER = list(PACKAGES)  # sources first, outputs last


def node_module(node: dict):
    kind, name = node["type"].split(".", 1)
    return importlib.import_module(f"sonar.{PACKAGES[kind]}.{name}")


def run_node(conn, node: dict, module=None, now: int | None = None, notify=None) -> bool:
    """Run one node. Never raises: failures go to source_health and the event log.
    A node with params.max_failures is switched off after that many failures in a row, with one
    ntfy message; it stays off until `python -m sonar.run --enable <id>`."""
    now = now or int(time.time())
    h = db.health_row(conn, node["id"])
    if h and h["disabled_at"]:
        log.info("node %s is auto-disabled; skipping (re-enable with --enable %s)", node["id"], node["id"])
        return False
    try:
        module = module or node_module(node)
        n = module.run(conn, node)
        with conn:
            db.record_run(conn, node["id"], ok=True, items=n or 0, now=now)
        return True
    except Exception as e:  # one broken node must never stop the rest
        log.exception("node %s failed", node["id"])
        err = f"{type(e).__name__}: {e}"
        with conn:
            db.record_run(conn, node["id"], ok=False, error=err, now=now)
            db.emit_event(conn, "node.failed", now=now, node=node["id"], error=str(e)[:500])
        limit = node.get("params", {}).get("max_failures")
        streak = db.health_row(conn, node["id"])["fail_streak"]
        if limit and streak >= limit:
            with conn:
                db.set_disabled(conn, node["id"], True, now)
                db.emit_event(conn, "source.disabled", now=now, node=node["id"], failures=streak, error=err[:500])
            log.error("node %s disabled after %d failures in a row", node["id"], streak)
            try:
                from .outputs.notify import public_url, send_ntfy
                (notify or send_ntfy)(f"⚠️ Sonar: {node.get('label', node['id'])} switched off",
                                      f"{streak} failures in a row. Last error: {err[:300]}\n"
                                      f"Re-enable with: python -m sonar.run --enable {node['id']}",
                                      click=public_url("health"), tags=["warning"], priority=4)
            except Exception:
                log.exception("could not send the disabled-source alert")
        return False


def due_nodes(data: dict, last_runs: dict, now: int, ran: set) -> list[dict]:
    """Enabled nodes that are due, in source -> scorer/filter -> output order."""
    out = []
    for node in sorted(data["nodes"], key=lambda n: ORDER.index(n["type"].split(".")[0])):
        if not node["enabled"]:
            continue
        last = last_runs.get(node["id"])
        if "every" in node:
            if last is None or now - last >= pipeline.every_seconds(node["every"]):
                out.append(node)
        elif "at" in node:
            if pipeline.at_due(node, last, now):
                out.append(node)
        elif any(u in ran for u in pipeline.upstream(data, node["id"])):
            out.append(node)
    return out


def run_all_due(conn, data: dict, now: int | None = None, modules: dict | None = None) -> set:
    now = now or int(time.time())
    last = db.last_runs(conn)
    ran: set[str] = set()
    for kind in ORDER:  # one pass per stage, so edge-triggered nodes see this pass's upstream runs
        for node in due_nodes(data, last, now, ran):
            if node["type"].startswith(kind + ".") and node["id"] not in ran:
                run_node(conn, node, (modules or {}).get(node["id"]), now)
                ran.add(node["id"])
    return ran


def top_table(conn, ts: int, per_group: int = 20) -> str:
    """Plain-text table of the best scores at ts, grouped by source."""
    rows = conn.execute(
        """SELECT i.source, i.category, i.title, s.heat, s.score, s.reasons_json FROM scores s
           JOIN items i ON i.id = s.item_id WHERE s.ts = ? ORDER BY i.source, s.score DESC, s.heat DESC""",
        (ts,)).fetchall()
    out, counts = [], {}
    for r in rows:
        n = counts[r["source"]] = counts.get(r["source"], 0) + 1
        if n == 1:
            out.append(f"\n== {r['source']} ==\n{'score':>5} {'heat':>6}  {'category':<14} {'title':<50} why")
        if n <= per_group:
            why = json.loads(r["reasons_json"])[0]
            out.append(f"{r['score']:>5.0f} {r['heat']:>6.1f}  {(r['category'] or '')[:14]:<14} "
                       f"{(r['title'] or '')[:50]:<50} {why}")
    return f"Sonar v{VERSION} top scores" + ("\n".join(out) if out else "\n(nothing scored yet)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sonar.run")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--source", "--node", dest="node", help="run one node by id")
    g.add_argument("--all-due", action="store_true", help="run every node whose schedule is due")
    g.add_argument("--score", action="store_true", help="score now and print the top 20 per source")
    g.add_argument("--test-alert", action="store_true", help="send a dummy ntfy message")
    g.add_argument("--enable", metavar="NODE", help="turn an auto-disabled node back on")
    p.add_argument("--pipeline", default=pipeline.PIPELINE_PATH)
    args = p.parse_args(argv)

    load_env()
    setup_logging()
    try:
        data = pipeline.load(args.pipeline)
    except pipeline.PipelineError as e:
        log.error("refusing to run: %s", e)
        return 2
    log.info("Sonar v%s starting", VERSION)
    if args.test_alert:
        from .outputs.notify import send_ntfy
        try:
            send_ntfy("Sonar test alert", f"If you can read this, Sonar v{VERSION} alerts work.",
                      tags=["satellite"])
        except Exception as e:
            print(f"Test alert failed: {e}")
            return 1
        print("Test alert sent.")
        return 0
    conn = db.connect()
    if args.enable:
        with conn:
            db.set_disabled(conn, args.enable, False)
        print(f"{args.enable} re-enabled.")
        return 0

    if args.score:
        node = next((n for n in data["nodes"] if n["type"] == "scorer.heat"), None)
        if not node:
            log.error("no scorer.heat node in pipeline.json")
            return 2
        now = int(time.time())
        if not run_node(conn, node, now=now):
            return 1
        print(top_table(conn, now))
        return 0
    if args.all_due:
        ran = run_all_due(conn, data)
        log.info("ran %d node(s): %s", len(ran), ", ".join(sorted(ran)) or "none due")
        return 0
    node = next((n for n in data["nodes"] if n["id"] == args.node), None)
    if not node:
        log.error("no node %r in pipeline.json", args.node)
        return 2
    return 0 if run_node(conn, node) else 1


if __name__ == "__main__":
    sys.exit(main())
