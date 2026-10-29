#!/usr/bin/env python3
"""Today's daylight for Lookout, printed as JSON.

Run with echo-dashboard-solo's venv (Skyfield) and the DE421 ephemeris that
planets.py already downloaded. Golden hour is the Sun between -4 and +6
degrees, blue hour between -6 and -4; day length is sunrise to sunset.
"""
import json
import pathlib
import sys
from datetime import date, datetime, timedelta

from skyfield import almanac
from skyfield.api import Loader, wgs84

LAT, LON = 30.33218, -81.65565
HERE = pathlib.Path(__file__).resolve().parent


def main():
    load = Loader(str(HERE), verbose=False)
    ts = load.timescale()
    eph = load("de421.bsp")
    here = wgs84.latlon(LAT, LON)
    obs = eph["earth"] + here
    tz = datetime.now().astimezone().tzinfo
    today = date.today()

    def at(d, hour=0):
        return ts.from_datetime(datetime(d.year, d.month, d.day, hour, tzinfo=tz))

    def crossings(d, degrees):
        """Times the Sun's centre crosses `degrees` altitude on local day d, rising then setting."""
        def above(t):
            return obs.at(t).observe(eph["sun"]).apparent().altaz()[0].degrees > degrees
        above.step_days = 0.02
        times, ups = almanac.find_discrete(at(d), at(d + timedelta(days=1)), above)
        rise = next((t for t, up in zip(times, ups) if up), None)
        sett = next((t for t, up in zip(times, ups) if not up), None)
        return rise, sett

    def sun_times(d):
        times, ups = almanac.find_discrete(at(d), at(d + timedelta(days=1)), almanac.sunrise_sunset(eph, here))
        rise = next((t for t, up in zip(times, ups) if up), None)
        sett = next((t for t, up in zip(times, ups) if not up), None)
        return rise, sett

    def hm(t):
        return t.utc_datetime().astimezone(tz).strftime("%-I:%M %p") if t is not None else None

    def length(d):
        rise, sett = sun_times(d)
        return (sett.tt - rise.tt) * 86400 if rise is not None and sett is not None else 0

    rise, sett = sun_times(today)
    today_len = length(today)
    change = today_len - length(today - timedelta(days=1))
    g_up, g_down = crossings(today, 6)     # golden hour ends (morning) / starts (evening)
    b_up, b_down = crossings(today, -4)    # blue hour ends / golden hour begins, and the reverse
    d_up, d_down = crossings(today, -6)    # civil dawn / dusk

    # Day length for every day of the year, in minutes, for the curve.
    start = date(today.year, 1, 1)
    days = (date(today.year + 1, 1, 1) - start).days
    year = []
    for i in range(days):
        d = start + timedelta(days=i)
        year.append(round(length(d) / 60, 1))

    json.dump({
        "sunrise": hm(rise), "sunset": hm(sett),
        "lengthMin": round(today_len / 60, 1), "changeSec": round(change),
        "goldenMorning": [hm(b_up), hm(g_up)], "goldenEvening": [hm(g_down), hm(b_down)],
        "blueMorning": [hm(d_up), hm(b_up)], "blueEvening": [hm(b_down), hm(d_down)],
        "tomorrowSunrise": hm(sun_times(today + timedelta(days=1))[0]),
        "year": year, "todayIndex": (today - start).days,
        "longest": max(year), "shortest": min(year),
    }, sys.stdout)


if __name__ == "__main__":
    main()
