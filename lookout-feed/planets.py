#!/usr/bin/env python3
"""Tonight's planets for Lookout, printed as JSON.

Run with echo-dashboard-solo's venv (it has Skyfield, already used for ISS
passes). Uses JPL's DE421 ephemeris, downloaded once next to this script.
"Tonight" is sunset to sunrise; a planet counts as visible if it gets 10
degrees above the horizon while the Sun is below -6 degrees.
"""
import json
import pathlib
import sys
from datetime import datetime, timedelta

from skyfield import almanac
from skyfield.api import Loader, wgs84

LAT, LON = 30.33218, -81.65565
HERE = pathlib.Path(__file__).resolve().parent
PLANETS = [("Mercury", "mercury"), ("Venus", "venus"), ("Mars", "mars"),
           ("Jupiter", "jupiter barycenter"), ("Saturn", "saturn barycenter")]
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def main():
    load = Loader(str(HERE), verbose=False)
    ts = load.timescale()
    eph = load("de421.bsp")
    earth, sun = eph["earth"], eph["sun"]
    here = wgs84.latlon(LAT, LON)
    obs = earth + here
    tz = datetime.now().astimezone().tzinfo

    now = ts.now()
    # Tonight's window: next sunset (or now, if it's already dark) to the following sunrise.
    t0, t1 = now, ts.from_datetime(now.utc_datetime() + timedelta(hours=26))
    f = almanac.sunrise_sunset(eph, here)
    times, ups = almanac.find_discrete(t0, t1, f)
    sun_alt_now = obs.at(now).observe(sun).apparent().altaz()[0].degrees
    start = now if sun_alt_now < -0.8 else next((t for t, up in zip(times, ups) if not up), now)
    end = next((t for t, up in zip(times, ups) if up and t.tt > start.tt), t1)

    # Sample the night every 10 minutes.
    n = max(2, int((end.tt - start.tt) * 24 * 6))
    samples = [ts.tt_jd(start.tt + (end.tt - start.tt) * i / (n - 1)) for i in range(n)]
    dark = [obs.at(t).observe(sun).apparent().altaz()[0].degrees < -6 for t in samples]

    out = []
    for name, key in PLANETS:
        body = eph[key]
        best = None
        for t, is_dark in zip(samples, dark):
            if not is_dark:
                continue
            alt, az, _ = obs.at(t).observe(body).apparent().altaz()
            if best is None or alt.degrees > best[0]:
                best = (alt.degrees, az.degrees, t)
        visible = bool(best is not None and best[0] >= 10)
        entry = {"name": name, "visible": visible}
        if best:
            alt, az, t = best
            entry.update(bestTime=t.utc_datetime().astimezone(tz).strftime("%-I:%M %p"),
                         altitude=int(round(float(alt))), direction=COMPASS[int((float(az) + 22.5) // 45) % 8])
        # Rise/set over the next day, for context.
        rs = almanac.risings_and_settings(eph, body, here)
        rt, ry = almanac.find_discrete(now, ts.from_datetime(now.utc_datetime() + timedelta(hours=24)), rs)
        for t, rising in zip(rt, ry):
            entry.setdefault("rises" if rising else "sets",
                             t.utc_datetime().astimezone(tz).strftime("%-I:%M %p"))
        out.append(entry)
    json.dump({"from": start.utc_datetime().astimezone(tz).strftime("%-I:%M %p"),
               "to": end.utc_datetime().astimezone(tz).strftime("%-I:%M %p"), "planets": out}, sys.stdout)


if __name__ == "__main__":
    main()
