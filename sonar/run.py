"""Runs pipeline nodes.

  python -m sonar.run --source github   run one node by id
  python -m sonar.run --all-due         run every enabled node whose schedule is due

A node's code lives in a module named by its type: source.github -> sonar.collectors.github,
scorer.x / filter.x -> sonar.scoring.x, output.x -> sonar.outputs.x. Each module exposes
run(conn, node) -> int. A node with no "every" runs when one of its upstream nodes ran.
"""
import argparse
import importlib
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


def run_node(conn, node: dict, module=None, now: int | None = None) -> bool:
    """Run one node. Never raises: failures go to source_health and the event log."""
    now = now or int(time.time())
    try:
        module = module or node_module(node)
        n = module.run(conn, node)
        with conn:
            db.record_run(conn, node["id"], ok=True, items=n or 0, now=now)
        return True
    except Exception as e:  # one broken node must never stop the rest
        log.exception("node %s failed", node["id"])
        with conn:
            db.record_run(conn, node["id"], ok=False, error=f"{type(e).__name__}: {e}", now=now)
            db.emit_event(conn, "node.failed", now=now, node=node["id"], error=str(e)[:500])
        return False


def due_nodes(data: dict, last_runs: dict, now: int, ran: set) -> list[dict]:
    """Enabled nodes that are due, in source -> scorer/filter -> output order."""
    out = []
    for node in sorted(data["nodes"], key=lambda n: ORDER.index(n["type"].split(".")[0])):
        if not node["enabled"]:
            continue
        if "every" in node:
            last = last_runs.get(node["id"])
            if last is None or now - last >= pipeline.every_seconds(node["every"]):
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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sonar.run")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--source", "--node", dest="node", help="run one node by id")
    g.add_argument("--all-due", action="store_true", help="run every node whose schedule is due")
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
    conn = db.connect()

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
