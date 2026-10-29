# latitude-muse-gadget

Runs Meta's [Muse Gadget Linux SDK](https://github.com/facebookincubator/muse-gadget-sdk)
on the Latitude as a `systemd --user` service for `rusty` (no sudo), so Muse
can run commands there and programs there can message Muse:

```sh
musegadget send-user-msg "Nightly backup failed"
musegadget send-user-msg --session-id 6f1c2d4e-0b7a-4c3e-9f5d-2a8b1e0c7d93 "posted to a side chat"
```

- `install.sh` (run on the server): pinned SDK checkout, private venv with
  `websockets`, the `musegadget` wrapper in `~/.local/bin`, and the user unit.
- `executor-user-session.patch`: keeps `XDG_RUNTIME_DIR`/the session bus and
  `~/.local/bin` in commands' environment, so `systemctl --user` works.
- `pair.sh` (run on the laptop): pairs over the laptop's Bluetooth, then copies
  `identity.json`, `pairing.json` and `sdk_token` to
  `~/.local/state/musegadget` on the server.

## Alerts to Muse

`ntfy_to_muse.py` (user service `ntfy-to-muse`) subscribes to the homelab's
ntfy topic and forwards real alerts with `send-user-msg`, asking Muse what
they mean; the Echo Show announces Muse's answer. Priority 4-5 always, plus
priority-3 recoveries; titles in `SKIP_TITLES` (the Daily Digest) never.
Duplicates within 10 min are dropped, at most 10 an hour, and it resumes
from the last seen alert after a restart. Config (not in git):
`~/.config/latitude-muse-gadget/ntfy.env` with `NTFY_TOPIC=…`.

Tests: `python3 -m unittest test_ntfy_to_muse`.

Paired 2026-10-05 as `homelink-0b95ce`. Commands run with rusty's full
permissions (wheel, docker).

Apache-2.0 SDK; these scripts AGPL-3.0-or-later.
