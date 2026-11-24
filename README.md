# Sonar

A trend radar that spots fast-climbing things early: repos, products, posts and topics across GitHub, Hacker News, Product Hunt, Reddit and more.

**heat = (growth in the last 24h) / max(normal daily growth over the 7 days before, floor)**. A heat of 5 means something is growing 5x faster than usual.

Status: v0.1.0, scaffold only (no collectors yet).

## How it fits together

```
collectors (one per source) -> SQLite snapshots -> scorer -> filters -> outputs
```

Everything Sonar runs, and how the parts connect, lives in [`pipeline.json`](pipeline.json), validated by [`pipeline.schema.json`](pipeline.schema.json). Sonar refuses to start if the file is invalid.

- Node types: `source.*`, `scorer.*`, `filter.*`, `output.*`. A node with `every` runs on that schedule. A node without it runs when an upstream node (per `edges`) runs.
- Every node's health (last run, last success, last error, items found) is kept in the `source_health` table. A broken node never stops the others.
- Findings go to the `events` table as standard events: `{"type": "trend.found", "source": ..., "item": ..., "heat": ..., "ts": ...}`.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m sonar.run --source github   # one node
.venv/bin/python -m sonar.run --all-due         # everything that's due (put this on a timer)
```

## Tests

```bash
.venv/bin/pip install pytest && .venv/bin/python -m pytest
```

## Ops

`ops/grab-a1.sh` keeps requesting an Oracle Always Free ARM A1 server until one is free. See `ops/HANDOFF-grab-a1.md`.

Investing-related signals are information only, not financial advice.
