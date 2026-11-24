# Sonar

A trend radar that spots fast-climbing things early: repos, products, posts and topics across GitHub, Hacker News, Product Hunt, Reddit and more.

**heat = (growth in the last 24h) / max(normal daily growth over the 7 days before, floor)**. A heat of 5 means something is growing 5x faster than usual.

Status: v0.5.0, deployed (see DEPLOY.md). Daily digest and instant ntfy alerts. Collectors for GitHub, Hacker News, Product Hunt and Reddit (off until API access is approved), plus the heat scorer.

## How it fits together

```
collectors (one per source) -> SQLite snapshots -> scorer -> filters -> outputs
```

Everything Sonar runs, and how the parts connect, lives in [`pipeline.json`](pipeline.json), validated by [`pipeline.schema.json`](pipeline.schema.json). Sonar refuses to start if the file is invalid.

- Node types: `source.*`, `scorer.*`, `filter.*`, `output.*`. A node with `every` runs on that schedule. A node without it runs when an upstream node (per `edges`) runs.
- Every node's health (last run, last success, last error, items found) is kept in the `source_health` table. A broken node never stops the others.
- Findings go to the `events` table as standard events: `{"type": "trend.found", "source": ..., "item": ..., "heat": ..., "ts": ...}`.

## Scoring (`scorer.heat`)

For each item and metric (stars, points, votes...):

- **v** = value now minus value 24h ago, interpolated between snapshots. If Sonar has watched the item for less than 24h, v is estimated from what it has seen and scaled to 24h.
- **b** = average daily growth over the 7 days before that. Items under 48h old have no baseline yet, so b is the typical 24h growth of new items from the same source.
- **heat** = v / max(b, floor). The floor per source and metric is in `pipeline.json`, so a zero baseline can never divide by zero.
- **Early bonus:** heat x (1 + early_weight / log10(size + 10)), so small things count more.
- **Cross-source bonus:** +25% for each other source where the same entity is climbing (heat >= 2).
- **Score 0-100:** heat on a log scale, normalised per source so GitHub stars and HN points are comparable. A heat of 20 (configurable) scores 100.

Each score is saved with plain-English reasons, e.g. `stars 56 -> 628 in 24h, 114x its normal rate`. When an item's heat crosses `trend_min_heat` (5), a `trend.found` event is logged.

## Alerts and digest

- **Instant alert** (`output.ntfy_alert`, runs after each scoring): an ntfy push when an item's heat is at least `min_heat` (5) and its score at least `min_score`. The same item is not alerted again within `cooldown_hours` (24) unless its score has risen by `rejump_score` (20). There are never more than `max_per_hour` (5) alerts an hour, and none during the first `warmup_hours` (24) of collecting.
- **Daily digest** (`output.digest`, `"at": "07:00"`, `"tz": "Australia/Sydney"`): top climbers per source, new entries, broken sources and the version, saved as `data/digests/YYYY-MM-DD.html`. It's sent as a short ntfy message, and also emailed if `SMTP_*` and `DIGEST_TO` are set in `.env`.
- `python -m sonar.run --test-alert` sends a dummy ntfy message.

All thresholds, times and caps are in `pipeline.json`.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m sonar.run --source github   # one node
.venv/bin/python -m sonar.run --all-due         # everything that's due (put this on a timer)
.venv/bin/python -m sonar.run --score           # score now, print the top 20 per source
```

## Tests

```bash
.venv/bin/pip install pytest && .venv/bin/python -m pytest
```

## Ops

`ops/grab-a1.sh` keeps requesting an Oracle Always Free ARM A1 server until one is free. See `ops/HANDOFF-grab-a1.md`.

Investing-related signals are information only, not financial advice.
