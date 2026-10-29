#!/usr/bin/env python3
"""echo-hub: what the Muse Echo app needs from the Latitude, on one LAN port.

GET  /status                 homelab health from Uptime Kuma's database (read-only)
GET  /homelab                more detail for the Echo's homelab page: uptime, disk, memory,
                             containers, battery and the last backup
GET  /activity               whether Muse is working on this machine right now
GET  /museecho/latest.json   the newest Muse Echo build (written by its release script)
GET  /museecho/app.apk       that build

The Echo itself:
POST /echo/heartbeat         every minute; GET /echo/health is 200 while they arrive, 503
                             (GET /echo/health/kuma, for Uptime Kuma, also wants Muse linked and no recent crash)
                             when they stop (point an Uptime Kuma HTTP monitor at it)
POST /echo/state             its timers etc.; GET /echo/state for the watch
POST /echo/inbox             a command for the Echo (timer, announcement); needs the inbox
                             token (X-Inbox-Token). GET /echo/inbox hands them to the Echo once.
POST /echo/register          the Echo's backup key, accepted once (trust on first use)
POST /echo/backup            nightly backup (X-Echo-Key); GET /echo/backup returns the newest
GET  /photos, /photos/<name> the Syncthing "Echo Photos" folder, resized for the frame
GET  /presence               whether the chosen phone is home (it's looked for on the LAN every minute);
                             GET /presence/devices lists what's on the LAN to choose it from, and
                             POST /presence/device sets it (settings page)
GET  /presence/log           when the phone came and went (the settings page's history); /presence
                             also lists visitors: new phones on the LAN in the last 3 hours
GET  /homelab/week           the last 7 days on this machine, for the Echo's weekly recap
POST /echo/journal           the Echo's diary, things of the day and today's timeline; GET for the page
GET  /echo/album             the blob's daily snapshots (POST from the Echo); /echo/album/<date>.jpg each
POST /echo/friends/add       settings page / Pixel: {"count": n} new blob friends (named by Pixel)
POST /echo/gift              settings page: a present for the blob {kind: hat|snack|room, value, note}
POST /echo/export/request    settings page: ask the Echo for its world file (no secrets); GET /echo/export fetches it
POST /echo/import            settings page: a world file to restore (the Echo GETs it and restarts)
POST /echo/ask               settings page: have the Echo write its diary / a new thing of the day now
POST /desktop                the laptop's desktop colours and wallpaper (tools/desktop-sync in muse-echo);
                             GET /desktop and GET /desktop/wallpaper hand them to the Echo
The phone app (Puff on Rusty's phone; every /phone/ call needs the phone token as "Authorization: Bearer"):
GET  /phone/state            the Echo's live state (Puff's mood, who's over, what it's doing)
GET  /phone/world?since=t    Puff's world (the journal the Echo sends), unless it hasn't changed since t
GET  /phone/events?after=n   new things to notify about (letters, stickers, cards, the first snow)
GET  /phone/album[/<name>.jpg], /phone/album-friends/<name>.jpg   the album pictures
GET  /phone/settings         the few Echo settings the phone shows; GET/POST /phone/prefs its notification switches
POST /phone/do               {action: poke|invite|gift|play|game|announce|timer|settings|touch|remote|hello|card|type|
                             fortune|letter|trivia|outfit, ...}:
                             queued for the Echo
GET  /phone/wait?after=t&ev=n held open until the Echo rings the phone (POST /echo/find-phone), there's an
                             event after n (a visitor...), or ~50 s
GET  /phone/screen           the newest picture of the Echo's screen; POST /phone/screen/watch asks for pictures
                             every second for a while (the phone's live view, which can tap and swipe through)
POST /phone/photo?dest=frame|album&caption=..   a JPEG for the frame or Puff's album (the Echo fetches
                             GET /echo/phone-photo/<id>)
GET  /phone-app/latest.json, /phone-app/app.apk   the phone app's newest build (no token)
GET  /phone/pairing          settings page: the token, links and QR content; POST /phone/pairing/reset makes a new token
GET  /settings               a settings page for the Echo, to use from the laptop; the Echo posts
                             its settings to /echo/settings_state, the page reads GET /echo/settings
                             and POSTs changes there (they reach the Echo through its inbox)

Bind to the LAN and allow only the home subnet in the firewall. `python3 echo_hub.py
reset-echo-key` forgets the registered key (after the Echo is reflashed, to restore it).
"""
from __future__ import annotations

import base64
import hmac
import io
import ipaddress
import re
import socket
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

PORT = int(os.environ.get("ECHO_HUB_PORT", "8094"))
KUMA_DB = Path(os.environ.get("KUMA_DB", Path.home() / "latitude-status/data/kuma.db"))
RELEASES = Path(os.environ.get("ECHO_HUB_RELEASES", Path.home() / ".local/share/echo-hub/museecho"))
DATA = Path(os.environ.get("ECHO_HUB_DATA", Path.home() / ".local/share/echo-hub"))
PHOTOS = Path(os.environ.get("ECHO_PHOTOS", Path.home() / "latitude-syncthing/sync/echo-photos"))
PHOTO_CACHE = Path(os.environ.get("ECHO_PHOTO_CACHE", Path.home() / ".cache/echo-hub/photos"))
# Muse's tailnet address: its SSH sessions here mean it's busy on this machine.
MUSE_PEER = os.environ.get("MUSE_PEER", "")
# Uptime Kuma heartbeat states.
STATES = {0: "down", 1: "up", 2: "pending", 3: "maintenance"}
HEARTBEAT_STALE_S = 180
SPEED_LOG = Path(os.environ.get("SPEED_LOG", Path.home() / "latitude-actions/speed_log.jsonl"))
RESTART_GAP_S = 60
_last_restart = {"at": 0.0}
BACKUPS_KEPT = 7
MAX_BODY = 8 * 1024 * 1024
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
PHOTO_EDGE = 1280

PRESENCE_EVERY_S = 60
# A phone asleep can miss a few looks; it only counts as gone after this long unseen.
AWAY_AFTER_S = 15 * 60
SWEEP_EVERY_S = 5 * 60
# A visitor's phone still on the Wi-Fi after this long gets a "someone's here?" on Rusty's phone.
VISITOR_STAY_S = 5 * 60
DESKTOP_KEYS = {"background", "surface_container_lowest", "surface_container_low", "surface_container_high",
                "on_surface", "on_surface_variant", "primary", "on_primary_container", "tertiary", "error"}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

_lock = threading.Lock()
_echo = {"journal": None, "journal_at": 0.0, "heartbeat": None, "heartbeat_at": 0.0, "state": None, "state_at": 0.0, "inbox": [],
         "settings": None, "settings_at": 0.0, "screen": None, "screen_at": 0.0,
         "find_phone_at": 0.0, "phone_waiting_at": 0.0, "link_down_since": 0.0}
SETTINGS_PAGE = Path(__file__).with_name("settings_page.html")
BLOB_KINDS = {"reaction", "mood", "accessory", "prop", "color", "weather", "holiday", "state", "friend"}
SETTING_KEYS = {"clock_face", "frame_clock", "frame_delay_min", "blob_energy", "blob_sounds", "speak_replies", "follow_up",
                "come_to_front", "return_to_front", "show_server_pill", "auto_brightness", "laptop_notifications",
                "wake_sensitivity", "guest_hours", "quick_actions", "night_scene", "quick_sets",
                "theme", "background", "wellness_water", "wellness_stretch", "wellness_eyes", "wellness_start",
                "wellness_end", "routines", "room_sense", "diary", "daily_thing", "blob_room", "blob_friend",
                "laptop_break", "warm_evenings", "bedtime_hint", "page_order", "pages_hidden", "blob_name", "still_minutes",
                "puff_sniffles", "puff_away_sleepovers", "puff_house_build", "quiet_minutes",
                "day_night_look", "ambience", "ui_sounds", "user_name", "weather_accents",
                "guest_wifi_ssid", "guest_wifi_pass", "guest_wifi_sec"}
TV_KEYS = {"Up", "Down", "Left", "Right", "Select", "Back", "Home", "Rev", "Play", "Fwd", "VolumeDown", "VolumeUp", "VolumeMute", "Power", "InstantReplay", "Info"}
LAPTOP_KEYS = {"PLAY", "PAUSE", "NEXT", "PREVIOUS", "MUTE", "UNMUTE", "LOCK", "SLEEP", "BRIGHTNESS_UP", "BRIGHTNESS_DOWN"}
PHONE_PHOTOS = DATA / "phone-photos"
PHONE_APP = Path(os.environ.get("ECHO_HUB_PHONE_APP", DATA / "phone-app"))
# Rusty's phone reaches the hub over the tailnet when it's away from home (tailscale serve, tailnet only).
TAILNET_URL = os.environ.get("ECHO_HUB_TAILNET_URL", "")
LAN_URL = os.environ.get("ECHO_HUB_LAN_URL", "http://192.168.1.158:8094")
EVENT_KINDS = ("letters", "stickers", "cards", "snow", "weekly", "visitor", "costume")
# What the phone may change on the Echo (the rest stays on the settings page).
PHONE_SETTINGS = {"still_minutes", "quiet_minutes", "guest_hours", "blob_friend", "puff_sniffles", "puff_away_sleepovers", "puff_house_build"}
GIFT_KINDS = ("hat", "snack", "room")


def kuma_status(db_path: Path = KUMA_DB) -> dict:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        monitors = {mid: name for mid, name in db.execute("select id, name from monitor where active = 1")}
        latest = db.execute(
            "select monitor_id, status, time from heartbeat "
            "where id in (select max(id) from heartbeat group by monitor_id)"
        ).fetchall()
    finally:
        db.close()
    out = []
    for mid, status, when in latest:
        if mid in monitors:
            out.append({"name": monitors[mid], "status": STATES.get(status, "unknown"), "since": when})
    out.sort(key=lambda m: m["name"])
    states = {m["status"] for m in out}
    overall = "down" if "down" in states else "degraded" if states - {"up", "maintenance"} else "up"
    return {"overall": overall, "monitors": out, "checked_at": datetime.now(timezone.utc).isoformat()}


def kuma_uptime(db_path: Path = KUMA_DB, days: int = 1) -> dict[str, float]:
    """Share of 'up' heartbeats per monitor over the last [days] (24 hours by default), in percent."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = db.execute(
            "select m.name, avg(case when h.status = 1 then 1.0 else 0.0 end) from heartbeat h "
            "join monitor m on m.id = h.monitor_id "
            "where m.active = 1 and h.time > datetime('now', ?) group by m.name", (f"-{int(days)} day",)
        ).fetchall()
    finally:
        db.close()
    return {name: round(share * 100, 1) for name, share in rows if share is not None}


def _run(cmd: list[str], timeout: int = 5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def activity(peer: str = MUSE_PEER) -> dict:
    """Is Muse busy here: an open SSH connection from it, or a gadget command in the last minute?

    Only yes/no and which kind; never what it's running.
    """
    ssh = sum(1 for line in _run(["ss", "-tnH", "state", "established", "( sport = :22 )"]).splitlines()
              if line.split() and line.split()[-1].rsplit(":", 1)[0] == peer)
    gadget = "invoke" in _run(["journalctl", "--user", "-u", "musegadget", "--since", "-60s", "--no-pager", "-o", "cat"]).lower()
    kinds = (["ssh"] if ssh else []) + (["gadget"] if gadget else [])
    return {"busy": bool(kinds), "kinds": kinds, "checked_at": datetime.now(timezone.utc).isoformat()}


def _meminfo() -> dict:
    vals = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            vals[k] = int(v.split()[0])
    except (OSError, ValueError):
        return {}
    total, avail = vals.get("MemTotal", 0), vals.get("MemAvailable", 0)
    return {"total_mb": total // 1024, "used_pct": round(100 * (total - avail) / total, 1) if total else None}


def _battery() -> dict | None:
    bats = sorted(Path("/sys/class/power_supply").glob("BAT*"))
    if not bats:
        return None
    try:
        pct = int((bats[0] / "capacity").read_text().strip())
    except (OSError, ValueError):
        return None
    ac = any((p / "online").read_text().strip() == "1"
             for p in Path("/sys/class/power_supply").glob("A[CD]*") if (p / "online").exists())
    return {"percent": pct, "on_ac": ac}


def _last_backup() -> dict:
    out = _run(["systemctl", "--user", "show", "latitude-backup.service",
                "-p", "Result", "-p", "ExecMainStartTimestamp", "-p", "ActiveState"], 10)
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    ts = kv.get("ExecMainStartTimestamp", "").split()  # "Sat 2026-10-10 03:30:34 EDT"
    when = None
    if len(ts) >= 3:
        try:
            when = datetime.strptime(f"{ts[1]} {ts[2]}", "%Y-%m-%d %H:%M:%S").astimezone().isoformat()
        except ValueError:
            pass
    return {"at": when, "ok": kv.get("Result") == "success", "running": kv.get("ActiveState") in ("active", "activating")}


def _containers() -> list[dict]:
    rows = []
    for line in _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"], 15).splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            health = "unhealthy" if "unhealthy" in parts[2] else "healthy" if "healthy" in parts[2] else ""
            rows.append({"name": parts[0], "state": parts[1], "health": health})
    return sorted(rows, key=lambda r: r["name"])


def speed_history(days: float = 14) -> list[dict]:
    """latitude-actions' speed tests (it runs them; this only reads the log)."""
    cutoff = time.time() - days * 86400
    out = []
    try:
        for line in SPEED_LOG.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("t", 0) >= cutoff and "down_mbps" in r:
                out.append({"t": r["t"], "down": r.get("down_mbps"), "up": r.get("up_mbps"), "ping": r.get("ping_ms")})
    except OSError:
        pass
    return out[-60:]


def restart_container(name: str) -> tuple[bool, str]:
    """Restart one existing container (never stop/remove), at most once a minute."""
    known = {c["name"] for c in _containers()}
    if name not in known:
        return False, "no such container"
    if time.time() - _last_restart["at"] < RESTART_GAP_S:
        return False, "one restart a minute; try again shortly"
    _last_restart["at"] = time.time()
    try:
        r = subprocess.run(["docker", "restart", name], capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return r.returncode == 0, (r.stderr.strip() or "restarted")[:200]


def homelab() -> dict:
    status = kuma_status()
    try:
        uptime = kuma_uptime()
    except sqlite3.Error:
        uptime = {}
    for m in status["monitors"]:
        m["uptime_24h"] = uptime.get(m["name"])
    disk = shutil.disk_usage("/")
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = []
    return {
        "overall": status["overall"],
        "monitors": status["monitors"],
        "disk": {"total_gb": round(disk.total / 1e9, 1), "used_pct": round(100 * disk.used / disk.total, 1)},
        "memory": _meminfo(),
        "load": load,
        "cpus": os.cpu_count() or 1,
        "battery": _battery(),
        "containers": _containers(),
        "backup": _last_backup(),
        "speed": speed_history(),
        "uptime_s": int(float(Path("/proc/uptime").read_text().split()[0])) if Path("/proc/uptime").exists() else None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ---- who's home ----------------------------------------------------------------------

_presence = {"home": None, "since": 0.0, "last_seen": 0.0, "ip": None, "swept_at": 0.0}
VISITOR_WINDOW_S = 3 * 3600
_visitors: list[dict] = []


def _presence_log_file() -> Path:
    return DATA / "presence-log.json"


def presence_log() -> list[dict]:
    try:
        return json.loads(_presence_log_file().read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _log_presence(home: bool, at: float) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    _presence_log_file().write_text(json.dumps((presence_log() + [{"at": at, "home": home}])[-300:]))


def _known_file() -> Path:
    return DATA / "known-devices.json"


def random_mac(mac: str) -> bool:
    """Phones join with a private, made-up address: the "locally administered" bit is set."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except ValueError:
        return False


def check_visitors(neigh: dict[str, tuple[str, str]], lan: set[str], now: float | None = None) -> list[dict]:
    """Remember every device seen on the LAN; a new one with a phone-style private address is a
    visitor. The first look only learns what's already here."""
    now = now or time.time()
    try:
        known = json.loads(_known_file().read_text())
        first = False
    except (OSError, json.JSONDecodeError):
        known, first = {}, True
    mine = (presence_device() or {}).get("mac")
    new = []
    for mac, (ip, _state) in neigh.items():
        if ip not in lan or mac in known:
            continue
        known[mac] = now
        if not first and random_mac(mac) and mac != mine:
            try:
                name = socket.gethostbyaddr(ip)[0]
            except (OSError, socket.herror):
                name = ""
            new.append({"mac": mac, "name": name, "at": now})
    DATA.mkdir(parents=True, exist_ok=True)
    _known_file().write_text(json.dumps(known))
    with _lock:
        _visitors[:] = [v for v in _visitors + new if now - v["at"] < VISITOR_WINDOW_S]
    return new


def visitors_staying(neigh: dict[str, tuple[str, str]], now: float | None = None) -> list[dict]:
    """Visitors whose phone is still on the Wi-Fi a few minutes on (not just passing by): each one once,
    as a phone notification (skipped while the Echo's guest mode is already on)."""
    now = now or time.time()
    out = []
    with _lock:
        guest_on = ((_echo.get("settings") or {}).get("guest_until_ms") or 0) / 1000 > now
        for v in _visitors:
            if v.get("told") or now - v["at"] < VISITOR_STAY_S:
                continue
            ip_state = neigh.get(v["mac"])
            if ip_state is None or ip_state[1] in ("FAILED", "INCOMPLETE"):
                continue
            v["told"] = True
            if guest_on:
                continue
            who = v["name"].split(".")[0].replace("-", " ").strip()
            who = "" if not who or who[0].isdigit() else who
            away = _presence.get("home") is False
            out.append({"kind": "visitor", "title": "🚪 Someone's here?" if not away else "🚪 Someone's at home while you're out",
                        "body": f"A new phone{f' ({who})' if who else ''} has been on the Wi-Fi for a few minutes. "
                                "Turn on guest mode to hide your agenda and notes on the Echo."})
    return out


def _presence_file() -> Path:
    return DATA / "presence.json"


def presence_device() -> dict | None:
    try:
        d = json.loads(_presence_file().read_text())
        return d if d.get("mac") else None
    except (OSError, json.JSONDecodeError):
        return None


def set_presence_device(mac: str, label: str) -> bool:
    mac = mac.strip().lower()
    if mac and not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        return False
    DATA.mkdir(parents=True, exist_ok=True)
    _presence_file().write_text(json.dumps({"mac": mac, "label": label.strip()[:40]}))
    with _lock:
        _presence.update(home=None, since=0.0, last_seen=0.0, ip=None)
    return True


def parse_neighbours(text: str) -> dict[str, tuple[str, str]]:
    """`ip -4 neigh` lines -> {mac: (ip, state)}, leaving out failed/incomplete entries."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if "lladdr" not in parts or len(parts) < 5:
            continue
        mac = parts[parts.index("lladdr") + 1].lower()
        state = parts[-1]
        if state in ("FAILED", "INCOMPLETE"):
            continue
        out[mac] = (parts[0], state)
    return out


def _neighbours() -> dict[str, tuple[str, str]]:
    return parse_neighbours(_run(["ip", "-4", "neigh", "show"]))


def _ping(ip: str) -> None:
    try:
        subprocess.run(["ping", "-c1", "-W1", ip], capture_output=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _subnet() -> list[str]:
    """This machine's own /24 on its default route (the home LAN)."""
    route = _run(["ip", "-4", "route", "show", "default"]).split()
    dev = route[route.index("dev") + 1] if "dev" in route else None
    if not dev:
        return []
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", _run(["ip", "-4", "addr", "show", dev]))
    if not m:
        return []
    net = ipaddress.ip_network(f"{m.group(1)}/{max(int(m.group(2)), 24)}", strict=False)
    return [str(h) for h in net.hosts()]


def _sweep() -> None:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(64) as pool:
        list(pool.map(_ping, _subnet()))


def presence_tick(now: float | None = None) -> None:
    """One look for the phone: ping where it last was (a sleeping phone still answers ARP),
    sweep the subnet now and then if it's not there, and update home/away."""
    dev = presence_device()
    if not dev:
        return
    now = now or time.time()
    mac = dev["mac"]
    with _lock:
        ip = _presence["ip"]
    if ip:
        _ping(ip)
    seen = _neighbours().get(mac)
    if not (seen and seen[1] in ("REACHABLE", "DELAY", "PROBE")):
        with _lock:
            due = now - _presence["swept_at"] > SWEEP_EVERY_S
            if due:
                _presence["swept_at"] = now
        if due:
            _sweep()
            seen = _neighbours().get(mac)
    with _lock:
        if seen and seen[1] in ("REACHABLE", "DELAY", "PROBE"):
            _presence["ip"] = seen[0]
            _presence["last_seen"] = now
        last = _presence["last_seen"]
        home = last > 0 and now - last < AWAY_AFTER_S
        if home != _presence["home"]:
            # Leaving counts from when it was last seen, not from when that was noticed.
            changed = _presence["home"] is not None
            _presence["since"] = (last if not home and last else now) if changed else 0.0
            _presence["home"] = home
            if changed:
                _log_presence(home, _presence["since"])


def presence() -> dict:
    dev = presence_device()
    with _lock:
        p = dict(_presence)
        visitors = [v for v in _visitors if time.time() - v["at"] < VISITOR_WINDOW_S]
    return {"configured": dev is not None, "label": (dev or {}).get("label", ""), "visitors": visitors,
            "home": p["home"], "since": p["since"] or None,
            "last_seen_s": round(time.time() - p["last_seen"]) if p["last_seen"] else None}


def lan_devices() -> list[dict]:
    """What's on the LAN right now, with names where the router knows them (for the picker)."""
    from concurrent.futures import ThreadPoolExecutor
    _sweep()
    lan = set(_subnet())
    found = [(mac, ip, state) for mac, (ip, state) in _neighbours().items() if ip in lan]

    def name(ip: str) -> str:
        try:
            return socket.gethostbyaddr(ip)[0]
        except (OSError, socket.herror):
            return ""
    with ThreadPoolExecutor(16) as pool:
        names = list(pool.map(name, [ip for _, ip, _ in found]))
    out = [{"mac": mac, "ip": ip, "name": n, "state": state} for (mac, ip, state), n in zip(found, names)]
    return sorted(out, key=lambda d: tuple(int(x) for x in d["ip"].split(".")))


def _presence_loop() -> None:
    swept = 0.0
    while True:
        try:
            presence_tick()
            # Every few minutes, a look at everything on the LAN for new visitors.
            if time.time() - swept > SWEEP_EVERY_S:
                swept = time.time()
                _sweep()
                neigh = _neighbours()
                check_visitors(neigh, set(_subnet()))
                add_events(visitors_staying(neigh))
        except Exception as exc:  # keep looking
            print(f"presence: {exc}", flush=True)
        time.sleep(PRESENCE_EVERY_S)


# ---- the laptop's desktop -------------------------------------------------------------

def _desktop_file() -> Path:
    return DATA / "desktop.json"


def _wallpaper_file() -> Path:
    return DATA / "desktop-wallpaper.jpg"


def save_desktop(d: dict) -> dict:
    """Colours (a few of matugen's) and the wallpaper as a small JPEG, from the laptop."""
    colors = {k: v for k, v in (d.get("colors") or {}).items() if k in DESKTOP_KEYS and isinstance(v, str) and HEX.match(v)}
    if len(colors) < len(DESKTOP_KEYS) // 2:
        raise ValueError("not enough colours")
    info = {"colors": colors, "updated_at": time.time(), "wallpaper": False}
    if d.get("wallpaper_b64"):
        data = base64.b64decode(d["wallpaper_b64"])
        if data[:2] != b"\xff\xd8":
            raise ValueError("wallpaper must be a JPEG")
        DATA.mkdir(parents=True, exist_ok=True)
        _wallpaper_file().write_bytes(data)
        info["wallpaper"] = True
    elif _wallpaper_file().is_file():
        info["wallpaper"] = True
    DATA.mkdir(parents=True, exist_ok=True)
    _desktop_file().write_text(json.dumps(info))
    return info


ALBUM_KINDS = {"daily": 90, "friends": 150}


def _album_dir(kind: str = "daily") -> Path:
    return DATA / ("album" if kind == "daily" else "album-friends")


def album_list(kind: str = "daily") -> list[dict]:
    try:
        return json.loads((_album_dir(kind) / "album.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []


def save_album(date: str, caption: str, jpeg: bytes, kind: str = "daily") -> bool:
    """The Echo's daily blob snapshot (one per date, about three months kept), or a friend
    moment (every visit, the newest 150)."""
    if kind not in ALBUM_KINDS or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or jpeg[:2] != b"\xff\xd8":
        return False
    d = _album_dir(kind)
    d.mkdir(parents=True, exist_ok=True)
    name = date if kind == "daily" else f"{date}_{int(time.time() * 1000) % 86_400_000}"
    (d / f"{name}.jpg").write_bytes(jpeg)
    items = [a for a in album_list(kind) if kind != "daily" or a["date"] != date]
    items.append({"date": date, "name": name, "caption": caption[:80], "at": time.time()})
    items.sort(key=lambda a: a["at"])
    keep = ALBUM_KINDS[kind]
    for old in items[:-keep]:
        (d / f"{old.get('name', old['date'])}.jpg").unlink(missing_ok=True)
    (d / "album.json").write_text(json.dumps(items[-keep:]))
    return True


def desktop() -> dict | None:
    try:
        return json.loads(_desktop_file().read_text())
    except (OSError, json.JSONDecodeError):
        return None


def week_facts() -> dict:
    """The last 7 days on the Latitude, for the Echo's weekly recap (Pixel writes it up)."""
    try:
        up = kuma_uptime(days=7)
    except sqlite3.Error:
        up = {}
    speeds = speed_history(7)
    downs = [x["down"] for x in speeds if x.get("down")]
    restarts = []
    names = [c["name"] for c in _containers()]
    if names:
        for line in _run(["docker", "inspect", "-f", "{{.Name}}\t{{.RestartCount}}\t{{.State.StartedAt}}"] + names, 15).splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                started = parts[2][:19]
                try:
                    recent = time.time() - datetime.strptime(started, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() < 7 * 86400
                except ValueError:
                    recent = False
                restarts.append({"name": parts[0].lstrip("/"), "restart_count": int(parts[1] or 0), "started_this_week": recent})
    backups = _run(["journalctl", "--user", "-u", "latitude-backup", "--since", "-7d", "--no-pager", "-o", "cat"], 15)
    disk = shutil.disk_usage("/")
    return {
        "uptime_7d": up,
        "worst": sorted(up.items(), key=lambda kv: kv[1])[:3],
        "speed": {"tests": len(downs), "avg_down": round(sum(downs) / len(downs)) if downs else None,
                  "min_down": round(min(downs)) if downs else None, "max_down": round(max(downs)) if downs else None},
        "containers": restarts,
        "backups_ok": backups.count("Finished") + backups.count("Deactivated successfully"),
        "backups_failed": backups.lower().count("failed"),
        "disk_used_pct": round(100 * disk.used / disk.total, 1),
        "uptime_days": round(float(Path("/proc/uptime").read_text().split()[0]) / 86400, 1) if Path("/proc/uptime").exists() else None,
    }


# ---- the Echo ---------------------------------------------------------------------------

def echo_health(now: float | None = None) -> tuple[bool, dict]:
    now = now or time.time()
    with _lock:
        hb, at = _echo["heartbeat"], _echo["heartbeat_at"]
    age = now - at if at else None
    ok = age is not None and age < HEARTBEAT_STALE_S
    return ok, {"ok": ok, "age_s": round(age) if age is not None else None, "last": hb}


LINK_DOWN_GRACE_S = 300
CRASH_WINDOW_S = 900


def echo_kuma_health(now: float | None = None) -> tuple[bool, dict]:
    """Stricter than echo_health, for Uptime Kuma: the heartbeat arriving, Muse linked (allowing a few
    minutes for reconnects) and no crash in the last 15 minutes."""
    now = now or time.time()
    ok, info = echo_health(now)
    problems = []
    if not ok:
        problems.append("no heartbeat" if info["age_s"] is None else f"no heartbeat for {info['age_s']} s")
    hb = info.get("last") or {}
    with _lock:
        if ok and hb.get("link") not in (None, "connected"):
            _echo["link_down_since"] = _echo["link_down_since"] or now
        else:
            _echo["link_down_since"] = 0.0
        down = _echo["link_down_since"]
    if down and now - down >= LINK_DOWN_GRACE_S:
        problems.append(f"Muse link {hb.get('link')} for {round((now - down) / 60)} min")
    for e in (hb.get("health") or {}).get("recent_errors") or []:
        if "CRASH" not in e:
            continue
        try:
            t = time.mktime(time.strptime(e[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if now - t < CRASH_WINDOW_S:
            problems.append("the app crashed recently")
            break
    return not problems, {"ok": not problems, "problems": problems, "version": hb.get("version"), "link": hb.get("link")}


def ring_phone(now: float | None = None) -> dict:
    """The Echo's "find my phone": the phone app's listener picks it up from /phone/wait."""
    now = now or time.time()
    with _lock:
        _echo["find_phone_at"] = now
        listening = now - _echo["phone_waiting_at"] < 90
    return {"ok": True, "listening": listening}


def _secret_file(name: str) -> Path:
    return DATA / name


def inbox_token() -> str:
    """The shared secret for POST /echo/inbox; latitude-actions reads the same file."""
    f = _secret_file("inbox-token")
    if not f.is_file():
        DATA.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_hex(24))
        f.chmod(0o600)
    return f.read_text().strip()


def echo_key() -> str | None:
    f = _secret_file("echo-key")
    return f.read_text().strip() if f.is_file() else None


def register_echo_key(key: str) -> bool:
    """Trust on first use: the first key wins until reset-echo-key."""
    if len(key) < 32:
        return False
    current = echo_key()
    if current is not None:
        return hmac.compare_digest(current, key)
    DATA.mkdir(parents=True, exist_ok=True)
    f = _secret_file("echo-key")
    f.write_text(key)
    f.chmod(0o600)
    return True


def _backup_dir() -> Path:
    d = DATA / "echo-backups"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def save_backup(body: bytes) -> str:
    json.loads(body)  # must be JSON
    name = datetime.now().strftime("backup-%Y%m%d-%H%M%S.json")
    f = _backup_dir() / name
    f.write_bytes(body)
    f.chmod(0o600)
    for old in sorted(_backup_dir().glob("backup-*.json"))[:-BACKUPS_KEPT]:
        old.unlink()
    return name


def latest_backup() -> bytes | None:
    files = sorted(_backup_dir().glob("backup-*.json"))
    return files[-1].read_bytes() if files else None


def photo_list() -> list[dict]:
    if not PHOTOS.is_dir():
        return []
    out = []
    for p in sorted(PHOTOS.iterdir()):
        if p.is_file() and p.suffix.lower() in PHOTO_EXT and not p.name.startswith("."):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    return out


def photo_bytes(name: str) -> bytes | None:
    """A JPEG no bigger than PHOTO_EDGE on its long side, cached by name + mtime."""
    src = PHOTOS / name
    if "/" in name or name.startswith(".") or not src.is_file() or src.suffix.lower() not in PHOTO_EXT:
        return None
    PHOTO_CACHE.mkdir(parents=True, exist_ok=True)
    cached = PHOTO_CACHE / f"{name}.{int(src.stat().st_mtime)}.jpg"
    if cached.is_file():
        return cached.read_bytes()
    try:
        from PIL import Image, ImageOps
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((PHOTO_EDGE, PHOTO_EDGE))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
        data = buf.getvalue()
    except Exception:  # not an image Pillow can read: hand over the original
        data = src.read_bytes()
    for old in PHOTO_CACHE.glob(f"{name}.*.jpg"):
        old.unlink()
    cached.write_bytes(data)
    return data


# -- The phone app ---------------------------------------------------------------------------

def phone_token() -> str:
    f = _secret_file("phone-token")
    if not f.is_file():
        DATA.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_hex(24))
        f.chmod(0o600)
    return f.read_text().strip()


def reset_phone_token() -> str:
    _secret_file("phone-token").unlink(missing_ok=True)
    return phone_token()


def phone_prefs() -> dict:
    try:
        saved = json.loads((DATA / "phone-prefs.json").read_text())
    except (OSError, json.JSONDecodeError):
        saved = {}
    return {k: bool(saved.get(k, True)) for k in EVENT_KINDS}


def set_phone_prefs(changes: dict) -> dict:
    prefs = phone_prefs()
    for k, v in changes.items():
        if k in EVENT_KINDS and isinstance(v, bool):
            prefs[k] = v
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "phone-prefs.json").write_text(json.dumps(prefs))
    return prefs


def world_events(old: dict | None, new: dict, blob: str = "Puff") -> list[dict]:
    """What's new in Puff's world worth a notification, comparing two journals the Echo sent."""
    if not old:
        return []
    of, nf = old.get("friends") or {}, new.get("friends") or {}
    names = {f.get("id"): (f.get("name") or "a friend") for f in nf.get("friends") or []}
    penpal = (nf.get("penpal") or {}).get("name")
    out = []
    seen = {l.get("atMs") for l in of.get("letters") or []}
    for l in nf.get("letters") or []:
        if l.get("atMs") in seen:
            continue
        text = str(l.get("text", ""))
        if l.get("fromBlob") and l.get("friendId") == "rusty":
            out.append({"kind": "letters", "title": f"💌 {blob} wrote you a letter", "body": text[:160]})
        elif not l.get("fromBlob") and l.get("friendId") != "rusty":  # Rusty's own letters to Puff aren't news to him
            who = penpal if l.get("friendId") == "penpal" and penpal else names.get(l.get("friendId"), "a friend")
            out.append({"kind": "letters", "title": f"✉️ A letter for {blob} from {who}", "body": text[:160]})
    catalog = {s.get("id"): s for s in ((new.get("catalog") or {}).get("stickers") or [])}
    for sid in set(nf.get("stickers") or {}) - set(of.get("stickers") or {}):
        st = catalog.get(sid, {})
        mine = st.get("who") == "you"
        out.append({"kind": "stickers", "title": f"🏅 New sticker{' for you' if mine else ''}: {st.get('emoji', '')} {st.get('title', sid)}".replace("  ", " "),
                    "body": "Earned on the Echo. See it in the sticker book."})
    old_cards = {c.get("date") for c in of.get("cards") or []}
    for c in nf.get("cards") or []:
        if c.get("date") not in old_cards:
            out.append({"kind": "cards", "title": f"💌 {blob} and friends made you a {c.get('holiday', 'holiday')} card", "body": str(c.get("text", ""))[:160]})
    if (new.get("street") or {}).get("snowman") and not (old.get("street") or {}).get("snowman"):
        out.append({"kind": "snow", "title": "⛄ The first snow!", "body": f"Everyone came over and built a snowman in {blob}'s room."})
    if nf.get("costumeMonth") and nf.get("costumeMonth") != of.get("costumeMonth") and of.get("costumeMonth") is not None:
        out.append({"kind": "costume", "title": f"📦 A new costume for {blob}!",
                    "body": "This month's costume arrived in the closet. Dress Puff up from the Puff tab."})
    if new.get("week_recap_ms") and new.get("week_recap_ms") != old.get("week_recap_ms"):
        out.append({"kind": "weekly", "title": "📰 Your weekly report", "body": str(new.get("week_recap", ""))[:300]})
    return out


_events_lock = threading.Lock()


def _events_file() -> Path:
    return DATA / "phone-events.json"


def load_events() -> dict:
    try:
        return json.loads(_events_file().read_text())
    except (OSError, json.JSONDecodeError):
        return {"next": 1, "items": []}


def add_events(new: list[dict]) -> None:
    if not new:
        return
    with _events_lock:
        ev = load_events()
        for e in new:
            ev["items"].append({**e, "id": ev["next"], "at": time.time()})
            ev["next"] += 1
        ev["items"] = ev["items"][-100:]
        DATA.mkdir(parents=True, exist_ok=True)
        _events_file().write_text(json.dumps(ev))


def events_after(after: int) -> dict:
    ev = load_events()
    return {"events": [e for e in ev["items"] if e["id"] > after], "last": ev["next"] - 1}


def phone_action(d: dict) -> dict | None:
    """A request from the phone, as an inbox item for the Echo (None if it isn't allowed)."""
    a = str(d.get("action", ""))
    s = lambda k, n=80: str(d.get(k, ""))[:n]  # noqa: E731
    if a == "poke":
        return {"type": "poke", "target": "friend" if d.get("friend_id") else "blob", "friend_id": s("friend_id", 16), "from": "phone"}
    if a == "invite":
        return {"type": "invite", "name": s("name", 30), "activity": s("activity", 30)}
    if a == "gift" and d.get("kind") in GIFT_KINDS:
        return {"type": "gift", "kind": d["kind"], "value": s("value", 20), "note": s("note", 120)}
    if a == "play" and re.fullmatch(r"[A-Za-z_]{1,30}", s("value", 40)):
        return {"type": "blob", "kind": "friend", "value": s("value", 30).upper()}
    if a == "game" and d.get("outcome") in (-1, 0, 1) and d.get("game") in ("tic-tac-toe", "rock-paper-scissors"):
        return {"type": "game", "game": d["game"], "outcome": d["outcome"]}
    if a == "announce" and s("text", 300).strip():
        return {"type": "announce", "text": s("text", 300).strip()}
    if a == "timer" and isinstance(d.get("seconds"), int) and 1 <= d["seconds"] <= 24 * 3600:
        return {"type": "timer", "seconds": d["seconds"], "label": s("label", 40)}
    if a == "hello":
        return {"type": "hello"}
    if a == "remote":
        target, key = d.get("target"), s("key", 60)
        ok = (target == "tv" and key in TV_KEYS) or (target == "laptop" and (key in LAPTOP_KEYS or re.fullmatch(r"shortcut:[\w.-]{1,40}", key)))
        return {"type": "remote", "target": target, "key": key} if ok else None
    if a == "card" and s("text", 600).strip():
        return {"type": "card", "text": s("text", 600).strip(), "title": s("title", 60).strip()}
    if a == "type" and (s("text", 300) or d.get("enter") is True):
        return {"type": "type", "text": s("text", 300), "enter": d.get("enter") is True}
    if a == "touch" and d.get("kind") in ("tap", "swipe"):
        def frac(k):
            v = d.get(k)
            return float(v) if isinstance(v, (int, float)) and 0 <= v <= 1 else None
        x, y = frac("x"), frac("y")
        if x is None or y is None:
            return None
        item = {"type": "touch", "kind": d["kind"], "x": x, "y": y}
        if d["kind"] == "swipe":
            x2, y2 = frac("x2"), frac("y2")
            if x2 is None or y2 is None:
                return None
            item.update(x2=x2, y2=y2)
        return item
    # The fun things: a fortune cookie, a letter to Puff, trivia night, and an outfit from the closet.
    if a == "fortune":
        return {"type": "fortune"}
    if a == "letter" and s("text", 1000).strip():
        return {"type": "rustyletter", "text": s("text", 1000).strip()}
    if a == "trivia":
        return {"type": "trivia", "theme": s("theme", 40).strip()}
    if a == "outfit" and re.fullmatch(r"[A-Z_]{1,30}", s("name", 40)):
        return {"type": "outfit", "name": s("name", 30)}
    if a == "settings" and isinstance(d.get("changes"), dict):
        changes = {k: v for k, v in d["changes"].items() if k in PHONE_SETTINGS and isinstance(v, (bool, int, float))}
        return {"type": "settings", "changes": changes} if changes else None
    return None


def save_phone_photo(jpeg: bytes) -> str | None:
    if jpeg[:2] != b"\xff\xd8":
        return None
    PHONE_PHOTOS.mkdir(parents=True, exist_ok=True)
    # Old ones (the Echo fetched them long ago) go.
    for f in PHONE_PHOTOS.glob("*.jpg"):
        if time.time() - f.stat().st_mtime > 3 * 86400:
            f.unlink(missing_ok=True)
    pid = secrets.token_hex(8)
    (PHONE_PHOTOS / f"{pid}.jpg").write_bytes(jpeg)
    return pid


def phone_settings() -> dict:
    with _lock:
        st = dict(_echo["settings"] or {})
    keys = ("version", "blob_name", "still_until_ms", "quiet_until_ms", "guest_until_ms", "blob_friend",
            "puff_sniffles", "puff_away_sleepovers", "puff_house_build")
    return {k: st[k] for k in keys if k in st}


def phone_pairing() -> dict:
    app = None
    try:
        app = json.loads((PHONE_APP / "latest.json").read_text())
    except (OSError, json.JSONDecodeError):
        pass
    token = phone_token()
    return {"token": token, "lan": LAN_URL, "tailnet": TAILNET_URL, "app": app,
            "qr": json.dumps({"puff": 1, "lan": LAN_URL, "tailnet": TAILNET_URL, "token": token}, separators=(",", ":"))}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload).encode())

    def _body(self) -> bytes | None:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return None
        return self.rfile.read(n) if n else b""

    def _phone_ok(self) -> bool:
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return bool(given) and hmac.compare_digest(given, phone_token())

    def _key_ok(self) -> bool:
        key = echo_key()
        given = self.headers.get("X-Echo-Key", "")
        return key is not None and hmac.compare_digest(key, given)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        try:
            path = self.path.split("?", 1)[0]
            if path.startswith("/phone/") and path != "/phone/pairing":
                if not self._phone_ok():
                    self._json(401, {"error": "pair the app on the settings page"})
                    return
                self._phone_get(path)
                return
            if path == "/phone/pairing":
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                self._json(200, phone_pairing())
            elif m := re.fullmatch(r"/echo/phone-photo/([0-9a-f]{16})", path):
                f = PHONE_PHOTOS / f"{m.group(1)}.jpg"
                self._send(200, f.read_bytes(), "image/jpeg") if f.is_file() else self._json(404, {"error": "no such photo"})
            elif path in ("/phone-app/latest.json", "/phone-app/app.apk"):
                f = PHONE_APP / path.removeprefix("/phone-app/")
                if not f.is_file():
                    self._json(404, {"error": "no build yet"})
                else:
                    self._send(200, f.read_bytes(), "application/json" if f.suffix == ".json" else "application/vnd.android.package-archive")
            elif path == "/status":
                self._json(200, kuma_status())
            elif path == "/homelab":
                self._json(200, homelab())
            elif path == "/presence":
                self._json(200, presence())
            elif path == "/echo/album":
                from urllib.parse import parse_qs, urlparse
                kind = parse_qs(urlparse(self.path).query).get("kind", ["daily"])[0]
                self._json(200, album_list(kind) if kind in ALBUM_KINDS else [])
            elif m := re.fullmatch(r"/echo/album(-friends)?/(\d{4}-\d{2}-\d{2}(?:_\d+)?)\.jpg", path):
                f = _album_dir("friends" if m.group(1) else "daily") / f"{m.group(2)}.jpg"
                self._send(200, f.read_bytes(), "image/jpeg") if f.is_file() else self._json(404, {"error": "no such picture"})
            elif path in ("/echo/export", "/echo/import"):
                # The world file the Echo sent (for the page's download) / the one uploaded to restore (for the Echo).
                if path == "/echo/export" and self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                f = DATA / ("world-export.json" if path == "/echo/export" else "world-import.json")
                self._send(200, f.read_bytes()) if f.is_file() else self._json(404, {"error": "nothing yet"})
            elif path == "/presence/log":
                self._json(200, presence_log())
            elif path == "/homelab/week":
                self._json(200, week_facts())
            elif path == "/echo/journal":
                with _lock:
                    j, at = _echo["journal"], _echo["journal_at"]
                self._json(200, {"journal": j, "age_s": round(time.time() - at) if at else None})
            elif path == "/presence/devices":
                self._json(200, {"devices": lan_devices(), "chosen": presence_device()})
            elif path == "/desktop":
                d = desktop()
                self._json(200 if d else 404, d or {"error": "the laptop hasn't sent its desktop yet"})
            elif path == "/desktop/wallpaper":
                f = _wallpaper_file()
                self._send(200, f.read_bytes(), "image/jpeg") if f.is_file() else self._json(404, {"error": "no wallpaper"})
            elif path == "/activity":
                self._json(200, activity())
            elif path == "/echo/health":
                ok, info = echo_health()
                self._json(200 if ok else 503, info)
            elif path == "/echo/health/kuma":
                ok, info = echo_kuma_health()
                self._json(200 if ok else 503, info)
            elif path == "/echo/state":
                with _lock:
                    state, at = _echo["state"], _echo["state_at"]
                self._json(200, {"state": state, "age_s": round(time.time() - at) if at else None})
            elif path in ("/settings", "/settings/"):
                if SETTINGS_PAGE.is_file():
                    self._send(200, SETTINGS_PAGE.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._json(404, {"error": "settings page missing"})
            elif path == "/echo/screen":
                # The newest picture of the Echo's screen (for the settings page's live view).
                with _lock:
                    img, at = _echo["screen"], _echo["screen_at"]
                if img:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(img)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Age-S", str(round(time.time() - at)))
                    self.end_headers()
                    self.wfile.write(img)
                else:
                    self._json(404 if not at else 204, {"error": "no picture" if not at else "the app isn't on screen"})
            elif path == "/echo/settings":
                with _lock:
                    st, at = _echo["settings"], _echo["settings_at"]
                self._json(200, {"settings": st, "age_s": round(time.time() - at) if at else None})
            elif path == "/echo/inbox":
                with _lock:
                    items, _echo["inbox"] = _echo["inbox"], []
                self._json(200, items)
            elif path == "/echo/backup":
                if not self._key_ok():
                    self._json(403, {"error": "unknown echo key"})
                    return
                data = latest_backup()
                self._send(200, data) if data else self._json(404, {"error": "no backup yet"})
            elif path == "/photos":
                self._json(200, photo_list())
            elif path.startswith("/photos/"):
                data = photo_bytes(unquote(path.removeprefix("/photos/")))
                self._send(200, data, "image/jpeg") if data else self._json(404, {"error": "no such photo"})
            elif path == "/museecho/latest.json":
                f = RELEASES / "latest.json"
                self._send(200, f.read_bytes()) if f.is_file() else self._send(404, b'{"error":"no release"}')
            elif path == "/museecho/app.apk":
                f = RELEASES / "app.apk"
                if f.is_file():
                    self._send(200, f.read_bytes(), "application/vnd.android.package-archive")
                else:
                    self._send(404, b'{"error":"no release"}')
            else:
                self._send(404, b'{"error":"not found"}')
        except Exception as exc:  # report, keep serving
            self._json(500, {"error": str(exc)})

    def _phone_get(self, path: str) -> None:
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        if path == "/phone/state":
            with _lock:
                state, at = _echo["state"], _echo["state_at"]
            ok, _ = echo_health()
            self._json(200, {"state": state, "age_s": round(time.time() - at) if at else None, "online": ok})
        elif path == "/phone/world":
            since = float(q.get("since", ["0"])[0] or 0)
            with _lock:
                j, at = _echo["journal"], _echo["journal_at"]
            if j is None:
                self._json(404, {"error": "the Echo hasn't sent Puff's world yet"})
            elif at <= since:
                self._json(200, {"unchanged": True, "at": at})
            else:
                self._json(200, {"at": at, "journal": j, "blob_name": phone_settings().get("blob_name") or "Puff"})
        elif path == "/phone/events":
            self._json(200, events_after(int(q.get("after", ["0"])[0] or 0)))
        elif path == "/phone/prefs":
            self._json(200, phone_prefs())
        elif path == "/phone/settings":
            self._json(200, phone_settings())
        elif path == "/phone/album":
            kind = q.get("kind", ["daily"])[0]
            self._json(200, album_list(kind) if kind in ALBUM_KINDS else [])
        elif m := re.fullmatch(r"/phone/album(-friends)?/(\d{4}-\d{2}-\d{2}(?:_\d+)?)\.jpg", path):
            f = _album_dir("friends" if m.group(1) else "daily") / f"{m.group(2)}.jpg"
            self._send(200, f.read_bytes(), "image/jpeg") if f.is_file() else self._json(404, {"error": "no such picture"})
        elif path == "/phone/ping":
            self._json(200, {"ok": True})
        elif path == "/phone/wait":
            # The phone's listener: held open until the Echo asks to ring it (or ~50 s pass).
            # Also back early when there's something new to notify about (after "ev", the last one it saw).
            after = float(q.get("after", ["0"])[0] or 0)
            ev_after = int(q.get("ev", ["-1"])[0] or -1)
            end = time.time() + 50
            while True:
                with _lock:
                    _echo["phone_waiting_at"] = time.time()
                    ring = _echo["find_phone_at"]
                last = load_events()["next"] - 1
                if ring > after or (ev_after >= 0 and last > ev_after) or time.time() >= end:
                    break
                time.sleep(1)
            self._json(200, {"ring": ring if ring > after else None, "events": last, "now": time.time()})
        elif path == "/phone/screen":
            with _lock:
                img, at = _echo["screen"], _echo["screen_at"]
            if img:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(img)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Age-S", str(round(time.time() - at)))
                self.end_headers()
                self.wfile.write(img)
            else:
                self._json(404 if not at else 204, {"error": "no picture" if not at else "the app isn't on screen"})
        else:
            self._json(404, {"error": "not found"})

    def _phone_post(self, path: str, body: bytes) -> None:
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        if path == "/phone/do":
            item = phone_action(json.loads(body or b"{}"))
            if item is None:
                self._json(400, {"error": "not something the phone can do"})
                return
            item["at"] = time.time()
            with _lock:
                _echo["inbox"] = (_echo["inbox"] + [item])[-20:]
            self._json(200, {"ok": True, "queued": item["type"]})
        elif path == "/phone/photo":
            dest = q.get("dest", ["frame"])[0]
            if dest not in ("frame", "album"):
                self._json(400, {"error": "dest must be frame or album"})
                return
            pid = save_phone_photo(body)
            if pid is None:
                self._json(400, {"error": "send a JPEG"})
                return
            item = {"type": "photo", "id": pid, "dest": dest, "caption": q.get("caption", [""])[0][:80], "at": time.time()}
            with _lock:
                _echo["inbox"] = (_echo["inbox"] + [item])[-20:]
            self._json(200, {"ok": True, "id": pid})
        elif path == "/phone/prefs":
            self._json(200, set_phone_prefs(json.loads(body or b"{}")))
        elif path == "/phone/screen/watch":
            with _lock:
                if not any(i.get("type") == "screen" for i in _echo["inbox"]):
                    _echo["inbox"] = (_echo["inbox"] + [{"type": "screen", "fast": True, "at": time.time()}])[-20:]
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        try:
            path = self.path.split("?", 1)[0]
            body = self._body()
            if body is None:
                self._json(413, {"error": "too big"})
                return
            if path.startswith("/phone/") and path != "/phone/pairing/reset":
                if not self._phone_ok():
                    self._json(401, {"error": "pair the app on the settings page"})
                    return
                self._phone_post(path, body)
                return
            if path == "/phone/pairing/reset":
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                reset_phone_token()
                self._json(200, phone_pairing())
                return
            if path == "/echo/heartbeat":
                with _lock:
                    _echo["heartbeat"] = json.loads(body or b"{}")
                    _echo["heartbeat_at"] = time.time()
                self._json(200, {"ok": True})
            elif path == "/echo/find-phone":
                self._json(200, ring_phone())
            elif path == "/echo/state":
                with _lock:
                    _echo["state"] = json.loads(body or b"{}")
                    _echo["state_at"] = time.time()
                self._json(200, {"ok": True})
            elif path == "/echo/journal":
                # From the Echo: its diary, past things of the day and today's timeline.
                new = json.loads(body or b"{}")
                with _lock:
                    old = _echo["journal"]
                    _echo["journal"] = new
                    _echo["journal_at"] = time.time()
                if old is None:
                    try:
                        old = json.loads((DATA / "journal-last.json").read_text())
                    except (OSError, json.JSONDecodeError):
                        old = None
                add_events(world_events(old, new, phone_settings().get("blob_name") or "Puff"))
                DATA.mkdir(parents=True, exist_ok=True)
                (DATA / "journal-last.json").write_text(json.dumps(new))
                self._json(200, {"ok": True})
            elif path == "/echo/album":
                # From the Echo: today's blob snapshot (JPEG body; date and caption in the query).
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                ok = save_album(q.get("date", [""])[0], q.get("caption", [""])[0], body or b"", q.get("kind", ["daily"])[0])
                self._json(200 if ok else 400, {"ok": ok})
            elif path == "/echo/gift":
                # The settings page: a present for the blob (a hat, a snack, something for its room).
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                d = json.loads(body or b"{}")
                kind = str(d.get("kind", ""))
                if kind not in GIFT_KINDS:
                    self._json(400, {"error": "kind must be hat, snack or room"})
                    return
                item = {"type": "gift", "kind": kind, "value": str(d.get("value", ""))[:20], "note": str(d.get("note", ""))[:120], "at": time.time()}
                with _lock:
                    _echo["inbox"] = (_echo["inbox"] + [item])[-20:]
                self._json(200, {"ok": True})
            elif path == "/echo/export/request":
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                (DATA / "world-export.json").unlink(missing_ok=True)
                with _lock:
                    _echo["inbox"] = (_echo["inbox"] + [{"type": "export", "at": time.time()}])[-20:]
                self._json(200, {"ok": True})
            elif path == "/echo/export":
                # From the Echo: its world, without pairing or tokens.
                try:
                    shareable = json.loads(body or b"{}").get("shareable") is True
                except json.JSONDecodeError:
                    shareable = False
                if not shareable:
                    self._json(400, {"error": "only shareable exports"})
                    return
                DATA.mkdir(parents=True, exist_ok=True)
                (DATA / "world-export.json").write_bytes(body)
                self._json(200, {"ok": True})
            elif path == "/echo/import":
                # The settings page: a world file to restore onto the Echo.
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                try:
                    d = json.loads(body or b"{}")
                    assert d.get("format") == 1 and isinstance(d.get("files"), dict)
                except (json.JSONDecodeError, AssertionError, AttributeError):
                    self._json(400, {"error": "that isn't a Muse Echo world file"})
                    return
                DATA.mkdir(parents=True, exist_ok=True)
                (DATA / "world-import.json").write_bytes(body)
                with _lock:
                    _echo["inbox"] = (_echo["inbox"] + [{"type": "import", "at": time.time()}])[-20:]
                self._json(200, {"ok": True})
            elif path == "/echo/friends/add":
                # The settings page (or Pixel): new blob friends, named by Pixel shortly after.
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                try:
                    count = max(1, min(6, int(json.loads(body or b"{}").get("count", 1))))
                except (TypeError, ValueError):
                    self._json(400, {"error": "count must be a number"})
                    return
                with _lock:
                    _echo["inbox"] = (_echo["inbox"] + [{"type": "friends_add", "count": count, "at": time.time()}])[-20:]
                self._json(200, {"ok": True, "count": count})
            elif path == "/echo/friends/leave":
                # The settings page: everyone who's over goes home now (the visit is still recorded).
                # Turning visits off ends the visit on the Echo; they're straight back on after.
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                now = time.time()
                with _lock:
                    _echo["inbox"] = (_echo["inbox"] + [{"type": "settings", "changes": {"blob_friend": False}, "at": now},
                                                        {"type": "settings", "changes": {"blob_friend": True}, "at": now}])[-20:]
                self._json(200, {"ok": True})
            elif path == "/echo/ask":
                # The settings page: write the diary / a new thing of the day now.
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                what = str(json.loads(body or b"{}").get("what", ""))
                if what not in ("diary", "thing"):
                    self._json(400, {"error": "what must be diary or thing"})
                    return
                with _lock:
                    _echo["inbox"] = (_echo["inbox"] + [{"type": "ask", "what": what, "at": time.time()}])[-20:]
                self._json(200, {"ok": True})
            elif path == "/echo/settings_state":
                with _lock:
                    _echo["settings"] = json.loads(body or b"{}")
                    _echo["settings_at"] = time.time()
                self._json(200, {"ok": True})
            elif path == "/echo/settings":
                # From the settings page. A custom header can't be sent cross-site without a CORS
                # preflight (which this server never approves), so other websites can't post here.
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                changes = {k: v for k, v in json.loads(body or b"{}").items() if k in SETTING_KEYS}
                if not changes:
                    self._json(400, {"error": "no known settings"})
                    return
                with _lock:
                    _echo["inbox"] = (_echo["inbox"] + [{"type": "settings", "changes": changes, "at": time.time()}])[-20:]
                self._json(200, {"ok": True, "queued": sorted(changes)})
            elif path == "/echo/screen":
                # From the Echo: a JPEG, or an empty body when its app isn't on screen.
                with _lock:
                    _echo["screen"] = body or None
                    _echo["screen_at"] = time.time()
                self._json(200, {"ok": True})
            elif path == "/echo/screen/watch":
                # The settings page is showing the live view: ask the Echo for pictures for a while.
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                with _lock:
                    if not any(i.get("type") == "screen" for i in _echo["inbox"]):
                        _echo["inbox"] = (_echo["inbox"] + [{"type": "screen", "at": time.time()}])[-20:]
                self._json(200, {"ok": True})
            elif path == "/echo/blob":
                # The settings page's blob playground: one thing to show for a few seconds.
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                d = json.loads(body or b"{}")
                kind, value = str(d.get("kind", "")), str(d.get("value", ""))
                if kind not in BLOB_KINDS or not value or len(value) > 40:
                    self._json(400, {"error": "unknown kind or value"})
                    return
                with _lock:
                    _echo["inbox"] = (_echo["inbox"] + [{"type": "blob", "kind": kind, "value": value, "at": time.time()}])[-20:]
                self._json(200, {"ok": True})
            elif path == "/presence/device":
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                d = json.loads(body or b"{}")
                ok = set_presence_device(str(d.get("mac", "")), str(d.get("label", "")))
                self._json(200 if ok else 400, {"ok": ok})
            elif path == "/desktop":
                # From the laptop's desktop-sync (same cross-site guard as the settings page).
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                try:
                    info = save_desktop(json.loads(body or b"{}"))
                except (ValueError, TypeError) as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(200, {"ok": True, "colors": len(info["colors"]), "wallpaper": info["wallpaper"]})
            elif path == "/homelab/restart":
                # From the Echo's homelab page (press and hold a container, then confirm).
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                ok, msg = restart_container(str(json.loads(body or b"{}").get("name", "")))
                print(f"restart request -> {'ok' if ok else 'refused'}: {msg}", flush=True)
                self._json(200 if ok else 409, {"ok": ok, "message": msg})
            elif path == "/echo/announce":
                # From the settings page (same cross-site guard as /echo/settings).
                if self.headers.get("X-Requested-With") != "echo-settings":
                    self._json(403, {"error": "missing header"})
                    return
                text = str(json.loads(body or b"{}").get("text", "")).strip()[:300]
                if not text:
                    self._json(400, {"error": "empty"})
                    return
                with _lock:
                    _echo["inbox"] = (_echo["inbox"] + [{"type": "announce", "text": text, "at": time.time()}])[-20:]
                self._json(200, {"ok": True})
            elif path == "/echo/inbox":
                if not hmac.compare_digest(self.headers.get("X-Inbox-Token", ""), inbox_token()):
                    self._json(403, {"error": "bad inbox token"})
                    return
                item = json.loads(body or b"{}")
                if item.get("type") not in ("timer", "announce", "poke"):
                    self._json(400, {"error": "type must be timer, announce or poke"})
                    return
                item["at"] = time.time()
                with _lock:
                    _echo["inbox"] = (_echo["inbox"] + [item])[-20:]
                self._json(200, {"ok": True})
            elif path == "/echo/register":
                accepted = register_echo_key(json.loads(body or b"{}").get("key", ""))
                self._json(200 if accepted else 403, {"ok": accepted})
            elif path == "/echo/backup":
                if not self._key_ok():
                    self._json(403, {"error": "unknown echo key"})
                    return
                self._json(200, {"saved": save_backup(body)})
            else:
                self._json(404, {"error": "not found"})
        except json.JSONDecodeError:
            self._json(400, {"error": "bad JSON"})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def log_message(self, fmt, *args):  # quiet; journald has errors
        pass


if __name__ == "__main__":
    if sys.argv[1:] == ["reset-echo-key"]:
        _secret_file("echo-key").unlink(missing_ok=True)
        print("Forgot the Echo's backup key; the next Echo to register is trusted.")
        sys.exit(0)
    RELEASES.mkdir(parents=True, exist_ok=True)
    inbox_token()
    threading.Thread(target=_presence_loop, daemon=True).start()
    print(f"echo-hub on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
