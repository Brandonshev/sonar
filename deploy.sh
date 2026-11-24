#!/usr/bin/env bash
# Installs or updates Sonar on the server. Run as root ON THE SERVER, or from your Mac:
#   ssh postline-oracle 'sudo bash -s' < deploy.sh
# Safe to re-run: it pulls the latest code, refreshes the venv and units, and never touches .env or data/.
set -euo pipefail

REPO=https://github.com/Brandonshev/sonar.git
DIR=/opt/sonar
U=sonar

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }

id "$U" >/dev/null 2>&1 || useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin "$U"
command -v git >/dev/null || apt-get install -y -q git
python3 -c "import ensurepip" 2>/dev/null || apt-get install -y -q python3-venv

if [ -d "$DIR/.git" ]; then
  sudo -u "$U" git -C "$DIR" pull --ff-only -q
else
  install -d -o "$U" -g "$U" "$DIR"
  sudo -u "$U" git clone -q "$REPO" "$DIR"
fi
install -d -o "$U" -g "$U" -m 750 "$DIR/data" "$DIR/logs"

[ -x "$DIR/.venv/bin/python" ] || sudo -u "$U" python3 -m venv "$DIR/.venv"
sudo -u "$U" "$DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$DIR/requirements.txt"

# .env holds tokens: never in git, readable only by the sonar user.
if [ ! -f "$DIR/.env" ]; then
  install -o "$U" -g "$U" -m 600 "$DIR/.env.example" "$DIR/.env"
  echo "Created $DIR/.env from the example. Add your tokens to it."
fi
chown "$U:$U" "$DIR/.env"; chmod 600 "$DIR/.env"

install -m 644 "$DIR"/ops/systemd/sonar*.service "$DIR"/ops/systemd/sonar*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now -q sonar.timer sonar-housekeeping.timer

echo "Deployed Sonar $(sudo -u "$U" "$DIR/.venv/bin/python" -c 'import sonar; print(sonar.VERSION)')"
systemctl list-timers 'sonar*' --no-pager | head -4
