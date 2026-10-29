#!/bin/sh
# One internet speed test, appended to ~/latitude-lookout/speedtests.jsonl
# for Lookout's Server page. Run every 3 hours by speedtest-log.timer.
out=$(speedtest-cli --json --secure 2>/dev/null) || exit 0  # offline: just skip this one
printf '%s\n' "$out" | python3 -c '
import json, sys, time
d = json.load(sys.stdin)
print(json.dumps({"t": int(time.time()), "down": round(d["download"] / 1e6, 1),
                  "up": round(d["upload"] / 1e6, 1), "ping": round(d["ping"]),
                  "server": d["server"]["sponsor"]}))' >> "$HOME/latitude-lookout/speedtests.jsonl"
