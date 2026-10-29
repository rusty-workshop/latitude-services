#!/usr/bin/env python3
"""Data feed for the Lookout Roku channel (roku-lookout).

GET /api/feed         everything the three screens show, as one JSON object
GET /board/<id>.png   a rendered chessboard for one of Rusty's Lichess games
GET /puzzle.png       the Lichess daily puzzle's starting position
GET /api/planes       aircraft within 40 nm plus recent lightning (polled every 10 s by the Sky page)
GET /api/ships        vessels around JAXPORT (polled every 10 s by the Port page)
GET /api/tv           the live Lichess TV game (polled every few seconds by the app)
GET /tv.png           its current position
GET /countdowns       page to add/remove countdown dates (API: /api/countdowns)
GET /settings         page to pick which Lookout tabs rotate, their order and timing (API: /api/settings)

Runs on the Latitude with gambit's venv (it has python-chess + cairosvg for
the board images). Every source is cached and fails soft: a broken source
shows as missing on the TV instead of breaking the whole feed.
"""
import json
import shutil
import os
import pathlib
import re
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from collections import Counter
from datetime import date, timedelta

import cairosvg
import chess
import chess.svg

PORT = 8473
HOME = pathlib.Path.home()
LAT, LON = 30.33218, -81.65565  # Jacksonville, from the Echo dashboard's saved location
GAMBIT = "http://127.0.0.1:8460"
DASHBOARD = "http://127.0.0.1:8420"  # echo-dashboard-solo's server, for ISS passes
ARM_DB = HOME / "latitude-arm/home/db/arm.db"
ARM_LOGS = HOME / "latitude-arm/logs"
RESTIC_REPO = HOME / "Backups/restic-latitude"
RESTIC_PASSWORD = HOME / ".config/latitude-backup/password"
QUERYLOG = HOME / "latitude-adguard/work/data/querylog.json"  # root-only; read with sudo
COUNTDOWNS = HOME / "latitude-lookout/countdowns.json"
SETTINGS = HOME / "latitude-lookout/settings.json"
# Every Lookout page, in the app's built-in order: (key, label).
PAGES = [("weather", "Weather"), ("today", "Today"), ("homelab", "Homelab"), ("server", "Server"),
         ("chess", "Chess"), ("live", "Live"), ("sky", "Sky"), ("airport", "Airport"),
         ("radar", "Radar"), ("space", "Space"), ("tonight", "Tonight")]
SPEEDTESTS = HOME / "latitude-lookout/speedtests.jsonl"  # written by speedtest-log.sh
LIGHTNING = HOME / "latitude-lookout/lightning.json"      # written by lightning.py
LIGHTNING_WANTED = HOME / "latitude-lookout/.lightning-wanted"
SHIPS = HOME / "latitude-lookout/ships.json"              # written by ships.py
AIS_KEY = HOME / "latitude-lookout/.aisstream-key"
MEDIA = HOME / "media"
UA = {"User-Agent": "lookout-feed (Rusty's homelab display)"}

# What the status screen lists: (label, kind, name)
SERVICES = [
    ("Jellyfin", "docker", "jellyfin"),
    ("DVD ripper", "docker", "arm"),
    ("AdGuard", "docker", "adguardhome"),
    ("Vaultwarden", "docker", "vaultwarden"),
    ("Uptime Kuma", "docker", "uptime-kuma"),
    ("Syncthing", "docker", "syncthing"),
    ("PrivateBin", "docker", "privatebin"),
    ("Change detection", "docker", "changedetection"),
    ("Guestbook", "docker", "guestbook-api"),
    ("Judgment bot", "user", "judgment-bot"),
    ("Echo hub", "user", "echo-hub"),
    ("Dashboard server", "user", "dashboard-server"),
    ("Gambit", "user", "gambit"),
    ("Watch actions", "user", "latitude-actions"),
    ("Muse gadget", "user", "musegadget"),
    ("Screensaver", "user", "screensaver-server"),
]

WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog",
       48: "Fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
       61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
       67: "Freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
       77: "Snow grains", 80: "Showers", 81: "Showers", 82: "Heavy showers",
       85: "Snow showers", 86: "Snow showers", 95: "Thunderstorms",
       96: "Thunderstorms, hail", 99: "Thunderstorms, hail"}

_cache = {}
_lock = threading.Lock()


def cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    try:
        value = fn()
        stamp = time.time()
    except Exception as e:  # keep the rest of the feed alive
        print(f"{key}: {e!r}", flush=True)
        value = hit[1] if hit else None
        # Retry in about a minute instead of holding the failure for the full ttl.
        stamp = time.time() - ttl + min(ttl, 60)
    with _lock:
        _cache[key] = (stamp, value)
    return value


def get_json(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def weather():
    q = urllib.parse.urlencode({
        "latitude": LAT, "longitude": LON, "timezone": "America/New_York",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,weather_code,precipitation_probability",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,sunrise,sunset,precipitation_probability_max,uv_index_max",
        "forecast_days": 3,
    })
    d = get_json("https://api.open-meteo.com/v1/forecast?" + q)
    c, day, hr = d["current"], d["daily"], d["hourly"]
    now = c["time"][:13]
    start = next((i for i, t in enumerate(hr["time"]) if t[:13] >= now), 0)
    return {
        "temp": round(c["temperature_2m"]), "feels": round(c["apparent_temperature"]),
        "humidity": c["relative_humidity_2m"], "wind": round(c["wind_speed_10m"]),
        "summary": WMO.get(c["weather_code"], "—"),
        "high": round(day["temperature_2m_max"][0]), "low": round(day["temperature_2m_min"][0]),
        "rain": day["precipitation_probability_max"][0],
        "sunrise": day["sunrise"][0][11:], "sunset": day["sunset"][0][11:],
        "uvMax": round(day["uv_index_max"][0]),
        "hours": [{"time": hr["time"][i][11:16], "temp": round(hr["temperature_2m"][i]),
                   "summary": WMO.get(hr["weather_code"][i], "—"),
                   "rain": hr["precipitation_probability"][i]}
                  for i in range(start + 1, start + 13, 2)],
        "days": [{"date": day["time"][i], "high": round(day["temperature_2m_max"][i]),
                  "low": round(day["temperature_2m_min"][i]),
                  "summary": WMO.get(day["weather_code"][i], "—")} for i in (1, 2)],
    }


def air():
    q = urllib.parse.urlencode({"latitude": LAT, "longitude": LON, "timezone": "America/New_York",
                                "current": "us_aqi,uv_index"})
    c = get_json("https://air-quality-api.open-meteo.com/v1/air-quality?" + q)["current"]
    aqi, uv = round(c["us_aqi"]), round(c["uv_index"])
    aqi_label = next(label for limit, label in [
        (50, "Good"), (100, "Moderate"), (150, "Unhealthy for sensitive groups"),
        (200, "Unhealthy"), (300, "Very unhealthy"), (10**9, "Hazardous")] if aqi <= limit)
    uv_label = next(label for limit, label in [
        (2, "Low"), (5, "Moderate"), (7, "High"), (10, "Very high"), (10**9, "Extreme")] if uv <= limit)
    return {"aqi": aqi, "aqiLabel": aqi_label, "uv": uv, "uvLabel": uv_label}


def alerts():
    d = get_json(f"https://api.weather.gov/alerts/active?point={LAT},{LON}",
                 {"User-Agent": "lookout-feed (Rusty's homelab)", "Accept": "application/geo+json"})
    rank = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
    out = []
    for f in d.get("features", []):
        p = f["properties"]
        ends = p.get("ends") or p.get("expires")
        until = datetime.fromisoformat(ends).astimezone().strftime("%a %-I:%M %p") if ends else None
        out.append({"event": p["event"], "severity": p.get("severity", "Unknown"), "until": until})
    return sorted(out, key=lambda a: rank.get(a["severity"], 9))


def iss():
    key = ""
    for line in (HOME / "dashboard-server/env").read_text().splitlines():
        if line.startswith("DASHBOARD_SERVER_KEY="):
            key = line.split("=", 1)[1].strip().strip('"')
    d = get_json(f"{DASHBOARD}/iss-passes?lat={LAT}&lon={LON}&hours=72",
                 {"X-Dashboard-Key": key}, timeout=60)
    now = datetime.now(timezone.utc)
    upcoming = [p for p in d["passes"] if datetime.fromisoformat(p["set"]) > now]
    if not upcoming:
        return None
    p = upcoming[0]
    rise = datetime.fromisoformat(p["rise"]).astimezone()
    return {"when": rise.strftime("%a %-I:%M %p"), "maxElevation": round(p["max_elevation"]),
            "minutes": max(1, round(p["duration_seconds"] / 60))}


def puzzle():
    h = {"Authorization": f"Bearer {gambit_key()}"}
    p = get_json(GAMBIT + "/puzzle/daily", h, timeout=30)
    return {"id": p["id"], "rating": p["rating"], "color": p["color"], "fen": p["start"],
            "themes": p.get("themes", [])[:3]}


def services():
    out = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"],
                         capture_output=True, text=True, timeout=10).stdout
    docker = {}
    for line in out.splitlines():
        name, state, status = line.split("\t")
        docker[name] = state == "running" and "unhealthy" not in status
    result = []
    for label, kind, name in SERVICES:
        if kind == "docker":
            ok = docker.get(name, False)
        else:
            ok = subprocess.run(["systemctl", "--user", "is-active", "--quiet", name],
                                timeout=10).returncode == 0
        result.append({"name": label, "ok": ok})
    return result


def backup():
    env = dict(os.environ, RESTIC_REPOSITORY=str(RESTIC_REPO),
               RESTIC_PASSWORD_FILE=str(RESTIC_PASSWORD))
    out = subprocess.run(["restic", "snapshots", "--json", "--tag", "latitude-daily", "--no-lock"],
                         capture_output=True, text=True, timeout=60, env=env).stdout
    newest = max(datetime.fromisoformat(s["time"]) for s in json.loads(out))
    age_h = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
    return {"last": newest.astimezone().strftime("%a %-I:%M %p"), "ok": age_h < 30,
            "ageHours": round(age_h, 1)}


def ripper():
    db = sqlite3.connect(f"file:{ARM_DB}?mode=ro", uri=True)
    rows = db.execute("select title, status, logfile, job_id from job order by job_id desc limit 1").fetchall()
    db.close()
    if not rows:
        return {"state": "idle"}
    title, status, logfile, job_id = rows[0]
    pretty = (title or "Disc").replace("--", ": ").replace("-", " ")
    if status in ("success", "fail"):
        return {"state": "idle", "last": pretty, "lastOk": status == "success"}
    progress = None
    log = ARM_LOGS / (logfile or "")
    if log.is_file():
        with log.open("rb") as f:
            f.seek(max(0, log.stat().st_size - 4000))
            hits = re.findall(rb"Encoding: task \d+ of \d+, ([\d.]+) %", f.read())
        if hits:
            return {"state": "compressing", "title": pretty, "progress": float(hits[-1])}
    prog = ARM_LOGS / "progress" / f"{job_id}.log"
    if prog.is_file():
        m = re.findall(r"PRGV:\d+,(\d+),(\d+)", prog.read_text(errors="ignore"))
        if m:
            progress = round(100 * int(m[-1][0]) / int(m[-1][1]), 1)
    return {"state": "copying", "title": pretty, "progress": progress}


def gambit_key():
    for line in (HOME / "lichess-watch/gambit.env").read_text().splitlines():
        if line.startswith("WATCH_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("no WATCH_KEY")


def chess_feed():
    h = {"Authorization": f"Bearer {gambit_key()}"}
    home = get_json(GAMBIT + "/home", h, timeout=20)
    summary = get_json(GAMBIT + "/summary", h, timeout=20)
    games = [{
        "id": g["id"], "color": g["color"], "fen": g["fen"], "lastMove": g.get("lastMove"),
        "myTurn": g["myTurn"], "opponent": g["opponent"]["name"],
        "opponentRating": g["opponent"].get("rating"), "secondsLeft": g.get("secondsLeft"),
    } for g in home.get("games", [])]
    return {"me": home["me"]["name"], "rating": home["me"].get("corr"),
            "puzzleRating": home["me"].get("puzzle"), "puzzleSolved": summary.get("puzzleSolved"),
            "challenges": len(home.get("challengesIn", [])), "games": games}


BOARD_COLORS = {"square light": "#f0d9b5", "square dark": "#b58863",
                "square light lastmove": "#cdd26a", "square dark lastmove": "#aaa23a"}


def board_png(game):
    board = chess.Board(game["fen"])
    last = chess.Move.from_uci(game["lastMove"]) if game.get("lastMove") else None
    svg = chess.svg.board(board, orientation=chess.WHITE if game["color"] == "white" else chess.BLACK,
                          lastmove=last, size=540, coordinates=False, colors=BOARD_COLORS)
    return cairosvg.svg2png(bytestring=svg.encode())


# ---- Latitude vitals and internet speed ------------------------------------------

def vitals():
    temp = None
    for hw in pathlib.Path("/sys/class/hwmon").iterdir():
        if (hw / "name").read_text().strip() == "coretemp":
            temp = round(int((hw / "temp1_input").read_text()) / 1000)  # CPU package
    load = float(pathlib.Path("/proc/loadavg").read_text().split()[0])
    mem = {}
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":")
        mem[k] = int(v.split()[0])
    used_gb = (mem["MemTotal"] - mem["MemAvailable"]) / 1024 / 1024
    disk = shutil.disk_usage(MEDIA)
    up = float(pathlib.Path("/proc/uptime").read_text().split()[0])
    return {"cpuTemp": temp, "load": round(load, 2), "cores": os.cpu_count(),
            "memUsedGb": round(used_gb, 1), "memTotalGb": round(mem["MemTotal"] / 1024 / 1024),
            "diskFreeGb": round(disk.free / 1e9), "diskTotalGb": round(disk.total / 1e9),
            "uptimeDays": round(up / 86400, 1)}


def speed():
    try:
        lines = SPEEDTESTS.read_text().splitlines()
    except OSError:
        return None
    week = time.time() - 7 * 86400
    tests = [t for t in (json.loads(l) for l in lines if l.strip()) if t["t"] >= week]
    if not tests:
        return None
    last = tests[-1]
    return {"down": last["down"], "up": last["up"], "ping": last["ping"],
            "at": datetime.fromtimestamp(last["t"]).strftime("%a %-I:%M %p"),
            "history": [{"t": t["t"], "down": t["down"], "up": t["up"]} for t in tests]}


# ---- Lichess rating history ----------------------------------------------------------
# Lichess's rating-history API comes back empty while every rating is still
# provisional, so rebuild it from rated games: each one records the rating
# before and the change after.

def lichess_token():
    for line in (HOME / "lichess-watch/gambit.env").read_text().splitlines():
        if line.startswith("LICHESS_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("no LICHESS_TOKEN")


def chess_history():
    req = urllib.request.Request(
        "https://lichess.org/api/games/user/fllocalhostedcat?max=60&rated=true&moves=false",
        headers={**UA, "Accept": "application/x-ndjson", "Authorization": f"Bearer {lichess_token()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        games = [json.loads(l) for l in r if l.strip()]
    series, results = {}, []
    for g in reversed(games):  # oldest first
        color = "white" if (g["players"]["white"].get("user") or {}).get("id") == "fllocalhostedcat" else "black"
        me = g["players"][color]
        if me.get("rating") is None:
            continue
        series.setdefault(g["perf"], []).append(me["rating"] + me.get("ratingDiff", 0))
        w = g.get("winner")
        results.append({"result": "draw" if not w else "win" if w == color else "loss", "perf": g["perf"]})
    return {"series": [{"perf": k, "points": v} for k, v in series.items()], "recent": results[-10:]}


# ---- Rocket launches -------------------------------------------------------------------
# Launch Library 2 (The Space Devs). The free tier allows 15 calls an hour;
# this makes 2 per refresh and is cached for an hour.

FLORIDA_SITES = {12, 27}  # Cape Canaveral SFS, Kennedy Space Center


def launches():
    # Two small queries; one big one (limit=40) was slow enough to time out.
    base = "https://ll.thespacedevs.com/2.3.0/launches/upcoming/?ordering=net&mode=normal"
    fl = get_json(base + "&limit=3&location__ids=" + ",".join(map(str, sorted(FLORIDA_SITES))), UA, timeout=45)
    world = get_json(base + "&limit=10", UA, timeout=45)

    def shape(l):
        utc = datetime.fromisoformat(l["net"].replace("Z", "+00:00"))
        net = utc.astimezone()
        precision = ((l.get("net_precision") or {}).get("name") or "Second")
        exact = precision in ("Second", "Minute", "Hour")
        florida = ((l.get("pad") or {}).get("location") or {}).get("id") in FLORIDA_SITES
        # Vague dates are stored as the start of the period in UTC; label them
        # by that period instead of a fake exact day.
        if precision == "Day":
            when = "NET " + utc.strftime("%a %b %-d")
        elif precision == "Month":
            when = "NET " + utc.strftime("%B")
        elif not exact:
            when = "NET " + utc.strftime("%Y")
        else:
            when = net.strftime("%a %b %-d · %-I:%M %p")
        if not florida:
            sky = ""
        elif not exact:
            sky = "Time not set yet"
        else:
            sky = ("Night launch: look south-southeast" if net.hour >= 19 or net.hour < 6
                   else "Twilight: possible jellyfish plume" if net.hour in (6, 18)
                   else "Daytime launch")
        cfg = ((l.get("rocket") or {}).get("configuration") or {})
        mission = (l.get("mission") or {}).get("name") or l["name"].split("|")[-1].strip()
        img = l.get("image")
        return {"mission": mission, "rocket": cfg.get("full_name") or cfg.get("name"),
                "provider": (l.get("launch_service_provider") or {}).get("name"),
                "pad": (l.get("pad") or {}).get("name"), "net": l["net"], "exact": exact,
                "when": when, "sky": sky, "status": (l.get("status") or {}).get("abbrev"),
                "image": img.get("image_url") if isinstance(img, dict) else img}

    florida = [shape(l) for l in fl.get("results", [])]
    anywhere = next((l for l in world.get("results", [])
                     if (l.get("net_precision") or {}).get("name") in ("Second", "Minute", "Hour")), None)
    nxt = shape(anywhere) if anywhere else None
    if nxt:
        nxt["where"] = ((anywhere.get("pad") or {}).get("location") or {}).get("name")
    return {"florida": florida, "next": nxt}


# ---- Planes overhead ---------------------------------------------------------------------
# Positions from adsb.lol (open ADS-B data, 40 nm around home); routes and
# airlines from adsbdb, looked up for the nearest few flights and cached.

_routes = {}


def route(callsign):
    hit = _routes.get(callsign)
    if hit and time.time() - hit[0] < 12 * 3600:
        return hit[1]
    info = None
    try:
        r = get_json(f"https://api.adsbdb.com/v0/callsign/{callsign}", UA, timeout=6)["response"]["flightroute"]
        info = {"airline": (r.get("airline") or {}).get("name"),
                "from": (r.get("origin") or {}).get("iata_code"),
                "fromCity": (r.get("origin") or {}).get("municipality"),
                "to": (r.get("destination") or {}).get("iata_code"),
                "toCity": (r.get("destination") or {}).get("municipality"),
                "box": [(r.get("origin") or {}).get("latitude"), (r.get("origin") or {}).get("longitude"),
                        (r.get("destination") or {}).get("latitude"), (r.get("destination") or {}).get("longitude")]}
    except Exception:
        pass  # unknown callsign (private/military): remember the miss too
    _routes[callsign] = (time.time(), info)
    return info


def plausible(info, plane):
    """Airlines reuse flight numbers across legs, so adsbdb can return the
    wrong leg (SWA839 "Los Angeles -> Oakland" over Jacksonville). Keep the
    route only if the plane is within a few degrees of the box between the two
    airports; otherwise show just the airline."""
    if not info:
        return info
    out = {k: v for k, v in info.items() if k != "box"}
    box = info.get("box") or []
    if len(box) == 4 and None not in box:
        lat1, lon1, lat2, lon2 = box
        margin = 4.0
        if not (min(lat1, lat2) - margin <= plane["lat"] <= max(lat1, lat2) + margin and
                min(lon1, lon2) - margin <= plane["lon"] <= max(lon1, lon2) + margin):
            out.update({"from": None, "fromCity": None, "to": None, "toCity": None})
    return out


def planes():
    d = get_json(f"https://api.adsb.lol/v2/point/{LAT}/{LON}/40", UA, timeout=10)
    out = []
    for a in d.get("ac", []):
        if a.get("lat") is None:
            continue
        alt = a.get("alt_baro")
        out.append({"hex": a.get("hex"), "callsign": (a.get("flight") or "").strip() or a.get("r") or a.get("hex"),
                    "type": a.get("t"), "alt": alt if isinstance(alt, (int, float)) else 0,
                    "ground": alt == "ground", "speed": round(a.get("gs") or 0), "track": a.get("track") or 0,
                    "vrate": a.get("baro_rate") or a.get("geom_rate") or 0,
                    "lat": a["lat"], "lon": a["lon"], "dist": round(a.get("dst") or 0, 1)})
    airborne = sorted((p for p in out if not p["ground"]), key=lambda p: p["dist"])
    lookups = 0
    for p in airborne[:10]:
        if p["callsign"] not in _routes and lookups >= 4:
            continue  # pick up the rest next refresh
        lookups += p["callsign"] not in _routes
        p["route"] = plausible(route(p["callsign"]), p)
    return {"center": [LAT, LON], "rangeNm": 40, "aircraft": out, "nearest": airborne[:10],
            "airport": airport_board(out)}


JAX = (30.4941, -81.6879)  # Jacksonville International (KJAX)


def airport_board(aircraft):
    """Arrivals and departures at JAX, worked out from live positions: low and
    descending toward the field = landing, low and climbing away = departing,
    on the ground within 3 nm = at the airport."""
    rows = []
    for p in aircraft:
        d = nm_between(p["lat"], p["lon"], *JAX)
        if p["ground"]:
            if d <= 3:
                rows.append({**p, "status": "On ground", "jaxNm": round(d, 1), "order": 2})
            continue
        if d > 15 or p["alt"] > 9000:
            continue
        if p["vrate"] <= -300:
            eta = round(d / max(p["speed"], 60) * 60)
            rows.append({**p, "status": "Landing", "eta": eta, "jaxNm": round(d, 1), "order": 0})
        elif p["vrate"] >= 300 and d <= 12:
            rows.append({**p, "status": "Departing", "jaxNm": round(d, 1), "order": 1})
    rows.sort(key=lambda r: (r["order"], r.get("eta", 0), r["jaxNm"]))
    lookups = 0
    for r in rows[:8]:
        if r["callsign"] not in _routes and lookups >= 4:
            continue
        lookups += r["callsign"] not in _routes
        r["route"] = plausible(route(r["callsign"]), r)
    return [{k: v for k, v in r.items() if k != "order"} for r in rows[:8]]


def nm_between(lat1, lon1, lat2, lon2):
    import math
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(a))


# ---- Planets, lightning, ships -------------------------------------------------------

def daylight():
    out = subprocess.run([str(HOME / "dashboard-server/venv/bin/python"),
                          str(HOME / "latitude-lookout/daylight.py")],
                         capture_output=True, text=True, timeout=180, check=True).stdout
    return json.loads(out)


def planets():
    # Skyfield lives in echo-dashboard-solo's venv; run the calculation there.
    out = subprocess.run([str(HOME / "dashboard-server/venv/bin/python"),
                          str(HOME / "latitude-lookout/planets.py")],
                         capture_output=True, text=True, timeout=120, check=True).stdout
    return json.loads(out)


def lightning():
    LIGHTNING_WANTED.touch()  # tells lightning.py someone is looking
    try:
        d = json.loads(LIGHTNING.read_text())
    except (OSError, ValueError):
        return None
    now = time.time()
    flashes = [{"lat": f["lat"], "lon": f["lon"], "age": round(now - f["t"])} for f in d.get("flashes", [])]
    return {"flashes": flashes, "rangeNm": d.get("rangeNm", 60), "active": d.get("active", False)}


def ships():
    if not AIS_KEY.exists():
        return {"configured": False, "ships": []}
    try:
        d = json.loads(SHIPS.read_text())
    except (OSError, ValueError):
        return {"configured": True, "ships": []}
    return {"configured": True, "updated": d.get("updated"), "box": d.get("box"), "ships": d.get("ships", [])}


# ---- Tides ---------------------------------------------------------------------------
# NOAA CO-OPS, Mayport (Bar Pilots Dock), station 8720218: the next highs and
# lows, plus the latest observed level against the prediction for the same
# moment, which shows surge (like during a coastal flood warning).

TIDE_STATION = "8720218"


def tides():
    base = (f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?station={TIDE_STATION}"
            "&time_zone=lst_ldt&units=english&format=json&datum=MLLW&application=lookout")
    hilo = get_json(base + f"&product=predictions&interval=hilo&begin_date={date.today():%Y%m%d}&range=48", UA)
    now = datetime.now()
    upcoming = []
    for p in hilo.get("predictions", []):
        t = datetime.strptime(p["t"], "%Y-%m-%d %H:%M")
        if t >= now:
            upcoming.append({"type": "High" if p["type"] == "H" else "Low",
                             "time": t.strftime("%-I:%M %p"), "ft": round(float(p["v"]), 1)})
    surge = None
    try:
        obs = get_json(base + "&product=water_level&date=latest", UA)["data"][-1]
        pred = get_json(base + "&product=predictions&date=latest", UA)["predictions"]
        match = next((p for p in pred if p["t"] == obs["t"]), pred[-1])
        surge = {"observed": round(float(obs["v"]), 1), "predicted": round(float(match["v"]), 1),
                 "diff": round(float(obs["v"]) - float(match["v"]), 1),
                 "at": datetime.strptime(obs["t"], "%Y-%m-%d %H:%M").strftime("%-I:%M %p")}
    except Exception as e:
        print(f"tides observed: {e!r}", flush=True)
    return {"station": "Mayport", "next": upcoming[:4], "now": surge}


# ---- Settings -------------------------------------------------------------------------

def load_settings():
    try:
        s = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        s = {}
    known = [k for k, _ in PAGES]
    order = [p for p in s.get("pages", []) if p.get("key") in known]
    order += [{"key": k, "enabled": True} for k in known if k not in {p["key"] for p in order}]
    if not any(p["enabled"] for p in order):
        order[0]["enabled"] = True
    start = s.get("startPage") if s.get("startPage") in known else "weather"
    return {"pages": order, "rotateSec": max(10, min(600, int(s.get("rotateSec", 30)))), "startPage": start}


SETTINGS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lookout Settings</title>
<style>
:root { --bg:#0f1115; --card:#181b22; --text:#e8e8ec; --muted:#9a9cab; --accent:#aa5cc3; }
* { box-sizing:border-box; }
body { margin:0 auto; max-width:560px; padding:20px 16px; background:var(--bg); color:var(--text);
  font:16px/1.4 system-ui, sans-serif; }
h1 { font-size:22px; margin:0 0 4px; } p { color:var(--muted); margin:0 0 18px; }
.box { background:var(--card); border:1px solid #2c303b; border-radius:14px; padding:14px; margin-bottom:16px; }
ul { list-style:none; padding:0; margin:0; }
li { display:flex; align-items:center; gap:10px; padding:10px 4px; border-bottom:1px solid #23262e; }
li:last-child { border-bottom:0; }
li.off .name { color:var(--muted); text-decoration:line-through; }
.name { flex:1; font-size:18px; }
button { background:#22252e; color:var(--text); border:1px solid #2c303b; border-radius:9px;
  min-width:42px; padding:8px 10px; font:inherit; cursor:pointer; }
button.save { background:var(--accent); border-color:var(--accent); width:100%; padding:12px; }
label.row { display:flex; justify-content:space-between; align-items:center; gap:10px; margin:6px 0; }
select, input { background:#0f1115; color:var(--text); border:1px solid #2c303b; border-radius:9px;
  padding:8px; font:inherit; }
input[type=checkbox] { width:22px; height:22px; accent-color:var(--accent); }
#msg { color:var(--muted); text-align:center; margin-top:10px; min-height:1.4em; }
</style></head><body>
<h1>Lookout</h1>
<p>Which tabs show and rotate on the TV, in what order. Changes reach the TV within a minute.</p>
<div class="box"><ul id="list"></ul></div>
<div class="box">
  <label class="row">Seconds per page <input id="rotate" type="number" min="10" max="600" style="width:90px"></label>
  <label class="row">Start on <select id="start"></select></label>
</div>
<button class="save" id="save">Save</button>
<div id="msg"></div>
<script>
const $ = s => document.querySelector(s);
let pages = [], labels = {};
function render() {
  const ul = $("#list"); ul.innerHTML = "";
  pages.forEach((p, i) => {
    const li = document.createElement("li"); if (!p.enabled) li.className = "off";
    li.innerHTML = '<input type="checkbox"><span class="name"></span><button>↑</button><button>↓</button>';
    const [cb, name, up, down] = [li.querySelector("input"), li.querySelector(".name"), ...li.querySelectorAll("button")];
    cb.checked = p.enabled; name.textContent = labels[p.key];
    cb.onchange = () => { p.enabled = cb.checked; render(); };
    up.onclick = () => { if (i > 0) { [pages[i-1], pages[i]] = [pages[i], pages[i-1]]; render(); } };
    down.onclick = () => { if (i < pages.length - 1) { [pages[i+1], pages[i]] = [pages[i], pages[i+1]]; render(); } };
    ul.append(li);
  });
  const sel = $("#start"), cur = sel.value;
  sel.innerHTML = pages.filter(p => p.enabled).map(p => `<option value="${p.key}">${labels[p.key]}</option>`).join("");
  if (cur) sel.value = cur;
}
async function load() {
  const d = await fetch("/api/settings").then(r => r.json());
  labels = d.labels; pages = d.pages; $("#rotate").value = d.rotateSec; render(); $("#start").value = d.startPage;
}
$("#save").onclick = async () => {
  if (!pages.some(p => p.enabled)) { $("#msg").textContent = "Keep at least one page on."; return; }
  await fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({pages, rotateSec: +$("#rotate").value, startPage: $("#start").value})});
  $("#msg").textContent = "Saved. The TV picks it up within a minute.";
};
load();
</script></body></html>
"""


# ---- AdGuard ---------------------------------------------------------------
# AdGuard's API needs its admin login, so count straight from its query log
# instead. The log is ~10 MB/day and only ever appended to, so after the first
# read only the new bytes are processed. Only totals and the top blocked
# domain leave this function.

_ag = {"day": None, "offset": None, "total": 0, "blocked": 0, "top": Counter(), "midline": False}


def adguard():
    today = date.today()
    # Log timestamps are UTC; today's local midnight in the same format.
    start = datetime.combine(today, datetime.min.time()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    size = int(subprocess.run(["sudo", "-n", "stat", "-c", "%s", str(QUERYLOG)],
                              capture_output=True, text=True, timeout=10, check=True).stdout)
    if _ag["day"] != today or _ag["offset"] is None or _ag["offset"] > size:
        offset = max(0, size - 40_000_000)  # well over a day's worth
        _ag.update(day=today, total=0, blocked=0, top=Counter(), offset=offset, midline=offset > 0)
    if size > _ag["offset"]:
        data = subprocess.run(["sudo", "-n", "tail", "-c", f"+{_ag['offset'] + 1}", str(QUERYLOG)],
                              capture_output=True, timeout=60, check=True).stdout
        end = data.rfind(b"\n") + 1  # leave a half-written last line for next time
        # Starting partway into the file lands mid-line; skip to the next one.
        first = data.find(b"\n") + 1 if _ag["midline"] else 0
        _ag["midline"] = False
        for line in data[first:end].splitlines():
            if not line.startswith(b'{"T":"') or line[6:25].decode() < start:
                continue
            q = json.loads(line)
            _ag["total"] += 1
            if (q.get("Result") or {}).get("IsFiltered"):
                _ag["blocked"] += 1
                _ag["top"][q.get("QH", "").rstrip(".")] += 1
        _ag["offset"] += end
    top = _ag["top"].most_common(1)
    return {"total": _ag["total"], "blocked": _ag["blocked"],
            "percent": round(100 * _ag["blocked"] / _ag["total"], 1) if _ag["total"] else 0,
            "topDomain": top[0][0] if top else None, "topCount": top[0][1] if top else 0}


# ---- On this day -------------------------------------------------------------

def on_this_day():
    today = date.today()
    d = get_json(f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/selected/{today.month:02}/{today.day:02}", UA)
    events = d.get("selected", [])
    if not events:
        return None
    e = events[today.toordinal() % len(events)]  # one per day, stable all day
    thumb = next((p["thumbnail"]["source"] for p in e.get("pages", []) if p.get("thumbnail")), None)
    return {"year": e["year"], "yearsAgo": today.year - e["year"], "text": e["text"], "image": thumb}


# ---- Countdowns ----------------------------------------------------------------

def load_countdowns():
    try:
        return json.loads(COUNTDOWNS.read_text())
    except (OSError, ValueError):
        return []


def save_countdowns(items):
    tmp = COUNTDOWNS.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1))
    tmp.replace(COUNTDOWNS)


def next_date(item, today):
    d = date.fromisoformat(item["date"])
    if not item.get("yearly"):
        return d
    for year in (today.year, today.year + 1):
        try:
            nd = d.replace(year=year)
        except ValueError:  # Feb 29 in a non-leap year
            nd = date(year, 3, 1)
        if nd >= today:
            return nd


def countdowns():
    today = date.today()
    out = []
    for item in load_countdowns():
        nd = next_date(item, today)
        if nd >= today:
            out.append({"name": item["name"], "days": (nd - today).days,
                        "date": nd.strftime("%a %b %-d"), "yearly": bool(item.get("yearly"))})
    return sorted(out, key=lambda c: c["days"])


# ---- Lichess TV ----------------------------------------------------------------
# One long-lived stream of Lichess's featured game; the app polls /api/tv.

_tv = {"id": None, "fen": None, "lastMove": None, "orientation": "white", "players": [], "seq": 0}


def tv_watcher():
    while True:
        try:
            req = urllib.request.Request("https://lichess.org/api/tv/feed", headers=UA)
            with urllib.request.urlopen(req, timeout=90) as r:
                for raw in r:
                    if not raw.strip():
                        continue
                    msg = json.loads(raw)
                    d = msg.get("d", {})
                    if msg.get("t") == "featured":
                        _tv.update(id=d.get("id"), orientation=d.get("orientation", "white"),
                                   fen=d.get("fen"), lastMove=None, players=[{
                                       "color": p.get("color"), "rating": p.get("rating"),
                                       "seconds": p.get("seconds"),
                                       "name": ((p.get("user") or {}).get("title", "") + " " +
                                                (p.get("user") or {}).get("name", "?")).strip(),
                                   } for p in d.get("players", [])])
                    elif msg.get("t") == "fen":
                        _tv.update(fen=d.get("fen"), lastMove=d.get("lm"))
                        for p in _tv["players"]:
                            p["seconds"] = d.get("wc" if p["color"] == "white" else "bc", p["seconds"])
                    _tv["seq"] += 1
        except Exception as e:
            print(f"tv: {e!r}", flush=True)
        time.sleep(10)


def tv_png():
    board = chess.BaseBoard(_tv["fen"].split()[0])
    last = chess.Move.from_uci(_tv["lastMove"]) if _tv.get("lastMove") else None
    svg = chess.svg.board(board, orientation=chess.WHITE if _tv["orientation"] == "white" else chess.BLACK,
                          lastmove=last, size=760, coordinates=False, colors=BOARD_COLORS)
    return cairosvg.svg2png(bytestring=svg.encode())


COUNTDOWN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lookout Countdowns</title>
<style>
:root { --bg:#0f1115; --card:#181b22; --text:#e8e8ec; --muted:#9a9cab; --accent:#aa5cc3; }
* { box-sizing:border-box; }
body { margin:0 auto; max-width:640px; padding:20px 16px; background:var(--bg);
  color:var(--text); font:16px/1.4 system-ui, sans-serif; }
h1 { font-size:22px; margin:0 0 4px; } p { color:var(--muted); margin:0 0 18px; }
form, li { background:var(--card); border:1px solid #2c303b; border-radius:14px; }
form { display:grid; gap:10px; padding:14px; margin-bottom:18px; }
input[type=text], input[type=date] { width:100%; background:#0f1115; color:var(--text);
  border:1px solid #2c303b; border-radius:10px; padding:10px; font:inherit; }
button { background:var(--accent); color:#fff; border:0; border-radius:10px; padding:10px 14px;
  font:inherit; cursor:pointer; }
ul { list-style:none; padding:0; margin:0; display:grid; gap:10px; }
li { display:flex; align-items:center; gap:12px; padding:12px 14px; }
li .days { font-size:24px; min-width:64px; text-align:right; }
li .what { flex:1; } li small { color:var(--muted); display:block; }
li button { background:transparent; border:1px solid #3a3e4a; color:var(--muted); }
</style></head><body>
<h1>Countdowns</h1>
<p>Shown on Lookout's Today page, soonest first.</p>
<form id="f">
  <input id="name" type="text" placeholder="What (e.g. Mom's birthday)" required maxlength="40">
  <input id="date" type="date" required>
  <label><input id="yearly" type="checkbox"> Repeats every year (birthdays, holidays)</label>
  <button>Add</button>
</form>
<ul id="list"></ul>
<script>
const $ = s => document.querySelector(s);
async function load() {
  const items = await fetch("/api/countdowns").then(r => r.json());
  $("#list").innerHTML = "";
  for (const it of items) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="days"></span><span class="what"><b></b><small></small></span><button>Remove</button>';
    li.querySelector(".days").textContent = it.days === 0 ? "Today" : it.days + "d";
    li.querySelector("b").textContent = it.name;
    li.querySelector("small").textContent = it.next + (it.yearly ? " · every year" : "");
    li.querySelector("button").onclick = async () => {
      await fetch("/api/countdowns/" + it.id, {method: "DELETE"}); load(); };
    $("#list").append(li);
  }
}
$("#f").onsubmit = async e => {
  e.preventDefault();
  await fetch("/api/countdowns", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name: $("#name").value, date: $("#date").value, yearly: $("#yearly").checked})});
  $("#f").reset(); load();
};
load();
</script></body></html>
"""


def feed():
    c = cached("chess", 60, chess_feed)
    pz = cached("puzzle", 3600, puzzle)
    return {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "weather": cached("weather", 600, weather),
        "air": cached("air", 900, air),
        "alerts": cached("alerts", 300, alerts) or [],
        "iss": cached("iss", 1800, iss),
        "puzzle": pz and {k: v for k, v in pz.items() if k != "fen"} | {"board": f"/puzzle.png?id={pz['id']}"},
        "services": cached("services", 60, services),
        "backup": cached("backup", 900, backup),
        "ripper": cached("ripper", 20, ripper),
        "adguard": cached("adguard", 300, adguard),
        "onThisDay": cached("onthisday", 3600, on_this_day),
        "countdowns": countdowns(),
        "tv": tv_state(),
        "vitals": cached("vitals", 30, vitals),
        "speed": cached("speed", 300, speed),
        "chessHistory": cached("chesshistory", 1800, chess_history),
        "launches": cached("launches", 3600, launches),
        "planets": cached("planets", 3600, planets),
        "tides": cached("tides", 600, tides),
        "settings": load_settings(),
        "daylight": cached(f"daylight-{date.today()}", 86400, daylight),  # recomputed each new day
        "chess": c and {**c, "games": [{k: v for k, v in g.items() if k != "fen"}
                                       | {"board": f"/board/{g['id']}.png?m={g.get('lastMove') or ''}"}
                                       for g in c["games"]]},
    }


def tv_state():
    if not _tv["fen"]:
        return None
    return {"id": _tv["id"], "players": _tv["players"], "orientation": _tv["orientation"],
            "board": f"/tv.png?s={_tv['seq']}"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/feed":
            return self.send(200, feed())
        if path == "/api/planes":
            return self.send(200, {**(cached("planes", 8, planes) or {}), "lightning": lightning()})
        if path == "/api/ships":
            return self.send(200, ships())
        if path == "/api/tv":
            return self.send(200, tv_state())
        if path == "/tv.png":
            if not _tv["fen"]:
                return self.send(404, {"error": "no game yet"})
            return self.send(200, tv_png(), "image/png")
        if path == "/settings":
            return self.send(200, SETTINGS_PAGE.encode(), "text/html; charset=utf-8")
        if path == "/api/settings":
            return self.send(200, {**load_settings(), "labels": dict(PAGES)})
        if path == "/countdowns":
            return self.send(200, COUNTDOWN_PAGE.encode(), "text/html; charset=utf-8")
        if path == "/api/countdowns":
            today = date.today()
            return self.send(200, [{**c, "next": next_date(c, today).strftime("%a %b %-d, %Y"),
                                    "days": (next_date(c, today) - today).days}
                                   for c in sorted(load_countdowns(), key=lambda c: next_date(c, today))])
        if path == "/puzzle.png":
            pz = cached("puzzle", 3600, puzzle)
            if not pz:
                return self.send(404, {"error": "no puzzle"})
            return self.send(200, board_png({"fen": pz["fen"], "color": pz["color"]}), "image/png")
        m = re.fullmatch(r"/board/([\w-]+)\.png", path)
        if m:
            c = cached("chess", 60, chess_feed) or {"games": []}
            game = next((g for g in c["games"] if g["id"] == m[1]), None)
            if not game:
                return self.send(404, {"error": "no such game"})
            return self.send(200, board_png(game), "image/png")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/settings":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            known = {k for k, _ in PAGES}
            pages = [{"key": p["key"], "enabled": bool(p.get("enabled"))}
                     for p in body.get("pages", []) if p.get("key") in known]
            tmp = SETTINGS.with_suffix(".tmp")
            tmp.write_text(json.dumps({"pages": pages, "rotateSec": body.get("rotateSec", 30),
                                       "startPage": body.get("startPage", "weather")}, indent=1))
            tmp.replace(SETTINGS)
            return self.send(200, load_settings())
        if path != "/api/countdowns":
            return self.send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        name = str(body.get("name", "")).strip()[:40]
        try:
            day = date.fromisoformat(body.get("date", "")).isoformat()
        except ValueError:
            return self.send(400, {"error": "date must be YYYY-MM-DD"})
        if not name:
            return self.send(400, {"error": "name required"})
        items = load_countdowns()
        item = {"id": max([c["id"] for c in items], default=0) + 1, "name": name,
                "date": day, "yearly": bool(body.get("yearly"))}
        save_countdowns(items + [item])
        self.send(200, item)

    def do_DELETE(self):
        m = re.fullmatch(r"/api/countdowns/(\d+)", urllib.parse.urlparse(self.path).path)
        if not m:
            return self.send(404, {"error": "not found"})
        save_countdowns([c for c in load_countdowns() if c["id"] != int(m[1])])
        self.send(200, {"ok": True})


if __name__ == "__main__":
    threading.Thread(target=tv_watcher, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
