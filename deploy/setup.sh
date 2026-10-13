#!/usr/bin/env bash
# Install or update Kite Terminal on Ubuntu 24.04 (run as root from the unpacked folder):
#   bash deploy/setup.sh               install / update the app
#   bash deploy/setup.sh --lock-ssh    also turn off SSH password login (do this only
#                                      after you have confirmed key login works)
# Safe to re-run: .env and data/ are never overwritten.
set -euo pipefail

APP=/opt/kite-terminal
SRC=$(cd "$(dirname "$0")/.." && pwd)
LOCK_SSH=0
[[ "${1:-}" == "--lock-ssh" ]] && LOCK_SSH=1

[[ $EUID -eq 0 ]] || { echo "Run as root."; exit 1; }
step() { echo -e "\n\033[1;34m==> $*\033[0m"; }

step "System packages and automatic security updates"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip ufw fail2ban unattended-upgrades rsync >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null

step "Service user"
id kite >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin kite

step "Copying app to $APP"
mkdir -p "$APP/data"
rsync -a --delete \
  --exclude ".env" --exclude "data/" --exclude ".venv/" --exclude "__pycache__/" \
  --exclude ".claude/" --exclude ".git/" --exclude ".cache/" "$SRC/" "$APP/"

if [[ ! -f "$APP/.env" ]]; then
  cp "$APP/.env.example" "$APP/.env"
  sed -i 's/^KITE_MOCK=.*/KITE_MOCK=0/' "$APP/.env"
  NEED_ENV=1
fi
chown -R kite:kite "$APP"
chmod 600 "$APP/.env"

step "Python environment"
[[ -d "$APP/.venv" ]] || runuser -u kite -- python3 -m venv "$APP/.venv"
runuser -u kite -- "$APP/.venv/bin/pip" install -q --upgrade pip
runuser -u kite -- "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

step "Firewall: allow SSH only"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
ufw status | head -5

step "Nightly local backup of .env and TP/SL records (kept 14 days)"
cat > /etc/cron.d/kite-terminal-backup <<'EOF'
30 23 * * * root mkdir -p /root/backups && tar -czf /root/backups/kite-$(date +\%F).tgz -C /opt/kite-terminal .env data/protections.json data/session.json data/drawings.json 2>/dev/null; find /root/backups -name 'kite-*.tgz' -mtime +14 -delete
EOF

if [[ $LOCK_SSH -eq 1 ]]; then
  step "Turning off SSH password login"
  if [[ ! -s /root/.ssh/authorized_keys ]]; then
    echo "No SSH key in /root/.ssh/authorized_keys - refusing to lock you out."; exit 1
  fi
  cat > /etc/ssh/sshd_config.d/10-kite-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
  sshd -t && systemctl reload ssh
  echo "Password login disabled; key login only."
fi

step "Service"
cp "$APP/deploy/kite-terminal.service" /etc/systemd/system/kite-terminal.service
systemctl daemon-reload
systemctl enable kite-terminal >/dev/null 2>&1

if [[ "${NEED_ENV:-0}" == 1 ]] || grep -q "your_api_key" "$APP/.env"; then
  echo -e "\n\033[1;33mNext: put your Kite API key and secret in $APP/.env, then run:\033[0m"
  echo "  nano $APP/.env"
  echo "  systemctl restart kite-terminal"
else
  systemctl restart kite-terminal
  sleep 3
  systemctl --no-pager --lines=5 status kite-terminal || true
fi
echo -e "\nDone. Logs: journalctl -u kite-terminal -f"
