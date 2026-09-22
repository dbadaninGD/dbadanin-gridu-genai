#!/bin/bash
# GCE instance startup script. Installs Docker + the `docker compose` (v2)
# plugin so the app can be deployed with `sudo docker compose up -d --build`
# right after SSHing in. Runs as root automatically on every boot via GCE's
# metadata startup-script mechanism.
set -euo pipefail

exec > >(tee /var/log/startup-script.log) 2>&1
echo "[startup] $(date -Is) starting"

apt-get update
# docker-compose-v2 provides the `docker compose` (space, not hyphen)
# subcommand -- the standalone `docker-compose` python package is
# deprecated/unavailable on current Ubuntu releases.
apt-get install -y docker.io docker-compose-v2 git

systemctl enable docker
systemctl start docker

# Let the default GCE user run docker without sudo (convenient for the
# manual `git clone && docker compose up` steps in gcp_readme.md).
if id "$(logname 2>/dev/null || echo ubuntu)" &>/dev/null; then
    usermod -aG docker "$(logname 2>/dev/null || echo ubuntu)" || true
fi

echo "[startup] $(date -Is) done"
