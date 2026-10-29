"""Tests for echo-hub's own logic (no network, temp dirs)."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ["ECHO_HUB_DATA"] = tempfile.mkdtemp()
os.environ["ECHO_PHOTOS"] = tempfile.mkdtemp()
os.environ["ECHO_PHOTO_CACHE"] = tempfile.mkdtemp()
import echo_hub  # noqa: E402


class EchoHubTest(unittest.TestCase):
    def test_health_goes_stale(self):
        echo_hub._echo["heartbeat_at"] = 0.0
        self.assertFalse(echo_hub.echo_health()[0])
        echo_hub._echo["heartbeat_at"] = time.time()
        self.assertTrue(echo_hub.echo_health()[0])
        self.assertFalse(echo_hub.echo_health(time.time() + echo_hub.HEARTBEAT_STALE_S + 5)[0])

    def test_kuma_health(self):
        now = time.time()
        echo_hub._echo["link_down_since"] = 0.0
        echo_hub._echo["heartbeat"] = {"version": "1", "link": "connected", "health": {"recent_errors": []}}
        echo_hub._echo["heartbeat_at"] = now
        self.assertTrue(echo_hub.echo_kuma_health(now)[0])
        # Muse unlinked: fine for a few minutes, then a problem.
        echo_hub._echo["heartbeat"] = {"link": "connecting", "health": {}}
        self.assertTrue(echo_hub.echo_kuma_health(now)[0])
        echo_hub._echo["heartbeat_at"] = now + 400
        ok, info = echo_hub.echo_kuma_health(now + 400)
        self.assertFalse(ok)
        self.assertIn("Muse link", info["problems"][0])
        # A recent crash.
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 60))
        echo_hub._echo["heartbeat"] = {"link": "connected", "health": {"recent_errors": [f"{stamp}  CRASH in main: boom"]}}
        echo_hub._echo["heartbeat_at"] = now
        self.assertFalse(echo_hub.echo_kuma_health(now)[0])
        self.assertTrue(echo_hub.echo_kuma_health(now + 1000)[0] is False)  # heartbeat stale by then
        echo_hub._echo["heartbeat_at"] = now + 1000
        self.assertTrue(echo_hub.echo_kuma_health(now + 1000)[0])
        echo_hub._echo["heartbeat_at"] = 0.0
        self.assertFalse(echo_hub.echo_kuma_health()[0])

    def test_visitor_staying(self):
        now = time.time()
        echo_hub._echo["settings"] = {}
        echo_hub._visitors[:] = [{"mac": "aa:bb", "name": "Sams-iPhone.lan", "at": now - 400},
                                 {"mac": "cc:dd", "name": "", "at": now - 60}]
        neigh = {"aa:bb": ("192.168.1.50", "REACHABLE"), "cc:dd": ("192.168.1.51", "REACHABLE")}
        ev = echo_hub.visitors_staying(neigh, now)
        self.assertEqual(1, len(ev))
        self.assertIn("Sams iPhone", ev[0]["body"])
        self.assertEqual([], echo_hub.visitors_staying(neigh, now))  # once each
        # Gone again before the few minutes were up: nothing.
        echo_hub._visitors.append({"mac": "ee:ff", "name": "", "at": now - 400})
        self.assertEqual([], echo_hub.visitors_staying({"ee:ff": ("192.168.1.52", "FAILED")}, now))
        # Guest mode already on: nothing.
        echo_hub._visitors.append({"mac": "11:22", "name": "", "at": now - 400})
        echo_hub._echo["settings"] = {"guest_until_ms": (now + 3600) * 1000}
        self.assertEqual([], echo_hub.visitors_staying({"11:22": ("192.168.1.53", "STALE")}, now))
        echo_hub._echo["settings"] = None
        echo_hub._visitors.clear()

    def test_ring_phone(self):
        echo_hub._echo["phone_waiting_at"] = 0.0
        self.assertFalse(echo_hub.ring_phone()["listening"])
        echo_hub._echo["phone_waiting_at"] = time.time()
        r = echo_hub.ring_phone()
        self.assertTrue(r["listening"])
        self.assertGreater(echo_hub._echo["find_phone_at"], 0)

    def test_fun_actions(self):
        self.assertEqual({"type": "fortune"}, echo_hub.phone_action({"action": "fortune"}))
        self.assertEqual("rustyletter", echo_hub.phone_action({"action": "letter", "text": "Hi Puff!"})["type"])
        self.assertIsNone(echo_hub.phone_action({"action": "letter", "text": "  "}))
        self.assertIsNone(echo_hub.phone_action({"action": "settle", "choice": "umbrella"}))  # the debate was taken out
        self.assertIsNone(echo_hub.phone_action({"action": "outfit", "name": "../x"}))
        self.assertEqual("SUNHAT", echo_hub.phone_action({"action": "outfit", "name": "SUNHAT"})["name"])
        self.assertIsNone(echo_hub.phone_action({"action": "pettrick", "trick": "Shell spin"}))

    def test_fun_events(self):
        old = {"friends": {"friends": [{"id": "a", "name": "Pip"}], "costumeMonth": "2026-09", "letters": []}}
        new = {"friends": {"friends": [{"id": "a", "name": "Pip"}], "costumeMonth": "2026-10",
                           "letters": [{"atMs": 5, "friendId": "rusty", "text": "Hi Puff", "fromBlob": False}],
                           "debate": {"date": "2026-10-17", "friendId": "a", "puff": "umbrella", "friend": "raincoat"},
                           "mystery": {"date": "2026-10-16", "friendId": "a", "clues": ["It yawned"]}}}
        kinds = sorted(e["kind"] for e in echo_hub.world_events(old, new))
        self.assertEqual(["costume"], kinds)  # and not Rusty's own letter

    def test_weekly_report_event(self):
        old = {"friends": {}, "week_recap_ms": 1}
        new = {"friends": {}, "week_recap_ms": 2, "week_recap": "📚 School..."}
        ev = echo_hub.world_events(old, new)
        self.assertEqual(["weekly"], [e["kind"] for e in ev])
        self.assertEqual([], echo_hub.world_events(new, new))

    def test_key_is_trust_on_first_use(self):
        echo_hub._secret_file("echo-key").unlink(missing_ok=True)
        self.assertFalse(echo_hub.register_echo_key("short"))
        first = "a" * 40
        self.assertTrue(echo_hub.register_echo_key(first))
        self.assertTrue(echo_hub.register_echo_key(first))
        self.assertFalse(echo_hub.register_echo_key("b" * 40))

    def test_backups_rotate(self):
        for i in range(echo_hub.BACKUPS_KEPT + 3):
            name = echo_hub.save_backup(json.dumps({"n": i}).encode())
            os.utime(echo_hub._backup_dir() / name)
            time.sleep(1.01) if i == 0 else None
        files = list(echo_hub._backup_dir().glob("backup-*.json"))
        self.assertLessEqual(len(files), echo_hub.BACKUPS_KEPT)
        self.assertEqual(json.loads(echo_hub.latest_backup())["n"], echo_hub.BACKUPS_KEPT + 2)

    def test_photos_list_and_reject_paths(self):
        Path(echo_hub.PHOTOS, "a.jpg").write_bytes(b"not really a jpeg")
        Path(echo_hub.PHOTOS, ".hidden.jpg").write_bytes(b"x")
        Path(echo_hub.PHOTOS, "notes.txt").write_text("x")
        self.assertEqual([p["name"] for p in echo_hub.photo_list()], ["a.jpg"])
        self.assertIsNone(echo_hub.photo_bytes("../echo_hub.py"))
        self.assertIsNone(echo_hub.photo_bytes(".hidden.jpg"))
        self.assertEqual(echo_hub.photo_bytes("a.jpg"), b"not really a jpeg")

    def test_inbox_token_is_stable(self):
        self.assertEqual(echo_hub.inbox_token(), echo_hub.inbox_token())


class SpeedAndRestartTest(unittest.TestCase):
    def test_speed_history_reads_the_log(self):
        log = Path(tempfile.mkdtemp()) / "speed.jsonl"
        now = time.time()
        log.write_text("\n".join([json.dumps({"t": now - 100, "down_mbps": 300.0, "up_mbps": 90.0, "ping_ms": 20}),
                                   "not json", json.dumps({"t": now - 40 * 86400, "down_mbps": 1.0})]))
        old = echo_hub.SPEED_LOG
        echo_hub.SPEED_LOG = log
        try:
            self.assertEqual([r["down"] for r in echo_hub.speed_history()], [300.0])
        finally:
            echo_hub.SPEED_LOG = old

    def test_restart_refuses_unknown_containers(self):
        old = echo_hub._containers
        echo_hub._containers = lambda: [{"name": "real", "state": "running", "health": ""}]
        try:
            self.assertFalse(echo_hub.restart_container("../etc")[0])
            self.assertEqual(echo_hub.restart_container("nope")[1], "no such container")
        finally:
            echo_hub._containers = old


class SettingsPageTest(unittest.TestCase):
    def test_neighbours_skip_failed(self):
        text = ("192.168.1.85 dev wlan0 lladdr DC:91:BF:0E:59:6C REACHABLE\n"
                "192.168.1.9 dev wlan0  FAILED\n"
                "192.168.1.64 dev wlan0 lladdr 20:0b:cf:95:95:63 STALE\n")
        n = echo_hub.parse_neighbours(text)
        self.assertEqual(n["dc:91:bf:0e:59:6c"], ("192.168.1.85", "REACHABLE"))
        self.assertEqual(len(n), 2)

    def test_presence_home_then_away(self):
        self.assertFalse(echo_hub.set_presence_device("not a mac", ""))
        self.assertTrue(echo_hub.set_presence_device("AA:BB:CC:DD:EE:FF", "Phone"))
        seen = {"on": True}
        orig = (echo_hub._ping, echo_hub._sweep, echo_hub._neighbours)
        echo_hub._ping = lambda ip: None
        echo_hub._sweep = lambda: None
        echo_hub._neighbours = lambda: {"aa:bb:cc:dd:ee:ff": ("192.168.1.50", "REACHABLE")} if seen["on"] else {}
        try:
            t = 1_000_000.0
            echo_hub.presence_tick(t)
            self.assertTrue(echo_hub._presence["home"])
            seen["on"] = False
            echo_hub.presence_tick(t + 60)
            self.assertTrue(echo_hub._presence["home"], "a few missed looks aren't leaving")
            echo_hub.presence_tick(t + echo_hub.AWAY_AFTER_S + 1)
            self.assertFalse(echo_hub._presence["home"])
            self.assertEqual(echo_hub._presence["since"], t, "left when last seen")
            seen["on"] = True
            echo_hub.presence_tick(t + 5000)
            self.assertTrue(echo_hub._presence["home"])
            self.assertEqual(echo_hub._presence["since"], t + 5000)
        finally:
            echo_hub._ping, echo_hub._sweep, echo_hub._neighbours = orig
            echo_hub.set_presence_device("", "")

    def test_desktop_needs_colours_and_jpeg(self):
        import base64
        colors = {k: "#123456" for k in echo_hub.DESKTOP_KEYS}
        with self.assertRaises(ValueError):
            echo_hub.save_desktop({"colors": {"primary": "#fff"}})
        with self.assertRaises(ValueError):
            echo_hub.save_desktop({"colors": colors, "wallpaper_b64": base64.b64encode(b"PNG...").decode()})
        info = echo_hub.save_desktop({"colors": dict(colors, junk="#000000"),
                                      "wallpaper_b64": base64.b64encode(b"\xff\xd8hello").decode()})
        self.assertTrue(info["wallpaper"])
        self.assertNotIn("junk", echo_hub.desktop()["colors"])

    def test_visitors_only_new_private_addresses(self):
        echo_hub._known_file().unlink(missing_ok=True)
        lan = {"192.168.1.5", "192.168.1.6", "192.168.1.7"}
        first = echo_hub.check_visitors({"aa:00:00:00:00:01": ("192.168.1.5", "REACHABLE")}, lan, 1000.0)
        self.assertEqual(first, [], "the first look only learns")
        orig = echo_hub.socket.gethostbyaddr
        echo_hub.socket.gethostbyaddr = lambda ip: ("guest-phone.lan", [], [ip])
        try:
            new = echo_hub.check_visitors({"aa:00:00:00:00:01": ("192.168.1.5", "REACHABLE"),
                                           "da:11:22:33:44:55": ("192.168.1.6", "REACHABLE"),   # private: a phone
                                           "00:11:22:33:44:66": ("192.168.1.7", "REACHABLE"),   # a real, fixed one
                                           "de:ad:00:00:00:01": ("172.17.0.2", "REACHABLE")}, lan, 2000.0)
        finally:
            echo_hub.socket.gethostbyaddr = orig
        self.assertEqual([v["mac"] for v in new], ["da:11:22:33:44:55"])
        self.assertEqual(new[0]["name"], "guest-phone.lan")
        self.assertEqual(echo_hub.check_visitors({"da:11:22:33:44:55": ("192.168.1.6", "REACHABLE")}, lan, 3000.0), [])

    def test_album_keeps_one_per_day(self):
        self.assertFalse(echo_hub.save_album("../x", "c", b"\xff\xd8"))
        self.assertFalse(echo_hub.save_album("2026-10-11", "c", b"PNG"))
        self.assertTrue(echo_hub.save_album("2026-10-11", "first", b"\xff\xd8a"))
        self.assertTrue(echo_hub.save_album("2026-10-11", "second", b"\xff\xd8b"))
        self.assertEqual([a["caption"] for a in echo_hub.album_list()], ["second"])
        self.assertTrue(echo_hub.save_album("2026-10-11", "tea", b"\xff\xd8c", "friends"))
        self.assertTrue(echo_hub.save_album("2026-10-11", "catch", b"\xff\xd8d", "friends"))
        self.assertEqual(len(echo_hub.album_list("friends")), 2)
        self.assertFalse(echo_hub.save_album("2026-10-11", "x", b"\xff\xd8", "nope"))

    def test_page_script_parses(self):
        """A syntax error anywhere in the page's script leaves it stuck on "Loading…"."""
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node isn't installed")
        script = re.search(r"<script>(.*)</script>", echo_hub.SETTINGS_PAGE.read_text(), re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(script)
        r = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_page_exists_and_keys_cover_page_fields(self):
        html = echo_hub.SETTINGS_PAGE.read_text()
        import re
        names = set(re.findall(r'<(?:input|select)[^>]*name="([a-z_]+)"', html))
        self.assertTrue(names <= echo_hub.SETTING_KEYS, names - echo_hub.SETTING_KEYS)


if __name__ == "__main__":
    unittest.main()


class PhoneTest(unittest.TestCase):
    def test_world_events(self):
        old = {"friends": {"friends": [{"id": "a", "name": "Pip"}], "letters": [{"atMs": 1, "friendId": "a", "text": "hi"}],
                           "stickers": {}, "cards": []}, "street": {"snowman": False}}
        new = {"friends": {"friends": [{"id": "a", "name": "Pip"}], "penpal": {"name": "Sora"},
                           "letters": [{"atMs": 1, "friendId": "a", "text": "hi"}, {"atMs": 2, "friendId": "penpal", "text": "Dear Puff"},
                                       {"atMs": 3, "friendId": "rusty", "fromBlob": True, "text": "Dear Rusty"},
                                       {"atMs": 4, "friendId": "a", "fromBlob": True, "text": "reply"}],
                           "stickers": {"books_5": "2026-10-12"}, "cards": [{"date": "2026-11-26", "holiday": "Thanksgiving", "text": "Happy!"}]},
               "street": {"snowman": True},
               "catalog": {"stickers": [{"id": "books_5", "emoji": "📚", "title": "Read 5 books", "who": "puff"}]}}
        ev = echo_hub.world_events(old, new, "Puff")
        titles = [e["title"] for e in ev]
        self.assertIn("✉️ A letter for Puff from Sora", titles)
        self.assertIn("💌 Puff wrote you a letter", titles)
        self.assertTrue(any("Read 5 books" in t for t in titles))
        self.assertTrue(any("Thanksgiving" in t for t in titles))
        self.assertTrue(any(e["kind"] == "snow" for e in ev))
        self.assertEqual(5, len(ev))  # the reply to Pip isn't news
        self.assertEqual([], echo_hub.world_events(None, new))

    def test_events_numbered(self):
        echo_hub._events_file().unlink(missing_ok=True)
        echo_hub.add_events([{"kind": "letters", "title": "a", "body": ""}, {"kind": "cards", "title": "b", "body": ""}])
        got = echo_hub.events_after(0)
        self.assertEqual([1, 2], [e["id"] for e in got["events"]])
        self.assertEqual(2, got["last"])
        self.assertEqual(["b"], [e["title"] for e in echo_hub.events_after(1)["events"]])

    def test_phone_actions(self):
        self.assertEqual("invite", echo_hub.phone_action({"action": "invite", "name": "Pip"})["type"])
        self.assertIsNone(echo_hub.phone_action({"action": "gift", "kind": "car"}))
        self.assertEqual("COOKING", echo_hub.phone_action({"action": "play", "value": "cooking"})["value"])
        self.assertIsNone(echo_hub.phone_action({"action": "play", "value": "a; rm"}))
        self.assertIsNone(echo_hub.phone_action({"action": "settings", "changes": {"theme": "x"}}))
        self.assertEqual({"still_minutes": 60}, echo_hub.phone_action({"action": "settings", "changes": {"still_minutes": 60, "theme": "x"}})["changes"])
        self.assertIsNone(echo_hub.phone_action({"action": "timer", "seconds": 0}))
        self.assertIsNone(echo_hub.phone_action({"action": "game", "game": "chess", "outcome": 1}))
        self.assertIsNone(echo_hub.phone_action({"action": "rm"}))
        self.assertEqual("remote", echo_hub.phone_action({"action": "remote", "target": "tv", "key": "Select"})["type"])
        self.assertIsNone(echo_hub.phone_action({"action": "remote", "target": "tv", "key": "rm -rf"}))
        self.assertIsNotNone(echo_hub.phone_action({"action": "remote", "target": "laptop", "key": "shortcut:screenshot"}))
        self.assertIsNone(echo_hub.phone_action({"action": "remote", "target": "laptop", "key": "shortcut:a b"}))
        self.assertIsNone(echo_hub.phone_action({"action": "card", "text": "  "}))
        self.assertTrue(echo_hub.phone_action({"action": "type", "text": "", "enter": True})["enter"])
        self.assertEqual({"type": "touch", "kind": "tap", "x": 0.5, "y": 0.25}, echo_hub.phone_action({"action": "touch", "kind": "tap", "x": 0.5, "y": 0.25}))
        self.assertIsNone(echo_hub.phone_action({"action": "touch", "kind": "tap", "x": 2, "y": 0.2}))
        self.assertIsNone(echo_hub.phone_action({"action": "touch", "kind": "swipe", "x": 0.9, "y": 0.5}))
        self.assertEqual(0.1, echo_hub.phone_action({"action": "touch", "kind": "swipe", "x": 0.9, "y": 0.5, "x2": 0.1, "y2": 0.5})["x2"])

    def test_photo_and_prefs(self):
        self.assertIsNone(echo_hub.save_phone_photo(b"GIF89a"))
        pid = echo_hub.save_phone_photo(b"\xff\xd8\xff" + b"0" * 10)
        self.assertRegex(pid, r"^[0-9a-f]{16}$")
        self.assertTrue(all(echo_hub.phone_prefs().values()))
        self.assertFalse(echo_hub.set_phone_prefs({"snow": False, "bogus": False, "cards": "no"})["snow"])
        self.assertNotIn("bogus", echo_hub.phone_prefs())
        self.assertTrue(echo_hub.phone_prefs()["cards"])

    def test_phone_needs_token(self):
        import threading, urllib.request, urllib.error
        srv = echo_hub.ThreadingHTTPServer(("127.0.0.1", 0), echo_hub.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/phone/state")
            self.assertEqual(401, e.exception.code)
            req = urllib.request.Request(base + "/phone/ping", headers={"Authorization": "Bearer " + echo_hub.phone_token()})
            self.assertEqual({"ok": True}, json.loads(urllib.request.urlopen(req).read()))
            req = urllib.request.Request(base + "/phone/ping", headers={"Authorization": "Bearer wrong"})
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(req)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/phone/pairing")
            self.assertEqual(403, e.exception.code)
        finally:
            srv.shutdown()
