#!/usr/bin/env python3
"""Ships around JAXPORT and the St. Johns River mouth, from aisstream.io.

Holds aisstream's websocket open for a bounding box around the port and
river mouth, keeps the latest position and details per vessel, and writes
ships.json every 10 seconds for lookout-feed. The free API key goes in
~/latitude-lookout/.aisstream-key (mode 600); without it this just waits.
"""
import asyncio
import json
import pathlib
import time

import websockets

HERE = pathlib.Path.home() / "latitude-lookout"
KEY = HERE / ".aisstream-key"
OUT = HERE / "ships.json"
# St. Johns River from downtown to well offshore, including Blount Island and Mayport.
BOX = [[30.22, -81.80], [30.55, -81.20]]
STALE = 30 * 60

TYPES = [(range(60, 70), "Passenger"), (range(70, 80), "Cargo"), (range(80, 90), "Tanker"),
         (range(31, 33), "Towing"), (range(52, 53), "Tug"), (range(30, 31), "Fishing"),
         (range(35, 36), "Military"), (range(36, 37), "Sailboat"), (range(37, 38), "Pleasure craft"),
         (range(50, 51), "Pilot boat"), (range(55, 56), "Law enforcement")]

ships = {}


def kind(code):
    return next((name for r, name in TYPES if code in r), None)


def write():
    cutoff = time.time() - STALE
    for mmsi in [m for m, s in ships.items() if s["seen"] < cutoff]:
        del ships[mmsi]
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated": int(time.time()), "box": BOX,
                               "ships": [s for s in ships.values() if "lat" in s]}))
    tmp.replace(OUT)


async def stream(key):
    async with websockets.connect("wss://stream.aisstream.io/v0/stream", ping_interval=30) as ws:
        await ws.send(json.dumps({"APIKey": key, "BoundingBoxes": [BOX],
                                  "FilterMessageTypes": ["PositionReport", "StandardClassBPositionReport",
                                                         "ShipStaticData"]}))
        last = 0
        async for raw in ws:
            msg = json.loads(raw)
            meta = msg.get("MetaData", {})
            mmsi = meta.get("MMSI")
            if not mmsi:
                continue
            s = ships.setdefault(mmsi, {"mmsi": mmsi})
            s["seen"] = time.time()
            if meta.get("ShipName", "").strip():
                s["name"] = meta["ShipName"].strip()
            body = msg.get("Message", {})
            if "ShipStaticData" in body:
                st = body["ShipStaticData"]
                s["type"] = kind(st.get("Type") or 0)
                s["destination"] = (st.get("Destination") or "").strip() or None
                d = st.get("Dimension") or {}
                s["length"] = (d.get("A") or 0) + (d.get("B") or 0) or None
            for k in ("PositionReport", "StandardClassBPositionReport"):
                if k in body:
                    p = body[k]
                    s.update(lat=meta.get("latitude"), lon=meta.get("longitude"),
                             speed=round(p.get("Sog") or 0, 1), course=p.get("Cog") or 0,
                             heading=p.get("TrueHeading") if (p.get("TrueHeading") or 511) != 511 else p.get("Cog") or 0)
            if time.time() - last > 10:
                write()
                last = time.time()


async def main():
    while True:
        key = KEY.read_text().strip() if KEY.exists() else ""
        if not key:
            await asyncio.sleep(60)
            continue
        try:
            await stream(key)
        except Exception as e:
            print(f"ships: {e!r}", flush=True)
        await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(main())
