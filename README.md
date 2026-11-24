<p align="center"><img src="design/icon.svg" width="112" alt="Sonar icon: radar rings, a sweep and an orange blip"></p>

<h1 align="center">Sonar</h1>

<p align="center">A trend radar that spots fast-climbing things before everyone else:<br>
GitHub repos, products, videos, hashtags, searches, articles and companies.</p>

---

Sonar checks a dozen sources on a schedule and saves a snapshot of every number it sees (stars,
views, points, mentions). From that history it works out how much faster than usual each thing is
growing right now. You get a ranked dashboard, an instant ntfy push when something really takes off,
a daily digest, and a JSON feed for other programs.

It runs on a free Oracle Cloud VM with 1 GB of RAM, next to other apps: about 30 MB per run, with
hard CPU and memory caps. Python standard library plus two small dependencies, and $0 to run.

*Attention signals only, not financial advice.*

## The heat score

For each item and metric:

```
v    = value now − value 24 hours ago          (interpolated between snapshots)
b    = average daily growth over the 7 days before that
heat = v / max(b, floor)                        5 = growing five times faster than usual
```

- **New items** (under 48 hours old) have no history yet, so they're compared with the typical
  growth of other new items from the same source.
- **Early bonus:** `heat × (1 + 1 / log10(size + 10))`. A 40-star repo jumping to 600 outranks a
  100k-star repo growing slowly.
- **Cross-source bonus:** +25% for each other source where the same thing is also climbing. Items
  are matched by URL, repo, domain, hashtag, or a careful name match.
- **Score 0–100:** heat on a log scale, normalised per source so stars, points and views are comparable.

Every score comes with plain-English reasons, e.g. `stars 56 -> 628 in 24h, 114x its normal rate`.
This is why Sonar saves snapshots from day one: heat needs history, and most history can't be backfilled.

## Sources

| Source | What | How | Notes |
|---|---|---|---|
| GitHub | New repos by stars, tracked repos, company orgs | REST API | `GITHUB_TOKEN` recommended |
| Hacker News | Top, new and best stories | Firebase API | |
| Product Hunt | Recent launches | GraphQL API | `PRODUCTHUNT_TOKEN` |
| Reddit | Rising and hot posts in 132 subreddits | OAuth API | Off until Reddit approves API access |
| YouTube | AU/US/GB charts, topic searches, view re-checks | Data API v3 | Quota tracked, stays under 8,000 of 10,000 units a day |
| Google Trends | Daily trending searches (AU/US/GB) | Official RSS feed | |
| Google Trends rising | Rising related queries for seed topics | Unofficial endpoints | Backoff; own health status |
| Wikipedia | Top articles, company and linked articles | Wikimedia pageviews API | Backfills 10 days |
| TikTok | Hashtag post and view counts | Public web API | No trending lists or sounds (they need a real browser) |
| Instagram | Hashtag post counts | Instaloader + throwaway account | Off by default; fragile |
| News | Company mentions in 10 RSS feeds plus HN and Reddit titles | RSS/Atom | Each article counts once |

Fragile sources (TikTok, Instagram) are rate-limited with random jitter and switch themselves off
after 3 failures in a row. You get one ntfy message and a warning on the dashboard. Turn them back
on with `python -m sonar.run --enable <id>`.

**Companies:** `companies.json` lists 49 companies with careful aliases ("Meta Platforms", not
"Meta"). `scorer.companies` combines news, HN, Reddit, Wikipedia, Product Hunt launches and GitHub
org activity into one company heat, with a per-signal breakdown on the Companies tab.

## pipeline.json

Everything Sonar runs, and what connects to what, lives in [`pipeline.json`](pipeline.json) and is
validated against [`pipeline.schema.json`](pipeline.schema.json). Sonar refuses to start on an
invalid file. Nothing is hard-coded, so a workflow-map app can edit the file directly.

```json
{"id": "tiktok", "type": "source.tiktok", "enabled": true, "every": "4h",
 "params": {"hashtags": ["grwm", "booktok"], "min_interval_seconds": 4, "jitter_seconds": 3, "max_failures": 3}}
```

- **Node types:** `source.*`, `scorer.*`, `filter.*`, `output.*`. The code for `source.github` is `sonar/collectors/github.py`.
- **Schedules:** `"every": "3h"`, or `"at": "07:00"` with `"tz": "Australia/Sydney"`. A node with neither runs whenever an upstream node (in `edges`) runs.
- **Health:** every node's health (last run, last success, last error, items, failures in a row) is shown on the dashboard and by `python -m sonar.health`.
- **Events:** findings are standard events (`trend.found`, `alert.sent`, `source.disabled`, ...) in the `events` table, ready for a future central hub.

## Outputs

- **Dashboard:** password-protected and built for phones, with dark mode and an installable home-screen icon. Pages: top climbers with 7-day sparklines, tabs (Social, YouTube, Tech products, GitHub, Companies, Other), item pages with history charts and alert history, a digest archive and health.
- **Instant alerts** (ntfy): heat ≥ 5, biggest spikes first; no repeat for the same item within 24h unless its score jumps another 20; at most 5 an hour; quiet for the first 24h of collecting. High-volume sources get a daily cap (Hacker News: 3 a day), so their best still get through without flooding you. They're also ranked lower on the dashboard (`source_weights`), which affects scores only, never alerts.
- **Daily digest** at 7:00 Sydney time: top climbers per source, new entries and broken sources. Saved as a page, sent by ntfy, and by email if SMTP is set.
- **Lookout feed:** `GET /api/trends`, token-protected and rate-limited, serving `trend.found` events for video trends. See [API.md](API.md).

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                                  # fill in the tokens you have
.venv/bin/python -m sonar.run --source hackernews     # one node
.venv/bin/python -m sonar.run --all-due               # everything that's due (what the timer runs)
.venv/bin/python -m sonar.run --score                 # score now, print the top 20 per source
.venv/bin/python -m sonar.health                      # status of every node
.venv/bin/python -m sonar.web --set-password          # dashboard password (stores a scrypt hash only)
.venv/bin/python -m sonar.web                         # dashboard on 127.0.0.1:8390
.venv/bin/python -m sonar.run --test-alert            # send a test ntfy message
```

Tests (no network, about 7 s): `.venv/bin/pip install pytest && .venv/bin/python -m pytest`

**Deploying:** [DEPLOY.md](DEPLOY.md) covers it: one command (`ssh <server> 'sudo bash -s' < deploy.sh`), systemd timers with CPU and memory caps, HTTPS through a Cloudflare Tunnel, logs and housekeeping.

## Layout

```
sonar/collectors/   one file per source (base.py: storage, pacing, retries)
sonar/scoring/      heat.py (items), companies.py (company roll-up)
sonar/outputs/      ntfy_alert, digest, lookout_feed, dashboard
sonar/web.py        dashboard + /api/trends (standard library HTTP server, no JavaScript)
sonar/static/       icons and web manifest
design/icon.svg     icon master
ops/                systemd units, Oracle ARM server helper
```

## Version history

| Version | What changed |
|---|---|
| **1.0.0** | Radar app icon (favicon, home-screen and PWA icons, ntfy icon), web manifest, audit fixes: alert warm-up can't be fooled by backfilled history, scores kept 14 days |
| 0.10.0 | Lookout feed: `output.lookout_feed` and a token-protected, rate-limited `GET /api/trends`; API.md |
| 0.9.0 | News mentions per company; company heat roll-up and Companies tab |
| 0.8.0 | TikTok hashtags; Instagram via Instaloader (off by default); pacing with jitter, auto-disable after 3 failures, cross-platform hashtag watchlists |
| 0.7.0 | YouTube (quota-tracked), Google Trends daily and rising, Wikipedia pageviews; careful name linking |
| 0.6.0 | Password-protected dashboard behind HTTPS (Cloudflare Tunnel) |
| 0.5.0 | Instant ntfy spike alerts with cooldown and hourly cap; daily digest at 7:00 Sydney |
| 0.4.0 | Deployed to Oracle Cloud: systemd timers with resource caps, health command, housekeeping, log rotation |
| 0.3.0 | Heat scorer with new-item baseline, early and cross-source bonuses, plain-English reasons |
| 0.2.0 | GitHub, Hacker News, Product Hunt and Reddit collectors; URL entity keys |
| 0.1.0 | Scaffold: SQLite schema with migrations, validated `pipeline.json`, node runner |

## Limits, honestly

- TikTok trending lists and sound counts need a real headless browser (300–500 MB of RAM), which doesn't fit this server. Sonar watches seed and discovered hashtags instead.
- Reddit's API now needs Reddit's approval, and Instagram bans fresh automated accounts quickly. Both sources are written but off.
- Scraped sources (TikTok, Google Trends rising, Instagram) break the platforms' terms for automated access and can stop working at any time. Each one fails on its own without affecting the rest.
- A heat of 5 on day one means less than a heat of 5 after a week, because baselines need history.
