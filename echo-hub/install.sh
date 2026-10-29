#!/usr/bin/env bash
# Run on the Latitude: installs echo-hub as a systemd --user service on :8094.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p ~/.local/share/echo-hub/museecho ~/.config/systemd/user
install -m 755 "$HERE/echo_hub.py" ~/.local/share/echo-hub/echo_hub.py
install -m 644 "$HERE/echo-hub.service" ~/.config/systemd/user/echo-hub.service
systemctl --user daemon-reload
systemctl --user enable --now echo-hub.service >/dev/null
systemctl --user restart echo-hub.service
echo "echo-hub running. LAN access needs: sudo ufw allow from 192.168.1.0/24 to any port 8094 proto tcp"
