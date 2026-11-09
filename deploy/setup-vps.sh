#!/usr/bin/env bash
# One-time hardening + Docker install for a fresh Ubuntu 22.04/24.04 VPS. Run as root: sudo bash deploy/setup-vps.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

apt-get update
apt-get install -y ca-certificates curl ufw fail2ban unattended-upgrades

if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker   # containers use restart: unless-stopped, so the stack returns after a reboot

# Firewall: SSH only by default. Open 80/443 only if you use the `tls` profile.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
if [ "${OPEN_HTTPS:-no}" = "yes" ]; then ufw allow 80/tcp; ufw allow 443/tcp; fi
ufw --force enable
systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

# 2 GB swap as a safety net on a 4 GB machine
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo "Done. Next: cp .env.example .env, fill it in, then: docker compose up -d --build"
