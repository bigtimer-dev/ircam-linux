"""Core tests (no camera, no display needed): python3 -m unittest discover -s tests"""
import ctypes as ct
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_tmp = tempfile.mkdtemp(prefix="ircam-test-")
os.environ["XDG_CONFIG_HOME"] = _tmp

from ircam import cameras as C  # noqa: E402
from ircam import imageproc as ip  # noqa: E402
from ircam import palettes, rawfiles, thermal, v4l2  # noqa: E402
from ircam.config import Settings  # noqa: E402
from ircam.engine import Engine, data_source_names  # noqa: E402
from ircam.sources import DemoSource, RecordingSource, SnapshotSource  # noqa: E402


class StructSizes(unittest.TestCase):
    def test_v4l2_structs_match_kernel_abi(self):
        for name, size in [("Capability", 104), ("Format", 208), ("FmtDesc", 64), ("FrmSizeEnum", 44),
                           ("RequestBuffers", 20), ("Buffer", 88), ("StreamParm", 204), ("Control", 8)]:
            self.assertEqual(ct.sizeof(getattr(v4l2, name)), size, name)


class Thermal(unittest.TestCase):
    def _demo(self):
        d = DemoSource()
        d.open(lambda *a: None)
        return d

    def test_pool2_blackbody_identity(self):
        c = self._demo().cam
        c.emissivity, c.ambient, c.reflected = 1.0, 30.0, 30.0
        thermal.generate_lut(c)
        raw = np.array([int(round((30 + 273.15) * 64))])
        self.assertAlmostEqual(float(thermal.to_celsius(c, raw)[0]), 30.0, delta=0.05)

    def test_pool2_correction_matches_original_formula(self):
        """Same linear model as RMH_IRThermalCamera_GenerateThermoGrapicLookUpTable (pools 2/4/5/6):
        factor = 1/tau, offset = -((1-e)*tau*Tr + (1-tau)*Ta + core) / (e*tau)."""
        c = self._demo().cam
        c.emissivity, c.ambient, c.reflected, c.humidity, c.distance = 0.8, 25.0, 20.0, 0.45, 1.0
        thermal.generate_lut(c)
        tau = thermal.attenuation(thermal.water_vapour(c.humidity, c.ambient), c.distance)
        core = (1 - c.temp_shutter / c.temp_core) * c.temp_shutter if c.temp_core else 0.0
        self.assertAlmostEqual(c.corr_factor, 1 / tau, places=9)
        exp = -((1 - 0.8) * tau * 20.0 + (1 - tau) * 25.0 + core) / (0.8 * tau)
        self.assertAlmostEqual(c.corr_offset, exp, places=6)

    def test_decode_writes_max_min(self):
        d = self._demo()
        td = thermal.decode_frame(d.cam, d.read())
        thermal.read_metadata(d.cam, td)
        img = td[: 256 * 192]
        self.assertEqual(d.cam.tmax_raw, int(img.max()))
        self.assertEqual(d.cam.tmin_raw, int(img.min()))

    def test_metadata_rows(self):
        self.assertEqual(C.metadata_rows(256, 196), 4)
        self.assertEqual(C.metadata_rows(384, 292), 4)


class ImageProc(unittest.TestCase):
    def test_agc_range(self):
        g = ip.agc_linear(np.array([10.0, 20.0, 30.0]), 30.0, 10.0)
        self.assertEqual(int(g[0]), 0)
        self.assertEqual(int(g[-1]), ip.RES14)

    def test_palettes(self):
        self.assertGreaterEqual(len(palettes.names()), 52)
        self.assertEqual(palettes.get("Iron").shape, (16384, 3))
        self.assertIs(palettes.get("does not exist"), palettes.get("Iron"))

    def test_custom_palette_roundtrip(self):
        palettes.save_custom("Test Pal", [(0.0, [0, 0, 0]), (1.0, [255, 0, 0])])
        self.assertIn("Test Pal", palettes.names())
        self.assertEqual(tuple(palettes.get("Test Pal")[-1]), (255, 0, 0))
        palettes.delete_custom("Test Pal")
        self.assertNotIn("Test Pal", palettes.names())

    def test_line_pixels_endpoints(self):
        xs, ys = ip.line_pixels(0, 0, 10, 5)
        self.assertEqual((xs[0], ys[0], xs[-1], ys[-1]), (0, 0, 10, 5))


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ircam-files-"))
        self.s = Settings.load()
        for k in ("snapshot_dir", "video_dir", "log_dir"):
            self.s[k] = str(self.dir)
        self.e = Engine(self.s, lambda *a: None, lambda *a: None)

    def tearDown(self):
        self.e.disconnect()

    def _wait(self, n=5):
        t0 = time.time()
        while self.e.frames < n and time.time() - t0 < 5:
            time.sleep(0.05)

    def test_raw_snapshot_roundtrip(self):
        self.e.connect(DemoSource())
        self._wait()
        r1 = self.e.latest
        p = self.e.save_raw_snapshot()
        self.assertTrue(p.name.startswith("SnapShotRAW_"))
        self.e.disconnect()
        self.e.connect(SnapshotSource(str(p)))
        self._wait(1)
        time.sleep(0.2)
        cam = self.e.cam
        self.assertEqual((cam.pool, cam.width, cam.height), (C.POOL_2, 256, 384))
        self.assertAlmostEqual(cam.emissivity, C.DEFAULT_EMISSIVITY, places=5)
        self.assertTrue(abs(self.e.latest.tmax - r1.tmax) < 5)

    def test_raw_recording_roundtrip(self):
        self.s["record_fps"] = 25.0
        self.e.connect(DemoSource())
        self._wait()
        self.e.start_recording()
        time.sleep(0.6)
        self.e.stop_recording()
        avi = next(self.dir.glob("RAWRecording_*.avi"))
        self.e.disconnect()
        src = RecordingSource(str(avi))
        self.e.connect(src)
        self.assertGreater(src.frames, 5)
        self.assertEqual(self.e.cam.pool, C.POOL_2)

    def test_logging_and_frame_csv(self):
        self.s["log_interval"] = 0.1
        self.s["log_delimiter"] = ";"
        self.e.connect(DemoSource())
        self._wait()
        self.e.start_logging()
        time.sleep(0.5)
        self.e.stop_logging()
        log = next(self.dir.glob("DataLogSession_*.txt")).read_text().splitlines()
        self.assertEqual(log[0], "Sample;Time(ms);Maximum Temp;Minimum Temp;Average Temp")
        self.assertEqual(log[1].split(";")[:2], ["1", "100"])
        p = self.e.save_frame_csv()
        rows = p.read_text().splitlines()
        self.assertTrue(rows[0].startswith("Time And Data For Captured Data: "))
        self.assertEqual(len(rows), 2 + 192)
        self.assertEqual(len(rows[2].split(",")), 256)

    def test_alarm_triggers(self):
        self.s["alarms"][0].update(enabled=True, source="Maximum Temp", type="Above", high=40.0)
        self.s["alarms"][1].update(enabled=True, source="Minimum Temp", type="Below", low=-50.0)
        self.e.connect(DemoSource())
        self._wait()
        time.sleep(0.1)
        self.assertEqual(self.e.latest.alarms[:2], [True, False])

    def test_plot_data_only_while_visible(self):
        self.s["plot_visible"] = False
        self.e.connect(DemoSource())
        self._wait()
        time.sleep(0.2)
        self.assertEqual(self.e.plot, [])
        self.s["plot_visible"] = True
        time.sleep(0.3)
        self.assertGreater(len(self.e.plot), 3)
        self.s["plot_visible"] = False
        time.sleep(0.2)
        self.assertEqual(self.e.plot, [])

    def test_manual_range_selection(self):
        self.e.connect(DemoSource())
        self._wait()
        self.e.set_range(True)
        time.sleep(0.3)
        self.assertTrue(self.e.high_range)
        self.e.toggle_range()
        time.sleep(0.3)
        self.assertFalse(self.e.high_range)


class Sources(unittest.TestCase):
    def test_source_list(self):
        names = C.source_names()
        self.assertEqual(names[:3], [C.SNAPSHOT_ANALYSIS, C.RECORDING_ANALYSIS, C.DEMO_CAMERA])
        self.assertIn("InfiRay Or Thermal Master P2Pro", names)
        self.assertEqual(len(data_source_names()), 4 + 10 + 15 + 30 + 1)


if __name__ == "__main__":
    unittest.main()
