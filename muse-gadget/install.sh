#!/usr/bin/env bash
# Install Meta's Muse Gadget Linux SDK on this machine as a systemd --user
# service for the current account (no sudo). Commands Muse runs get exactly
# this account's permissions.
#
# Run on the server:  bash install.sh
# Then pair from a machine with Bluetooth:  ./pair.sh   (see README)
set -euo pipefail

SDK_REPO=https://github.com/facebookincubator/muse-gadget-sdk
SDK_REV=${SDK_REV:-74a5e2d7fc895f109f83a9a1dbed705dbcd8b1ff}
HERE=$(cd "$(dirname "$0")" && pwd)
BASE=$HOME/.local/share/musegadget
STATE=$HOME/.local/state/musegadget

mkdir -p "$BASE" "$STATE" "$HOME/.local/bin" "$HOME/.config/systemd/user"
chmod 700 "$STATE"

if [[ ! -d $BASE/sdk/.git ]]; then
    git clone -q "$SDK_REPO" "$BASE/sdk"
fi
git -C "$BASE/sdk" fetch -q origin
git -C "$BASE/sdk" checkout -q --force "$SDK_REV"
git -C "$BASE/sdk" apply "$HERE/executor-user-session.patch"

# The distro's python-cryptography, plus websockets in a private venv.
python3 -m venv --system-site-packages "$BASE/venv"
"$BASE/venv/bin/pip" install -q --disable-pip-version-check websockets
"$BASE/venv/bin/pip" install -q --disable-pip-version-check --no-deps "$BASE/sdk/linux"

install -m 755 "$HERE/musegadget-wrapper" "$HOME/.local/bin/musegadget"
install -m 644 "$HERE/musegadget.service" "$HOME/.config/systemd/user/musegadget.service"
install -m 755 "$HERE/ntfy_to_muse.py" "$BASE/ntfy_to_muse.py"
install -m 644 "$HERE/ntfy-to-muse.service" "$HOME/.config/systemd/user/ntfy-to-muse.service"
systemctl --user daemon-reload
systemctl --user enable musegadget.service >/dev/null
systemctl --user restart musegadget.service

# Alerts: forward the homelab ntfy topic to Muse once it's configured.
CFG=$HOME/.config/latitude-muse-gadget/ntfy.env
if [[ -s $CFG ]]; then
    systemctl --user enable ntfy-to-muse.service >/dev/null
    systemctl --user restart ntfy-to-muse.service
else
    echo "Alerts off: put NTFY_TOPIC=<topic> in $CFG (mode 600) and rerun to forward ntfy alerts to Muse."
fi
echo "Installed. Paired: $([[ -s $STATE/pairing.json ]] && echo yes || echo 'not yet - run pair.sh')"
