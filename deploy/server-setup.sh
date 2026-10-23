#!/usr/bin/env bash
# First setup of a fresh Ubuntu 24.04 server (Oracle Cloud Always Free, Arm or x86):
# system packages, fonts, Caddy (https), the app's own user and folders, and the
# firewall. Run once, on the server:  sudo bash server-setup.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a  # restart services after upgrades without asking
# Keep the firewall rules across restarts, without asking.
echo "iptables-persistent iptables-persistent/autosave_v4 boolean true" | debconf-set-selections
echo "iptables-persistent iptables-persistent/autosave_v6 boolean true" | debconf-set-selections

apt-get update
apt-get install -y software-properties-common
add-apt-repository -y multiverse  # Microsoft's core fonts live there
apt-get update
apt-get -y upgrade

# Microsoft's free core fonts (Arial, Times New Roman, Verdana, Georgia, Courier New, …)
# may be installed on Linux once their licence is accepted. Carlito and Caladea have the
# same letter widths as Calibri and Cambria; the editor's own look-alikes come with it.
echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections
apt-get install -y \
  python3 python3-venv python3-dev build-essential \
  fontconfig ttf-mscorefonts-installer fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation fonts-dejavu-core \
  debian-keyring debian-archive-keyring apt-transport-https curl gpg iptables-persistent

# Caddy: https, with certificates it renews on its own.
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

# The app runs as its own user, and may write only its data folder.
id pdfeditor >/dev/null 2>&1 || useradd --system --home /var/lib/pdf-editor --shell /usr/sbin/nologin pdfeditor
mkdir -p /opt/pdf-editor/incoming /var/lib/pdf-editor/data /var/lib/pdf-editor/cache
chown -R pdfeditor:pdfeditor /var/lib/pdf-editor
chown -R "${SUDO_USER:-ubuntu}" /opt/pdf-editor

# Oracle's Ubuntu images let only SSH in: let web visitors in too (ports 80 and 443).
for port in 80 443; do
  iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null \
    || iptables -I INPUT 6 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
done
netfilter-persistent save

echo "Server ready. Next, from your PC: deploy/push.sh ubuntu@<address> <domain>"
