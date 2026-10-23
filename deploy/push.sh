#!/usr/bin/env bash
# Put the committed version on the server, from this PC (Git Bash):
#   deploy/push.sh ubuntu@<server address> <domain>
# Only what's committed goes up (never samples/ or local data), plus the website built here
# (with the name in frontend/.env.local).
set -euo pipefail
HOST="${1:?usage: deploy/push.sh ubuntu@<address> <domain>}"
DOMAIN="${2:?usage: deploy/push.sh ubuntu@<address> <domain>}"
KEY="${PDFEDIT_SSH_KEY:-$HOME/.ssh/oracle_pdf_editor}"
cd "$(dirname "$0")/.."

if ! git diff --quiet HEAD; then
  echo "Commit your changes first: only committed files are sent." >&2
  exit 1
fi
(cd frontend && npm run build)

TMP="$(mktemp -d)"
git archive --format=tar HEAD backend deploy LICENSE README.md > "$TMP/app.tar"
tar -rf "$TMP/app.tar" frontend/dist

SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new)
scp -i "$KEY" -o StrictHostKeyChecking=accept-new "$TMP/app.tar" "$HOST:/opt/pdf-editor/incoming/app.tar"
"${SSH[@]}" "$HOST" "sudo bash -c 'cd /opt/pdf-editor && rm -rf backend/app backend/lookalikes frontend/dist deploy && tar -xf incoming/app.tar && rm incoming/app.tar && bash deploy/server-install.sh $DOMAIN'"
rm -rf "$TMP"
