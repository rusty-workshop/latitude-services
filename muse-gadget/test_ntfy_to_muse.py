import json
import unittest

from ntfy_to_muse import Forwarder

CFG = {"MIN_PRIORITY": "3", "URGENT_PRIORITY": "4", "SKIP_TITLES": "Daily Digest", "SESSION_ID": ""}


def msg(title, body, priority=None, event="message"):
    e = {"event": event, "title": title, "message": body}
    if priority:
        e["priority"] = priority
    return json.dumps(e)


class ForwarderTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.clock = 1000.0
        self.fwd = Forwarder(CFG, send=self.sent.append, now=lambda: self.clock)

    def test_filters_and_formats(self):
        self.assertIsNone(self.fwd.handle(msg("Daily Digest", "All clear")))
        self.assertIsNone(self.fwd.handle(msg("x", "y", event="keepalive")))
        self.assertIsNone(self.fwd.handle("not json"))
        down = self.fwd.handle(msg("Latitude DOWN", "SSH unreachable", priority=5))
        self.assertTrue(down.startswith("URGENT server alert from my homelab (Latitude DOWN): SSH unreachable"))
        up = self.fwd.handle(msg("Latitude back up", "SSH reachable again", priority=3))
        self.assertTrue(up.startswith("Server notice"))
        self.assertEqual(2, len(self.sent))

    def test_low_priority_dropped_when_min_raised(self):
        fwd = Forwarder({**CFG, "MIN_PRIORITY": "4"}, send=self.sent.append, now=lambda: self.clock)
        self.assertIsNone(fwd.handle(msg("t", "fyi", priority=3)))
        self.assertIsNotNone(fwd.handle(msg("t", "bad", priority=4)))

    def test_dedupes_and_rate_limits(self):
        self.fwd.handle(msg("Kuma", "judgment-bot down", priority=5))
        self.assertIsNone(self.fwd.handle(msg("Kuma", "judgment-bot down", priority=5)))
        self.clock += 601
        self.assertIsNotNone(self.fwd.handle(msg("Kuma", "judgment-bot down", priority=5)))
        for i in range(20):
            self.fwd.handle(msg("Kuma", f"thing {i} down", priority=5))
        self.assertEqual(10, len(self.sent))


if __name__ == "__main__":
    unittest.main()
