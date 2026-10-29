#!/usr/bin/env python3
"""Forward real alerts from the homelab's ntfy topic to Muse.

Subscribes to https://ntfy.sh/<topic>/json and hands each alert worth
announcing to `musegadget send-user-msg`, so Muse hears it from the Latitude
gadget (and the Echo Show announces Muse's take on it).

Config: ~/.config/latitude-muse-gadget/ntfy.env
    NTFY_TOPIC=...            required
    MIN_PRIORITY=3            ntfy priority 1-5; 3 = default
    URGENT_PRIORITY=4         at or above this, always forwarded
    SKIP_TITLES=Daily Digest  comma-separated titles never forwarded
    SESSION_ID=               optional Muse side-chat id (default: main chat)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

CONFIG = Path.home() / ".config/latitude-muse-gadget/ntfy.env"
STATE = Path.home() / ".local/state/latitude-muse-gadget/ntfy-last-id"
MUSEGADGET = str(Path.home() / ".local/bin/musegadget")
DEDUPE_S = 600
MAX_PER_HOUR = 10


def load_config(path: Path = CONFIG) -> dict:
    cfg = {"MIN_PRIORITY": "3", "URGENT_PRIORITY": "4", "SKIP_TITLES": "Daily Digest", "SESSION_ID": ""}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip()
    if not cfg.get("NTFY_TOPIC"):
        sys.exit(f"NTFY_TOPIC missing from {path}")
    return cfg


class Forwarder:
    def __init__(self, cfg: dict, send=None, now=time.time):
        self.min_priority = int(cfg["MIN_PRIORITY"])
        self.urgent = int(cfg["URGENT_PRIORITY"])
        self.skip = {t.strip().lower() for t in cfg["SKIP_TITLES"].split(",") if t.strip()}
        self.session_id = cfg.get("SESSION_ID", "")
        self.send = send or self._send
        self.now = now
        self.recent: dict[str, float] = {}
        self.sent_times: deque[float] = deque()

    def wanted(self, event: dict) -> bool:
        if event.get("event") != "message":
            return False
        priority = int(event.get("priority") or 3)
        title = (event.get("title") or "").strip()
        if title.lower() in self.skip:
            return False
        return priority >= self.min_priority or priority >= self.urgent

    def handle(self, line: str) -> str | None:
        """Process one JSON line; returns the text sent to Muse, if any."""
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not self.wanted(event):
            return None
        title = (event.get("title") or "").strip()
        body = (event.get("message") or "").strip()
        priority = int(event.get("priority") or 3)
        key = f"{title}\n{body}"
        now = self.now()
        self.recent = {k: t for k, t in self.recent.items() if now - t < DEDUPE_S}
        if key in self.recent:
            return None
        while self.sent_times and now - self.sent_times[0] > 3600:
            self.sent_times.popleft()
        if len(self.sent_times) >= MAX_PER_HOUR:
            return None
        self.recent[key] = now
        self.sent_times.append(now)
        level = "URGENT server alert" if priority >= self.urgent else "Server notice"
        text = (f"{level} from my homelab ({title or 'ntfy'}): {body}\n"
                "Briefly tell me what this means and whether I need to do anything. "
                "Check the Latitude if that helps; don't change anything without asking.")
        self.send(text)
        return text

    def _send(self, text: str) -> None:
        cmd = [MUSEGADGET, "send-user-msg"]
        if self.session_id:
            cmd += ["--session-id", self.session_id]
        subprocess.run(cmd + [text], check=False, timeout=90)


def stream(topic: str, since: str):
    # No `since` means "only new messages"; with an id, resume after it.
    url = f"https://ntfy.sh/{topic}/json" + (f"?since={since}" if since else "")
    req = urllib.request.Request(url, headers={"User-Agent": "latitude-muse-gadget"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            yield raw.decode("utf-8", errors="replace")


def main() -> None:
    cfg = load_config()
    fwd = Forwarder(cfg)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    backoff = 5
    while True:
        since = STATE.read_text().strip() if STATE.exists() else ""
        try:
            for line in stream(cfg["NTFY_TOPIC"], since):
                event_id = json.loads(line).get("id") if line.strip().startswith("{") else None
                sent = fwd.handle(line)
                if sent:
                    print(f"forwarded: {sent.splitlines()[0][:120]}", flush=True)
                if event_id and json.loads(line).get("event") == "message":
                    STATE.write_text(event_id)
                backoff = 5
        except Exception as exc:  # network drop, ntfy restart: reconnect
            print(f"stream ended: {exc}; retrying in {backoff}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    main()
