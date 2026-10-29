#!/usr/bin/env bash
# Pair the server as a Muse gadget from THIS machine's Bluetooth (the server
# needs none), then hand the pairing to the server over SSH.
#
#   ./pair.sh [ssh-host]        default host: latitude
#
# Needs: bluez (ExchangeMTU = 256 under [GATT] in /etc/bluetooth/main.conf),
# python-dbus, python-gobject, python-cryptography, and an SDK token in
# ./sdk_token or ~/Projects/muse-echo/sdk_token.
set -euo pipefail
HOST=${1:-latitude}
TOKEN_FILE=$( [[ -s sdk_token ]] && echo sdk_token || echo "$HOME/Projects/muse-echo/sdk_token" )
SDK=$(mktemp -d)/sdk
STATE=$(mktemp -d)
chmod 700 "$STATE"
trap 'rm -rf "$STATE" "$(dirname "$SDK")"' EXIT

git clone -q https://github.com/facebookincubator/muse-gadget-sdk "$SDK"
cp "$TOKEN_FILE" "$STATE/sdk_token"
echo "In the Muse app: Settings > Devices > Add device, and pick the MuseGadget name below."
MUSEGADGET_STATE_DIR=$STATE PYTHONPATH=$SDK/linux/src python3 -m musegadget pair
[[ -s $STATE/pairing.json ]] || { echo "Not paired." >&2; exit 1; }

ssh "$HOST" 'mkdir -p ~/.local/state/musegadget && chmod 700 ~/.local/state/musegadget'
scp -q "$STATE/identity.json" "$STATE/pairing.json" "$STATE/sdk_token" "$HOST:.local/state/musegadget/"
ssh "$HOST" 'chmod 600 ~/.local/state/musegadget/*; systemctl --user restart musegadget.service'
echo "Paired $HOST; the musegadget service there is restarting."
