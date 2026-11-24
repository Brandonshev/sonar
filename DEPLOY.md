# Deploying Sonar

Sonar runs on the Oracle Cloud Always Free server (`ssh postline-oracle`, Ubuntu 24.04, 1 GB RAM)
next to Postline, codedrop, patchbay and tradinglab. It lives in `/opt/sonar`, runs as the
`sonar` system user, and is driven by systemd timers. There is no long-running process.

| What | When | Caps |
|---|---|---|
| `sonar.timer` → `sonar.service` (`python -m sonar.run --all-due`) | every 15 min; runs only the nodes that are due, then the scorer | CPU 25%, RAM 300 MB, nice 10, idle I/O |
| `sonar-housekeeping.timer` → `python -m sonar.housekeeping` | daily 04:40 | same |

## First install / every update

From your Mac, in the repo folder (push your changes to GitHub first; the server pulls from there):

```bash
ssh postline-oracle 'sudo bash -s' < deploy.sh
```

This creates the `sonar` user, clones or pulls the repo into `/opt/sonar`, builds the venv,
installs the systemd units and starts the timers. It never touches `.env` or `data/`.

## Tokens (.env)

`.env` is never in git. Copy your local one to the server:

```bash
scp .env postline-oracle:/tmp/sonar.env
```

```bash
ssh postline-oracle 'sudo install -o sonar -g sonar -m 600 /tmp/sonar.env /opt/sonar/.env && rm /tmp/sonar.env'
```

Or edit it in place:

```bash
ssh -t postline-oracle 'sudo -u sonar nano /opt/sonar/.env'
```

## Checking on it

```bash
ssh postline-oracle 'cd /opt/sonar && sudo -u sonar .venv/bin/python -m sonar.health'
```

```bash
ssh postline-oracle 'cd /opt/sonar && sudo -u sonar .venv/bin/python -m sonar.run --score'
```

```bash
ssh postline-oracle 'sudo tail -n 50 /opt/sonar/logs/sonar.log'
```

```bash
ssh postline-oracle 'systemctl list-timers "sonar*"; systemctl status sonar.service --no-pager'
```

Run a collection right now instead of waiting for the timer:

```bash
ssh postline-oracle 'sudo systemctl start sonar.service'
```

## Logs and disk

- Logs: `/opt/sonar/logs/sonar.log`, rotated at 50 MB with 3 old files kept (200 MB max).
- Database: `/opt/sonar/data/sonar.db`. Snapshots older than 90 days are thinned to one per day,
  scores older than 90 days are deleted, and `VACUUM` runs at most weekly.

## Stopping or removing

```bash
ssh postline-oracle 'sudo systemctl disable --now sonar.timer sonar-housekeeping.timer'
```

To remove everything (this deletes the collected history, which can't be rebuilt):

```bash
ssh postline-oracle 'sudo systemctl disable --now sonar.timer sonar-housekeeping.timer && sudo rm /etc/systemd/system/sonar* && sudo systemctl daemon-reload && sudo rm -rf /opt/sonar && sudo userdel sonar'
```

## Moving to the ARM A1 server later

Once `ops/grab-a1.sh` lands an A1 server: run `deploy.sh` there, stop the timers on the old server,
then copy `data/sonar.db` and `.env` across (with `sqlite3 sonar.db ".backup /tmp/sonar.db"` first,
so the copy is consistent).
