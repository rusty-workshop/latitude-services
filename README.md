# latitude-services

The server half of a few of my projects. Everything here runs on **the
Latitude**, a retired Dell Latitude laptop that lives on a shelf and never
sleeps, as `systemd --user` services. Nothing talks to a cloud I don't
control; the LAN does most of the work and Tailscale covers the rest.

| Folder | What it feeds | Port |
|---|---|---|
| [`lookout-feed/`](lookout-feed) | [Lookout](https://github.com/rusty-workshop/tv-lookout), the wall display on the TV (and its [Roku sibling](https://github.com/rusty-workshop/roku-lookout)): weather, homelab health, chess, planes overhead, launches, the night sky | 8473 |
| [`echo-hub/`](echo-hub) | [Muse Echo](https://github.com/rusty-workshop/muse-echo) on the Echo Show and the [Puff phone app](https://github.com/rusty-workshop/puff-phone): status, photos, backups, app updates, the phone API | 8094 |
| [`muse-gadget/`](muse-gadget) | Runs Meta's Muse Gadget Linux SDK so Muse can run commands on the Latitude, and forwards homelab alerts to Muse so the Echo can announce them | n/a |

Each folder has its own README with the full details, install steps and
tests. They were separate repos on my machine; this is them bundled
together so the dependencies of the public projects live in one place.

## Configuration

Secrets and machine-specific values are never in git. Each service reads
them from an env file or a gitignored file next to it, described in that
service's README. For `echo-hub`, set these in its unit's environment:

- `ECHO_HUB_TAILNET_URL`: the hub's Tailscale address, e.g.
  `https://latitude.<tailnet>.ts.net:9094` (offered to the phone app when it
  is away from home)
- `MUSE_PEER`: Muse's tailnet IP, used only to answer "is Muse busy here?"

## License

AGPL-3.0-or-later. The Muse Gadget SDK itself is Apache-2.0 and is fetched
by `muse-gadget/install.sh`, not vendored here.
