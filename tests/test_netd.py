"""Bridge WiFi manager / setup page tests (simulated WiFi): python3 -m unittest discover -s tests"""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))

import ircam_netd as N  # noqa: E402


def wait_for(cond, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


class Parsing(unittest.TestCase):
    def test_split_terse_handles_escapes(self):
        self.assertEqual(N._split_terse(r"yes:My\:Net:72"), ["yes", "My:Net", "72"])
        self.assertEqual(N._split_terse(r"a\\b:c"), ["a\\b", "c"])
        self.assertEqual(N._split_terse("::"), ["", "", ""])

    def test_classify_error(self):
        self.assertIn("Wrong password", N.classify_error("Error: Secrets were required, but not provided."))
        self.assertIn("not found", N.classify_error("Error: No network with SSID 'x' found."))
        self.assertIn("in time", N.classify_error("timed out"))

    def test_validation(self):
        self.assertTrue(N.valid_ssid("Home"))
        self.assertFalse(N.valid_ssid(""))
        self.assertFalse(N.valid_ssid("x" * 33))
        self.assertTrue(N.valid_psk(""))
        self.assertFalse(N.valid_psk("short"))
        self.assertTrue(N.valid_psk("longenough"))
        self.assertFalse(N.HOSTNAME_RE.match("-bad"))
        self.assertTrue(N.HOSTNAME_RE.match("p2pro-shop"))


class ManagerFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_path = str(Path(self.tmp) / "netd.json")
        cfg = N.load_config(self.cfg_path)
        cfg.update(grace_s=0, connect_timeout_s=2)
        self.nm = N.FakeNM()
        self.mgr = N.Manager(self.nm, cfg, self.cfg_path, str(Path(self.tmp) / "status.json"))
        page = (ROOT / "bridge" / "setup.html").read_bytes()
        self.web = N.Web(("127.0.0.1", 0), self.mgr, page, fake=True)
        self.port = self.web.server_address[1]
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.mgr.tick()                          # no saved network -> hotspot

    def tearDown(self):
        self.web.shutdown()
        self.web.server_close()

    def post(self, path, body, host="127.0.0.1"):
        req = urllib.request.Request(f"http://{host}:{self.port}{path}", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_config_has_random_secrets(self):
        cfg = json.loads(Path(self.cfg_path).read_text())
        self.assertRegex(cfg["pin"], r"^\d{6}$")
        self.assertGreaterEqual(len(cfg["hotspot_password"]), 8)
        self.assertTrue(cfg["hotspot_ssid"].startswith("P2Pro-"))

    def test_starts_hotspot_without_saved_networks(self):
        self.assertEqual(self.mgr.mode, "hotspot")
        self.assertEqual(self.nm.active, N.HOTSPOT_CON)
        self.assertTrue(self.mgr.scan_cache)      # scanned before the hotspot came up

    def test_wrong_password_falls_back_to_hotspot(self):
        code, _ = self.post("/api/connect", {"ssid": "HomeRouter", "password": "wrongpass1"})
        self.assertEqual(code, 200)
        self.assertTrue(wait_for(lambda: self.mgr.last_error is not None and self.nm.active == N.HOTSPOT_CON))
        self.assertIn("Wrong password", self.mgr.last_error)
        self.assertEqual(self.nm.saved(), [])     # the bad profile is not kept

    def test_good_password_switches_and_is_saved(self):
        self.post("/api/connect", {"ssid": "HomeRouter", "password": "password123"})
        self.assertTrue(wait_for(lambda: self.mgr.mode == "client" and self.nm.active == "wifi-HomeRouter"))
        self.assertEqual([s["ssid"] for s in self.nm.saved()], ["HomeRouter"])
        self.assertIsNone(self.mgr.last_error)

    def test_lost_network_brings_back_hotspot(self):
        self.post("/api/connect", {"ssid": "HomeRouter", "password": "password123"})
        self.assertTrue(wait_for(lambda: self.mgr.mode == "client"))
        self.nm.air = [n for n in self.nm.air if n["ssid"] != "HomeRouter"]
        self.nm.active = None                    # router gone
        self.mgr.tick()                          # notices the disconnect
        self.mgr.since_disconnected -= 1
        self.mgr.tick()                          # grace (0 s) over -> hotspot
        self.assertEqual(self.nm.active, N.HOTSPOT_CON)

    def test_changes_from_the_home_network_need_the_pin(self):
        self.web.fake = False                    # 127.0.0.1 is no longer "on the hotspot"
        code, body = self.post("/api/forget", {"name": "x"})
        self.assertEqual(code, 403)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/forget", b'{"name":"x"}',
                                     {"Content-Type": "application/json", "X-PIN": self.mgr.cfg["pin"]})
        with self.assertRaises(urllib.error.HTTPError) as cm:   # authorized; 404 = no such network
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 404)
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/status", timeout=5) as r:
            st = json.loads(r.read())
        self.assertNotIn("pin", st)
        self.assertNotIn("hotspot_password", st)

    def test_captive_redirect_and_dismiss(self):
        def get(path, host):
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers={"Host": host})
            opener = urllib.request.build_opener(NoRedirect)
            try:
                with opener.open(req, timeout=5) as r:
                    return r.status, r.headers.get("Location")
            except urllib.error.HTTPError as e:
                return e.code, e.headers.get("Location")
        self.assertEqual(get("/generate_204", "connectivitycheck.gstatic.com"), (302, "http://10.42.0.1/"))
        self.post("/api/dismiss", {})
        self.assertEqual(get("/generate_204", "connectivitycheck.gstatic.com")[0], 204)
        self.assertEqual(get("/", "localhost")[0], 200)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


if __name__ == "__main__":
    unittest.main()
