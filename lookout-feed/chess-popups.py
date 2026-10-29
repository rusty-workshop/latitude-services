#!/usr/bin/env python3
"""Pop up "Your move" on Family Room TV when a correspondence opponent moves.

Run every minute by chess-popups.timer. Asks the gambit server which games
are waiting on Rusty; a game that newly became his turn gets a pop-up through
tv-button-remap's MessageReceiver. If the TV is off, it stays pending and
pops up the next time the TV is on (if it's still his move).
"""
import json
import pathlib
import shlex
import subprocess
import urllib.request

HOME = pathlib.Path.home()
STATE = HOME / ".cache/chess-popups.json"
GAMBIT = "http://127.0.0.1:8460"
TV = "192.168.1.75"
ADB_TARGET = f"{TV}:5555"


def watch_key():
    for line in (HOME / "lichess-watch/gambit.env").read_text().splitlines():
        if line.startswith("WATCH_KEY="):
            return line.split("=", 1)[1].strip().strip('"')


def tv_is_on():
    body = json.dumps({"method": "getPowerStatus", "id": 1, "params": [], "version": "1.0"}).encode()
    try:
        with urllib.request.urlopen(f"http://{TV}/sony/system", body, timeout=5) as r:
            return json.load(r)["result"][0]["status"] == "active"
    except (OSError, ValueError, KeyError, IndexError):
        return False


def popup(title, text):
    subprocess.run(["adb", "connect", ADB_TARGET], capture_output=True, timeout=15)
    cmd = " ".join(shlex.quote(a) for a in [
        "am", "broadcast", "-n", "com.rusty.tvremap/.MessageReceiver",
        "--es", "title", title, "--es", "text", text])
    out = subprocess.run(["adb", "-s", ADB_TARGET, "shell", cmd],
                         capture_output=True, text=True, timeout=15).stdout
    return "result=0" in out


def left(secs):
    if secs is None:
        return ""
    h = secs // 3600
    return f"  ·  {h // 24}d {h % 24}h left" if h >= 24 else f"  ·  {h}h {secs % 3600 // 60}m left"


def main():
    req = urllib.request.Request(GAMBIT + "/home", headers={"Authorization": f"Bearer {watch_key()}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        games = json.load(r)["games"]
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    first_run = not STATE.exists()
    mine = {g["id"]: g for g in games if g["myTurn"]}
    # Games where it's no longer my move reset, so their next turn pops up again.
    shown = {gid: v for gid, v in state.items() if gid in mine}
    pending = [g for gid, g in mine.items() if gid not in shown]
    if first_run:
        shown = {gid: True for gid in mine}  # don't announce turns that were already waiting
        pending = []
    if pending and tv_is_on():
        for g in pending:
            text = f"vs {g['opponent']['name']}{left(g.get('secondsLeft'))}"
            if popup("Your move", text):
                shown[g["id"]] = True
                print(f"popup: {text}")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(shown))


if __name__ == "__main__":
    main()
