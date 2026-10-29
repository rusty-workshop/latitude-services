# latitude-echo-hub

A tiny `systemd --user` service on the Latitude that gives the Muse Echo app
(Echo Show 5) what it needs over the LAN, on port 8094:

- `GET /status`: homelab health from Uptime Kuma's SQLite database (read-only):
  overall `up` / `degraded` / `down` plus each monitor. The Echo shows it as a
  status light.
- `GET /activity`: whether Muse is busy on the Latitude right now (an SSH
  connection from its tailnet address `MUSE_PEER`, or a gadget command in the
  last minute). Only yes/no and the kind, never what it runs. The Echo's blob
  shows a little "busy elsewhere" gear.
- `GET /museecho/latest.json` and `/museecho/app.apk`: the newest Muse Echo
  build, put there by `tools/release.sh` in the muse-echo repo. The Echo checks
  daily and offers the update.

- `GET /homelab`: more detail for the Echo's homelab page (24-hour uptime per
  monitor, disk, memory, load, battery, containers, last latitude-backup run).
- **The Echo itself:**
  - `POST /echo/heartbeat` every minute; `GET /echo/health` answers 200 while
    they arrive and 503 once they stop for 3 minutes. Point an Uptime Kuma
    **HTTP** monitor at `http://192.168.1.158:8094/echo/health` (the LAN IP,
    not localhost: Kuma runs in Docker) and the usual ntfy alerts cover the Echo.
  - `POST /echo/state` (its timers) and `GET /echo/state` for the Wrist app.
  - `POST /echo/inbox` (needs `X-Inbox-Token`, the file `inbox-token` here,
    which latitude-actions reads) queues a timer or an announcement; the Echo
    collects it with `GET /echo/inbox` within a few seconds.
  - `POST /echo/register` + `POST/GET /echo/backup`: the Echo's nightly backup
    (settings, pairing, notes...), last 7 kept in `echo-backups/` (mode 600).
    The first Echo key registered is trusted; after reflashing the Echo run
    `python3 ~/.local/share/echo-hub/echo_hub.py reset-echo-key` and then tap
    Restore in its setup.
- `GET /photos`, `GET /photos/<name>`: the Syncthing folder "Echo Photos"
  (`~/latitude-syncthing/sync/echo-photos`, receive-only here, send-only on the
  laptop at `~/Pictures/Echo Frame`), resized to 1280 px JPEGs for the frame.

- `GET /settings`: a settings page for the Echo, to open from the laptop
  (`http://192.168.1.158:8094/settings`). The Echo posts its settings to
  `/echo/settings_state`; the page reads `GET /echo/settings` and POSTs changes
  back (with an `X-Requested-With: echo-settings` header, which other websites
  can't send cross-site), and the Echo picks them up through its inbox.

Tests: `python3 -m unittest test_echo_hub`.

Install on the Latitude: `bash install.sh`, then allow the home subnet only:
`sudo ufw allow from 192.168.1.0/24 to any port 8094 proto tcp`.

AGPL-3.0-or-later.
