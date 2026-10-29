#!/usr/bin/env python3
"""Lightning near home from NOAA's GOES-19 Geostationary Lightning Mapper.

GLM publishes a ~340 KB file every 20 seconds to the public noaa-goes19 S3
bucket. That's a lot to pull all day, so this only downloads while someone
is looking: lookout-feed touches .lightning-wanted whenever the Sky page asks
for planes, and this skips fetching unless that happened in the last 2
minutes. Flashes within RANGE_NM of home from the last 15 minutes are written
to lightning.json for the feed.
"""
import io
import json
import math
import pathlib
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import h5py

HERE = pathlib.Path.home() / "latitude-lookout"
WANTED = HERE / ".lightning-wanted"
OUT = HERE / "lightning.json"
LAT, LON = 30.33218, -81.65565
RANGE_NM = 60  # a bit past the 40 nm radar, so storms rolling in show up as a count
KEEP = timedelta(minutes=15)
BUCKET = "https://noaa-goes19.s3.amazonaws.com"
UA = {"User-Agent": "lookout-lightning (Rusty's homelab display)"}


def get(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def keys_for(hour):
    prefix = hour.strftime("GLM-L2-LCFA/%Y/%j/%H/")
    xml = get(f"{BUCKET}/?list-type=2&prefix={prefix}").decode()
    return re.findall(r"<Key>([^<]+)</Key>", xml)


def start_time(key):
    m = re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})", key)
    y, doy, h, mi, s = map(int, m.groups())
    return datetime(y, 1, 1, h, mi, s, tzinfo=timezone.utc) + timedelta(days=doy - 1)


def values(ds):
    """GLM packs lat/lon as scaled integers; unpack like netCDF would."""
    v = ds[()].astype("float64")
    scale = ds.attrs.get("scale_factor")
    offset = ds.attrs.get("add_offset")
    if scale is not None:
        v = v * float(scale[0] if hasattr(scale, "__len__") else scale)
    if offset is not None:
        v = v + float(offset[0] if hasattr(offset, "__len__") else offset)
    return v


def nm(lat, lon):
    dlat = math.radians(lat - LAT)
    dlon = math.radians(lon - LON)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(LAT)) * math.cos(math.radians(lat)) * math.sin(dlon / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(a))


def main():
    seen = set()
    flashes = []  # dicts with lat, lon, t (epoch seconds)
    while True:
        try:
            wanted = WANTED.exists() and time.time() - WANTED.stat().st_mtime < 120
            if wanted:
                now = datetime.now(timezone.utc)
                keys = keys_for(now - timedelta(hours=1)) + keys_for(now)
                fresh = [k for k in keys if k not in seen and now - start_time(k) < KEEP]
                for k in sorted(fresh)[-45:]:  # at most 15 minutes' worth per pass
                    with h5py.File(io.BytesIO(get(f"{BUCKET}/{k}")), "r") as f:
                        lats, lons = values(f["flash_lat"]), values(f["flash_lon"])
                    t = start_time(k).timestamp()
                    for la, lo in zip(lats, lons):
                        if abs(la - LAT) < 1.2 and abs(lo - LON) < 1.4 and nm(la, lo) <= RANGE_NM:
                            flashes.append({"lat": round(float(la), 4), "lon": round(float(lo), 4), "t": t})
                    seen.add(k)
            cutoff = time.time() - KEEP.total_seconds()
            flashes = [f for f in flashes if f["t"] >= cutoff]
            seen = {k for k in seen if start_time(k).timestamp() >= cutoff - 600}
            tmp = OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps({"updated": int(time.time()), "active": wanted,
                                       "rangeNm": RANGE_NM, "flashes": flashes}))
            tmp.replace(OUT)
        except Exception as e:
            print(f"lightning: {e!r}", flush=True)
        time.sleep(20)


if __name__ == "__main__":
    main()
