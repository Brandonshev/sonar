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

## Dashboard

`sonar-web.service` serves the dashboard on `127.0.0.1:8390` only (RAM cap 150 MB; it uses about 20 MB).
It is not reachable from the internet until you put HTTPS in front of it, using option A or B below.
Never open port 8390 itself: the password would then travel unencrypted.

### 1. Set the password (once)

```bash
ssh -t postline-oracle 'cd /opt/sonar && sudo -u sonar .venv/bin/python -m sonar.web --set-password && sudo systemctl restart sonar-web'
```

Only a scrypt hash is stored in `.env`, plus a random `SONAR_SECRET` for signing the login cookie.
Running this again changes the password and signs everyone out. After 5 wrong tries,
logins from that IP are blocked for 15 minutes.

### 2A. Cloudflare Tunnel (recommended)

This is how the other apps on this server are exposed. You get free HTTPS, open no ports and install
nothing new. Replace `sonar.example.com` with a name on your Cloudflare domain.

```bash
ssh -t postline-oracle 'cloudflared tunnel login'
```

This prints a link. Open it, sign in to Cloudflare and pick your domain. Then:

```bash
ssh postline-oracle 'cloudflared tunnel create sonar && cloudflared tunnel route dns sonar sonar.example.com'
```

```bash
ssh postline-oracle 'sudo mkdir -p /etc/sonar && sudo cp ~/.cloudflared/$(cloudflared tunnel list -o json | python3 -c "import json,sys;print([t for t in json.load(sys.stdin) if t[\"name\"]==\"sonar\"][0][\"id\"])").json /etc/sonar/tunnel-credentials.json && printf "tunnel: sonar\ncredentials-file: /etc/sonar/tunnel-credentials.json\ningress:\n  - hostname: sonar.example.com\n    service: http://127.0.0.1:8390\n  - service: http_status:404\n" | sudo tee /etc/sonar/tunnel.yml'
```

```bash
ssh postline-oracle 'printf "[Unit]\nDescription=Cloudflare tunnel for Sonar\nAfter=network-online.target\n\n[Service]\nExecStart=/usr/bin/cloudflared tunnel --no-autoupdate --config /etc/sonar/tunnel.yml run\nRestart=on-failure\nMemoryMax=80M\n\n[Install]\nWantedBy=multi-user.target\n" | sudo tee /etc/systemd/system/sonar-tunnel.service && sudo systemctl daemon-reload && sudo systemctl enable --now sonar-tunnel'
```

Then set `SONAR_PUBLIC_URL=https://sonar.example.com` in `/opt/sonar/.env` and run
`sudo systemctl restart sonar-web`. That turns on the links in alerts and digests and marks the login cookie `Secure`.

### 2B. Open a port on Oracle and use Caddy (alternative)

Needs a domain or a free DuckDNS name with an A record pointing at the server's public IP.

1. **Oracle security list** (the cloud firewall): Oracle Console → Networking → Virtual Cloud Networks →
   your VCN → Subnets → the subnet → Security Lists → Default Security List → **Add Ingress Rules**:
   Source CIDR `0.0.0.0/0`, IP protocol TCP, destination port `80`. Add a second rule for port `443`.
2. **Server firewall** (Oracle's Ubuntu image uses iptables with a final REJECT rule, so insert above it):

   ```bash
   ssh postline-oracle 'sudo iptables -I INPUT 5 -p tcp -m state --state NEW --dport 80 -j ACCEPT && sudo iptables -I INPUT 5 -p tcp -m state --state NEW --dport 443 -j ACCEPT && sudo netfilter-persistent save'
   ```

3. **Caddy** (about 30–40 MB of RAM; gets and renews Let's Encrypt certificates by itself):

   ```bash
   ssh postline-oracle 'sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl && curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg && curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt | sudo tee /etc/apt/sources.list.d/caddy-stable.list && sudo apt-get update && sudo apt-get install -y caddy'
   ```

   ```bash
   ssh postline-oracle 'printf "sonar.example.com {\n  reverse_proxy 127.0.0.1:8390\n}\n" | sudo tee /etc/caddy/Caddyfile && sudo systemctl reload caddy'
   ```

Then set `SONAR_PUBLIC_URL` as in 2A.

## Logs and disk

- Logs: `/opt/sonar/logs/sonar.log` and `web.log`, each rotated at 50 MB with 3 old files kept (200 MB max).
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
