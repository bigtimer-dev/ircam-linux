"""Wireless bridge tests (no camera, no display): python3 -m unittest discover -s tests"""
import os
import random
import socket
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))
os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="ircam-test-"))

from ircam import cameras as C  # noqa: E402
from ircam.net import protocol as P  # noqa: E402
from ircam.sources import DemoSource, NetworkSource, make_source  # noqa: E402
import ircam_bridge  # noqa: E402


def scene(seed=0, rows=192):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:rows, 0:256]
    t = 25 + 10 * np.sin(xx / 30) + 60 * np.exp(-((xx - 120) ** 2 + (yy - 90) ** 2) / 400)
    return np.clip((t + 273.15) * 64 + rng.normal(0, 4, t.shape), 0, 65535).astype(np.uint16)


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Codec(unittest.TestCase):
    def test_bands_round_trip_bit_exact(self):
        rng = np.random.default_rng(1)
        for rows in (scene(), rng.integers(0, 65536, (192, 256), dtype=np.uint16),
                     np.zeros((192, 256), np.uint16), np.full((192, 256), 65535, np.uint16)):
            for codec in P.CODECS:
                for n in (1, 2, 8):
                    band = rows[:n]
                    out = P.decode_band(P.encode_band(band, codec), codec, n)
                    np.testing.assert_array_equal(out, band)

    def test_fast_encoder_is_byte_identical_to_the_original(self):
        """The 16-bit encoder must produce exactly the bytes of the original int32 version, so
        every existing viewer keeps decoding the stream unchanged."""
        def original(rows):
            d = rows.astype(np.int32)
            d[:, 1:] -= rows[:, :-1]
            s = d.astype(np.int16).astype(np.int32)
            z = ((s << 1) ^ (s >> 31)).astype(">u2")
            return zlib.compress(z.view(np.uint8).reshape(-1, 2).T.tobytes(), 1)
        rng = np.random.default_rng(3)
        frames = [scene(), scene(5), rng.integers(0, 65536, (192, 256), dtype=np.uint16),
                  np.zeros((192, 256), np.uint16), np.full((192, 256), 65535, np.uint16),
                  np.tile(np.array([0, 65535], np.uint16), (192, 128))]          # max-size deltas
        for rows in frames:
            for n in (1, 8, 192):
                self.assertEqual(P.encode_band(rows[:n], P.CODEC_ZLIB), original(rows[:n]))

        def original_packetize(rows, fid, ts, codec, flags=0):     # band by band, as before
            out, n, r = [], rows.shape[0], 0
            while r < n:
                cnt, c = min(P.BAND_ROWS[codec], n - r), codec
                enc = original if codec == P.CODEC_ZLIB else (lambda b: P.encode_band(b, P.CODEC_RAW))
                payload = enc(rows[r:r + cnt])
                while len(payload) > P.PAYLOAD and cnt > 1:
                    cnt //= 2
                    payload = enc(rows[r:r + cnt])
                if len(payload) > P.PAYLOAD:
                    c, payload = P.CODEC_RAW, P.encode_band(rows[r:r + cnt], P.CODEC_RAW)
                out.append(P.HEADER.pack(P.MAGIC, P.VERSION, c, flags, fid, ts, r, cnt) + payload)
                r += cnt
            return out
        for rows in frames:
            for codec in P.CODECS:
                self.assertEqual(P.packetize(rows, 9, 77, codec, 1), original_packetize(rows, 9, 77, codec, 1))

    def test_packets_fit_and_cover_the_frame(self):
        rng = np.random.default_rng(2)
        for rows in (scene(), rng.integers(0, 65536, (192, 256), dtype=np.uint16)):  # noise: worst case
            for codec in P.CODECS:
                dgs = P.packetize(rows, 7, 123, codec)
                self.assertTrue(all(len(d) <= P.MAX_DATAGRAM for d in dgs))
                covered = []
                for d in dgs:
                    _, _, fid, ts, r0, cnt, _ = P.parse(d)
                    self.assertEqual((fid, ts), (7, 123))
                    covered += range(r0, r0 + cnt)
                self.assertEqual(covered, list(range(192)))

    def test_compression_ratio_on_a_thermal_scene(self):
        size = sum(map(len, P.packetize(scene(), 0, 0, P.CODEC_ZLIB)))
        self.assertLess(size, 98304 / 2)

    def test_bad_datagrams_are_rejected(self):
        a = P.Assembler(192)
        for junk in (b"", b"hello", b"IRW1" + bytes(40), os.urandom(300)):
            self.assertEqual(a.feed(junk), [])
        good = P.packetize(scene(), 1, 0)[0]
        self.assertEqual(a.feed(good[:-5]), [])          # truncated payload
        self.assertGreaterEqual(a.bad, 4)


class Assembly(unittest.TestCase):
    def frames(self, n):
        return [scene(i) for i in range(n)]

    def test_in_order(self):
        a = P.Assembler(192)
        for i, f in enumerate(self.frames(3)):
            outs = [o for d in P.packetize(f, i, i) for o in a.feed(d)]
            self.assertEqual(len(outs), 1)
            np.testing.assert_array_equal(outs[0][0], f)
        self.assertEqual(a.loss(), 0.0)

    def test_reordered_and_duplicated(self):
        a = P.Assembler(192)
        f = scene(5)
        dgs = P.packetize(f, 1, 0)
        dgs = dgs + dgs[:3]
        random.Random(3).shuffle(dgs)
        outs = [o for d in dgs for o in a.feed(d)]
        self.assertEqual(len(outs), 1)
        np.testing.assert_array_equal(outs[0][0], f)

    def test_lost_band_is_filled_from_previous_frame(self):
        a = P.Assembler(192)
        f0, f1, f2 = self.frames(3)
        for d in P.packetize(f0, 0, 0):
            a.feed(d)
        d1 = P.packetize(f1, 1, 0)
        _, _, _, _, r0, cnt, _ = P.parse(d1[4])
        for d in d1[:4] + d1[5:]:                       # band 4 lost
            self.assertEqual(a.feed(d), [])
        outs = a.feed(P.packetize(f2, 2, 0)[0])         # next frame starts -> partial published
        self.assertEqual(len(outs), 1)
        got = outs[0][0]
        np.testing.assert_array_equal(got[r0:r0 + cnt], f0[r0:r0 + cnt])
        mask = np.ones(192, bool)
        mask[r0:r0 + cnt] = False
        np.testing.assert_array_equal(got[mask], f1[mask])
        self.assertEqual(a.partial, 1)
        self.assertGreater(a.loss(), 0)

    def test_stale_frame_is_dropped(self):
        a = P.Assembler(192)
        f = self.frames(2)
        a.feed(P.packetize(f[1], 10, 0)[0])
        self.assertEqual(a.feed(P.packetize(f[0], 9, 0)[0]), [])
        self.assertEqual(a.fid, 10)

    def test_frame_id_wraps(self):
        a = P.Assembler(192)
        f = scene()
        for fid in (0xFFFFFFFE, 0xFFFFFFFF, 0, 1):
            outs = [o for d in P.packetize(f, fid, 0) for o in a.feed(d)]
            self.assertEqual([o[1] for o in outs], [fid])


class Misc(unittest.TestCase):
    def test_split_host(self):
        self.assertEqual(P.split_host(""), ("p2pro.local", P.CTRL_PORT))
        self.assertEqual(P.split_host("10.42.0.1"), ("10.42.0.1", P.CTRL_PORT))
        self.assertEqual(P.split_host("pi:5000"), ("pi", 5000))
        self.assertEqual(P.split_host("[fe80::1]:5000"), ("fe80::1", 5000))

    def test_wireless_entry_is_an_add_on(self):
        names = C.source_names()
        self.assertIn(C.WIRELESS_P2PRO, names)
        self.assertIn("InfiRay Or Thermal Master P2Pro", names)
        self.assertIsInstance(make_source(C.WIRELESS_P2PRO, {"wireless_host": "x"}), NetworkSource)
        self.assertEqual(type(make_source("InfiRay Or Thermal Master P2Pro", {})).__name__, "V4L2Source")

    def test_unreachable_bridge_gives_a_clear_error(self):
        from ircam.sources import SourceError
        src = NetworkSource(f"127.0.0.1:{free_port()}")
        with self.assertRaises(SourceError):
            src.open(lambda *a: None)


class Loopback(unittest.TestCase):
    """The real bridge program (demo camera) talking to NetworkSource over localhost."""

    @classmethod
    def setUpClass(cls):
        cls.br = ircam_bridge.Bridge(ircam_bridge.DemoCamera(), "127.0.0.1",
                                     free_port(socket.SOCK_DGRAM), free_port())
        cls.br.start()
        deadline = time.monotonic() + 5
        while not cls.br.cam_ok and time.monotonic() < deadline:
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.br.stop()

    def test_stream_range_and_reconnect(self):
        logs = []
        for _ in range(2):                              # second round: reconnect works
            src = NetworkSource(f"127.0.0.1:{self.br.ctrl_port}")
            src.open(lambda m, k="info": logs.append((k, m)))
            try:
                frames = [f for f in (src.read() for _ in range(15)) if f]
                self.assertGreaterEqual(len(frames), 10)
                raw = np.frombuffer(frames[-1], "<u2").reshape(384, 256)
                t = raw[192:] / 64 - 273.15
                self.assertTrue(-30 < t.min() < t.max() < 120)
                # the frame decodes exactly like a wired / demo frame
                demo = DemoSource()
                demo.open(lambda *a: None)
                self.assertEqual(len(frames[-1]), len(demo.read()))
                self.assertTrue(src.can_switch_range)
                src.set_range(True, lambda *a: None)
                self.assertTrue(src.current_high_range())
                src.set_range(False, lambda *a: None)
                self.assertFalse(src.current_high_range())
                self.assertEqual(src.link_stats()["loss"], 0.0)
            finally:
                src.close()

    def test_bridge_dies_mid_stream_raises(self):
        br = ircam_bridge.Bridge(ircam_bridge.DemoCamera(), "127.0.0.1",
                                 free_port(socket.SOCK_DGRAM), free_port())
        br.start()
        src = NetworkSource(f"127.0.0.1:{br.ctrl_port}")
        src.open(lambda *a: None)
        try:
            self.assertIsNotNone(src.read() or src.read())
            br.stop()
            with self.assertRaises(ConnectionError):
                for _ in range(5):
                    src.read()
        finally:
            src.close()
            br.stop()


if __name__ == "__main__":
    unittest.main()
