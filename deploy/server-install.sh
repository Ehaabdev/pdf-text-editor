#!/usr/bin/env bash
# Install or update the app on the server, from the files push.sh just unpacked in
# /opt/pdf-editor: its Python packages (the exact versions the tests passed with), the
# background service, and https for the domain. Safe to run again.
#   sudo bash /opt/pdf-editor/deploy/server-install.sh <domain>
set -euo pipefail
DOMAIN="${1:?usage: server-install.sh <domain>}"
cd /opt/pdf-editor

[ -d backend/.venv ] || python3 -m venv backend/.venv
backend/.venv/bin/pip install --quiet --upgrade pip
backend/.venv/bin/pip install --quiet -r backend/requirements.lock

install -m 644 deploy/pdf-editor.service /etc/systemd/system/pdf-editor.service
sed "s/{{DOMAIN}}/$DOMAIN/g" deploy/Caddyfile > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable pdf-editor >/dev/null
systemctl restart pdf-editor
systemctl reload caddy 2>/dev/null || systemctl restart caddy

for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null http://127.0.0.1:8000/; then
    echo "The site is running: https://$DOMAIN"
    exit 0
  fi
  sleep 1
done
echo "The app didn't start. Its log: journalctl -u pdf-editor -n 50" >&2
exit 1
