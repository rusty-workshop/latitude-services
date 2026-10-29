# latitude-lookout

Data feed for the [Lookout](https://github.com/rusty-workshop/tv-lookout) display, on the Latitude as
user service `lookout-feed.service`, port 8473 (LAN only via ufw).

- `GET /api/feed`: weather, air quality and UV (Open-Meteo, Jacksonville),
  National Weather Service alerts, the next visible ISS pass (from
  echo-dashboard-solo's `/iss-passes`, key read from `~/dashboard-server/env`),
  the Lichess daily puzzle (via gambit), service status
  (docker containers + user units), latest restic backup, DVD ripper state
  from ARM's database and logs, and Lichess games from the gambit server.
  AdGuard's blocked count for today and top blocked domain (counted from its
  root-only query log with `sudo -n`, reading only new bytes after the first
  pass, since the API needs the admin login), Wikipedia's "on this day"
  (one event per day), and countdowns.
- Also in the feed: the Latitude's vitals (coretemp, load, memory, `~/media`
  free space, uptime), internet speed history from `speedtests.jsonl`
  (written by `speedtest-log.sh` via `speedtest-log.timer`, every 3 hours,
  `speedtest-cli`), and Lichess rating history per speed rebuilt from rated
  games (the official rating-history API is empty while ratings are
  provisional), using gambit's `LICHESS_TOKEN`.
- Rocket launches from Launch Library 2 (free tier: 15 calls/hour, so cached
  an hour): the next Florida launches (Cape Canaveral SFS + Kennedy, location
  ids 12/27) with honest "NET <month/year>" labels for vague dates, and the
  next exact-time launch anywhere.
- `GET /api/planes`: aircraft within 40 nm from adsb.lol (airplanes.live now
  requires contacting them), with routes/airlines for the nearest six from
  adsbdb (cached 12 h per callsign, at most 4 new lookups per refresh).
- `GET /board/<gameId>.png`: a rendered board for one of those games.
- `GET /api/tv` + `GET /tv.png`: the live Lichess TV game, kept current by a
  background thread on `lichess.org/api/tv/feed`. The app polls these every
  3 seconds while its Live page is showing.
- `GET /settings`: a phone-friendly page (not an app) to choose which
  Lookout tabs show, their order, seconds per page and the start page;
  saved to `settings.json` and served in the feed as `settings`.
- Tides from NOAA CO-OPS station 8720218 (Mayport): next highs/lows and the
  latest observed level vs. predicted (surge), cached 10 min.
- `GET /countdowns`: a phone-friendly page to add/remove countdown dates
  (`POST /api/countdowns`, `DELETE /api/countdowns/<id>`), stored in
  `~/latitude-lookout/countdowns.json`. Yearly ones roll over automatically.
- `GET /puzzle.png`: the daily puzzle's starting position.

Each source is cached (weather 10 min, services/chess 1 min, backup 15 min,
ripper 20 s) and fails soft, so one broken source doesn't break the screen.
It runs under gambit's venv (`~/lichess-watch/.venv`) for python-chess and
cairosvg, and reads gambit's `WATCH_KEY` from `~/lichess-watch/gambit.env`.
The services shown are the `SERVICES` list at the top of `lookout-feed.py`.

## Collectors

- `lightning.py` (`lookout-lightning.service`, system python + h5py): NOAA
  GOES-19 GLM flashes within 60 nm from the public `noaa-goes19` S3 bucket
  (a ~340 KB file every 20 s), fetched **only** while `.lightning-wanted` was
  touched in the last 2 minutes (the feed touches it on each `/api/planes`),
  kept 15 minutes in `lightning.json`.
- `ships.py` (`lookout-ships.service`, system python + websockets):
  aisstream.io websocket for a box around JAXPORT and the river mouth,
  written to `ships.json` every 10 s. Needs the free key in
  `.aisstream-key` (mode 600); idles without it.
- `planets.py`: run by the feed under echo-dashboard-solo's venv (Skyfield)
  with JPL DE421 (`de421.bsp`, downloaded once, not in git); cached an hour.

## Deploy

```bash
mkdir -p ~/latitude-lookout && cp lookout-feed.py ~/latitude-lookout/
cp systemd/lookout-feed.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now lookout-feed
sudo ufw allow from 192.168.1.0/24 to any port 8473 proto tcp comment 'lookout roku feed'
```

## License

AGPL-3.0-or-later
