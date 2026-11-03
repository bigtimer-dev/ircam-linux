"""Browser viewer add-on tests: bridge tap -> ircam_web -> WebSocket, and the setup page toggle.
python3 -m unittest tests.test_web"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))
os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="ircam-test-"))

from ircam.net import protocol as P  # noqa: E402
from ircam.sources import NetworkSource  # noqa: E402
import ircam_bridge  # noqa: E402
import ircam_netd  # noqa: E402
import ircam_web  # noqa: E402


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WsClient:
    """Just enough of a WebSocket client for the tests (masked frames out, unmasked in)."""

    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(f"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.s.recv(1)
        assert head.startswith(b"HTTP/1.1 101"), head          # browsers insist on 1.1
        want = base64.b64encode(hashlib.sha1(key.encode() + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        assert b"Sec-WebSocket-Accept: " + want + b"\r\n" in head, head  # RFC 6455 section 4.2.2

    def _exact(self, n):
        return ircam_web.recv_exact(self.s, n)

    def recv(self):
        b0, b1 = self._exact(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._exact(8))[0]
        data = self._exact(n)
        return (b0 & 0x0F), data

    def send_text(self, text):
        data, mask = text.encode(), os.urandom(4)
        self.s.sendall(bytes([0x81, 0x80 | len(data)]) + mask + bytes(x ^ mask[i & 3] for i, x in enumerate(data)))

    def close(self):
        ircam_web.hangup(self.s)


def decode_ws_frame(data):
    """A browser frame message: b"IRWC", u16 count, then u16 length + IRW1 datagram per band."""
    assert data[:4] == b"IRWC"
    n, off = struct.unpack_from("<H", data, 4)[0], 6
    img = np.zeros((192, 256), np.uint16)
    covered = np.zeros(192, bool)
    fid = flags = None
    for _ in range(n):
        ln = struct.unpack_from("<H", data, off)[0]
        codec, flags, fid, _ts, r0, cnt, payload = P.parse(data[off + 2:off + 2 + ln])
        img[r0:r0 + cnt] = P.decode_band(payload, codec, cnt)
        covered[r0:r0 + cnt] = True
        off += 2 + ln
    assert off == len(data) and covered.all()
    return fid, flags, img


class BrowserViewer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ircam-web-")
        cls.tap = os.path.join(cls.tmp, "tap.sock")
        cls.br = ircam_bridge.Bridge(ircam_bridge.DemoCamera(), "127.0.0.1", free_port(socket.SOCK_DGRAM), free_port())
        cls.br.start(tap=cls.tap)
        cls.hub = ircam_web.Hub(cls.tap)
        cls.srv = ircam_web.Server(("127.0.0.1", 0), cls.hub, ROOT / "bridge" / "web")
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        deadline = time.monotonic() + 5
        while not cls.br.cam_ok and time.monotonic() < deadline:
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.hub.stop()
        cls.br.stop()

    def frames(self, ws, n, timeout=8):
        out, status = [], None
        deadline = time.monotonic() + timeout
        while len(out) < n and time.monotonic() < deadline:
            op, data = ws.recv()
            if op == 2:
                out.append(decode_ws_frame(data))
            elif op == 1:
                status = json.loads(data)
        return out, status

    def test_static_files(self):
        for path in ("/", "/palettes.json", "/palettes.bin", "/manifest.webmanifest", "/icon-180.png"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
                self.assertEqual(r.status, 200)
                self.assertTrue(r.read())
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/palettes.json") as r:
            j = json.load(r)
        self.assertEqual(len(j["names"]) * j["size"] * 3, len((ROOT / "bridge/web/palettes.bin").read_bytes()))

    def test_stream_matches_the_bridge_frames(self):
        ws = WsClient(self.port)
        try:
            frames, _ = self.frames(ws, 10)
            self.assertGreaterEqual(len(frames), 10)
            fid, flags, img = frames[-1]
            self.assertEqual(img.shape, (192, 256))
            t = img / 64 - 273.15
            self.assertTrue(-30 < t.min() < t.max() < 120)
            ids = [f[0] for f in frames]
            self.assertEqual(ids, sorted(ids))
        finally:
            ws.close()

    def test_each_frame_is_compressed_once_for_both_viewers(self):
        """IRCAM (IRW1) and the browser get the same datagrams from one encode, and the browser's
        picture is bit-exact with what the camera delivered."""
        seen, encodes = {}, []
        orig = P.packetize

        def counting(rows, frame_id, ts_us, codec=P.CODEC_ZLIB, flags=0):
            encodes.append(frame_id)
            seen[frame_id] = rows.copy()
            return orig(rows, frame_id, ts_us, codec, flags)
        P.packetize = counting
        src = NetworkSource(f"127.0.0.1:{self.br.ctrl_port}")
        src.open(lambda *a: None)
        ws = WsClient(self.port)
        try:
            got = []
            for _ in range(4):
                frames, _ = self.frames(ws, 5)
                got += frames
                for _ in range(5):
                    src.read()
            self.assertGreaterEqual(len(got), 15)
            dup = [f for f in set(encodes) if encodes.count(f) > 1]
            self.assertEqual(dup, [], "a frame was compressed more than once")
            checked = [(fid, img) for fid, _flags, img in got if fid in seen]   # the first one may predate the hook
            self.assertGreaterEqual(len(checked), 10)
            for fid, img in checked:
                np.testing.assert_array_equal(img, seen[fid])
        finally:
            P.packetize = orig
            ws.close()
            src.close()

    def test_browser_and_ircam_viewer_at_the_same_time(self):
        """The add-on must not take the IRW1 viewer slot away from ircam-linux / Android."""
        src = NetworkSource(f"127.0.0.1:{self.br.ctrl_port}")
        src.open(lambda *a: None)
        ws = WsClient(self.port)
        try:
            for _ in range(3):
                frames, _ = self.frames(ws, 5)
                self.assertGreaterEqual(len(frames), 5)
                got = [f for f in (src.read() for _ in range(5)) if f]
                self.assertGreaterEqual(len(got), 4)
            self.assertEqual(src.link_stats()["loss"], 0.0)
            self.assertEqual(self.br.status()["viewer"], "127.0.0.1")
        finally:
            ws.close()
            src.close()

    def test_range_switch_and_status(self):
        ws = WsClient(self.port)
        try:
            status = None
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and not (status and status.get("bridge")):
                _, status = self.frames(ws, 1)
            self.assertTrue(status["bridge"] and status["camera_ok"] and status["can_range"])
            self.assertEqual(status["web_viewers"], 1)
            self.assertTrue(0 <= status["cpu_pct"] <= 100)
            for high in (True, False):
                ws.send_text(json.dumps({"cmd": "set_range", "high": high}))
                deadline = time.monotonic() + 5
                flags = None
                while time.monotonic() < deadline:
                    frames, _ = self.frames(ws, 1)
                    if frames and bool(frames[-1][1] & P.FLAG_HIGH_RANGE) == high:
                        flags = frames[-1][1]
                        break
                self.assertIsNotNone(flags, f"frames never showed high={high}")
                self.assertEqual(self.br.high, high)
        finally:
            ws.close()

    def test_tap_idle_without_browsers(self):
        ws = WsClient(self.port)
        self.frames(ws, 2)
        ws.close()
        deadline = time.monotonic() + 5
        while self.br.tap_conns and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(self.br.tap_conns, "the tap should close when the last browser leaves")


class SetupPageToggle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ircam-netd-")
        cfg = ircam_netd.load_config(os.path.join(self.tmp, "netd.json"))
        self.pin = cfg["pin"]
        self.svc = ircam_netd.FakeWebService()
        mgr = ircam_netd.Manager(ircam_netd.FakeNM(), cfg, os.path.join(self.tmp, "netd.json"),
                                 os.path.join(self.tmp, "status.json"), web=self.svc)
        self.web = ircam_netd.Web(("127.0.0.1", 0), mgr, b"page", fake=False)
        self.port = self.web.server_address[1]
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.shutdown()

    def call(self, path, body=None, pin=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "X-PIN": pin or ""})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_off_by_default_and_toggle_without_pin(self):
        code, st = self.call("/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(st["web"], {"enabled": False, "active": False, "port": 8080})
        self.assertEqual(self.call("/api/name", {"name": "x"})[0], 403)    # other settings keep the PIN
        self.assertEqual(self.call("/api/web", {"enable": "yes"})[0], 400)
        self.assertFalse(self.svc.enabled)
        code, j = self.call("/api/web", {"enable": True})
        self.assertEqual((code, j["web"]["enabled"], j["web"]["active"]), (200, True, True))
        self.assertTrue(self.call("/api/status")[1]["web"]["active"])
        code, j = self.call("/api/web", {"enable": False})
        self.assertEqual((code, j["web"]["enabled"], j["web"]["active"]), (200, False, False))


if __name__ == "__main__":
    unittest.main()
