#!/usr/bin/env bash
# Idempotent installer for the Epson receipt printer API on Raspberry Pi OS.
# Run from the repo root: sudo deploy/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "run with sudo: sudo deploy/install.sh" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${SUDO_USER:-pi}"

echo "==> Installing apt prerequisites"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip libusb-1.0-0 libopenjp2-7 zlib1g
# libjpeg package name varies by OS release
apt-get install -y -qq libjpeg62-turbo 2>/dev/null || apt-get install -y -qq libjpeg62 || true

echo "==> Creating venv and installing Python dependencies (piwheels makes this fast)"
# Recreate the venv unless it actually works here — a venv copied over from
# another machine (e.g. rsync'd from a dev box) has foreign binaries/paths.
if ! sudo -u "$APP_USER" "$REPO_DIR/venv/bin/python" -c 'import sys' >/dev/null 2>&1; then
    rm -rf "$REPO_DIR/venv"
    sudo -u "$APP_USER" python3 -m venv "$REPO_DIR/venv"
fi
sudo -u "$APP_USER" "$REPO_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

echo "==> Installing udev rule for USB access"
install -m 644 "$REPO_DIR/deploy/99-epson-tm.rules" /etc/udev/rules.d/99-epson-tm.rules
udevadm control --reload-rules
udevadm trigger

echo "==> Installing systemd unit"
sed -e "s|/home/pi/epson-rp-api|$REPO_DIR|g" -e "s|User=pi|User=$APP_USER|" \
    "$REPO_DIR/deploy/epson-rp-api.service" > /etc/systemd/system/epson-rp-api.service
systemctl daemon-reload
systemctl enable --now epson-rp-api

echo "==> Done. Check: systemctl status epson-rp-api"
echo "    Test print: curl -X POST http://localhost:8080/print/test"
