"""Frame sources: real cameras (per protocol pool), the demo camera and the two analysis
modes (RAW snapshot / RAW recording). Port of the acquisition half of
RMH_ThermalCameraSupport_Library.cpp (Open/Calibrate/SetRange/WriteConfig/InitialEvents).
"""
import math
import struct
import threading
import time

import numpy as np

from . import cameras as C
from . import rawfiles, thermal, v4l2
from .thermal import CameraState


class SourceError(Exception):
    pass


class Source:
    name = ""
    live = True                 # False for snapshot/recording analysis
    can_calibrate = False
    can_switch_range = False
    nominal_fps = 25.0

    def __init__(self):
        self.cam = CameraState()
        self.lock = threading.RLock()
        self.info = []

    # lifecycle ----------------------------------------------------------------------
    def open(self, log):
        raise NotImplementedError

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass

    def read(self):
        """Raw camera frame bytes (or None)."""
        raise NotImplementedError

    def fps(self):
        return 0.0

    # camera functions ----------------------------------------------------------------
    def calibrate(self, log):
        log("This function is not supported for the selected camera.", "warning")
        return False

    def set_range(self, high, log):
        log("The selected camera does not support a high temperature range.", "warning")
        return False

    def current_high_range(self):
        """True/False when the camera can report its active range, None when it can't."""
        return None

    def write_config(self, log):
        """Send the configuration to the camera (pool 1/3 only) and regenerate the LUT."""
        thermal.generate_lut(self.cam)

    # helpers -----------------------------------------------------------------------
    def _geometry(self, width, height, pool):
        cam = self.cam
        cam.pool, cam.width, cam.height = pool, width, height
        cam.meta_rows = C.metadata_rows(width, height)
        cam.frame_offsets_for_pool()
        cam.init_constants()

    def snapshot_meta(self):
        c = self.cam
        return rawfiles.RawMeta(c.pool, c.meta_rows, c.width_offset, c.height_offset, c.temp_correction,
                                c.ambient, c.reflected, c.humidity, c.emissivity, int(c.distance))


def find_v4l2_device(model, preferred="auto"):
    devs = v4l2.list_devices()
    if preferred and preferred != "auto":
        for d in devs:
            if d.path == preferred:
                return d, devs
    names = {n.lower() for n in model.device_names}
    for d in devs:
        if model.usb_ids and (d.vid, d.pid) in model.usb_ids:
            return d, devs
    for d in devs:
        card = d.card.lower()
        if card in names or card.split(":")[0].strip() in names:
            return d, devs
    return None, devs


class V4L2Source(Source):
    """Pools 1, 2, 3, 4 and 6 (UVC cameras)."""

    def __init__(self, model, device="auto"):
        super().__init__()
        self.model = model
        self.name = model.name
        self.device_pref = device
        self.dev = None
        self.p2 = None
        self.want_size = None
        self.can_switch_range = model.high_range and model.pool in (C.POOL_1, C.POOL_3)
        self.can_calibrate = model.pool in (C.POOL_1, C.POOL_3)

    def open(self, log):
        m = self.model
        found, devs = find_v4l2_device(m, self.device_pref)
        if not found:
            names = ", ".join(f"'{d.card}'" for d in devs) or "none"
            raise SourceError(f"No matching camera found for '{m.name}'. Video devices present: {names}.")
        log(f"Opening {found.label()}")
        try:
            self.dev = v4l2.V4L2Camera(found.path)
        except OSError as e:
            raise SourceError(f"Cannot open {found.path}: {e.strerror}. Is your user in the 'video' group?")
        self.device = found
        fmts = self.dev.formats()
        pool = m.pool
        try:
            if pool == C.POOL_2:
                self.dev.set_format(*C.POOL2_SIZE, v4l2.YUYV)
            elif pool == C.POOL_4:
                self.dev.set_format(*C.POOL4_SIZE, v4l2.YUYV)
            elif pool == C.POOL_6:
                self.dev.set_format(pixfmt=v4l2.H264)
                self.want_size = C.POOL6_RAW_BYTES
            else:
                yuyv = [s for f, s in fmts if f == v4l2.YUYV]
                if yuyv and yuyv[0]:
                    w, h = max(yuyv[0], key=lambda s: s[0] * s[1])
                    self.dev.set_format(w, h, v4l2.YUYV)
                else:
                    self.dev.set_format(pixfmt=v4l2.YUYV)
        except OSError as e:
            raise SourceError(f"Could not set the video format: {e}")
        self.dev.set_fps(m.fps)
        w, h = self.dev.width, self.dev.height
        if pool == C.POOL_6:
            w, h = C.POOL6_SIZE
        if pool == C.POOL_2 and (w, h) != C.POOL2_SIZE:
            raise SourceError(f"The camera delivers {w}x{h}; expected 256x384 for this model.")
        self._geometry(w, h, pool)
        log(f"Video format {v4l2.fourcc_str(self.dev.pixfmt)} {self.dev.width}x{self.dev.height}, "
            f"image {w}x{self.cam.image_height}, pool {pool}")
        if pool == C.POOL_1:
            self.dev.set_zoom(C.POOL1_RAW_MODE)
        elif pool == C.POOL_3:
            self.dev.set_zoom(C.POOL3_RAW_MODE)
        # default range = low, then calibrate (RMH_IRThermalCamera_OpenIRCameraDevice)
        if pool in (C.POOL_1, C.POOL_3):
            self.dev.set_zoom(C.POOL13_LOW_RANGE)
            self.dev.set_zoom(C.POOL13_NUC)
        if pool == C.POOL_2 and (found.vid, found.pid) == (0x0BDA, 0x5830):
            try:
                from .p2pro import P2ProControl
                self.p2 = P2ProControl()
                self.can_switch_range = True
                log("P2 Pro vendor control channel available (range switching enabled).")
            except Exception as e:  # noqa: BLE001 - optional feature
                log(f"P2 Pro range switching unavailable ({e}). See README for the udev rule.", "warning")
        self.dev.start()
        self._initial_events(log)

    def _initial_events(self, log):
        cam = self.cam
        if cam.pool == C.POOL_1:
            raw = None
            for _ in range(25):
                time.sleep(0.125)
                raw = self.dev.read(0.5, self.want_size)
                if raw is None:
                    continue
                td = thermal.decode_frame(cam, raw)
                thermal.read_metadata(cam, td)
                if cam.fpa_raw and cam.shutter_raw:
                    break
            else:
                raise SourceError("The camera did not deliver calibration metadata.")
            thermal.read_calibration(cam, td)
            corr = cam.temp_correction
            thermal.read_camera_config(cam, td)
            cam.temp_correction = corr
            self._fix_config(log)
            thermal.generate_lut(cam)
        elif cam.pool == C.POOL_3:
            time.sleep(C.POOL3_READY_MS / 1000)
            self.calibrate(log)
            raw = self.dev.read(1.0, self.want_size)
            if raw:
                td = thermal.decode_frame(cam, raw)
                corr = cam.temp_correction
                thermal.read_camera_config(cam, td)
                cam.temp_correction = corr
            self._fix_config(log)
            thermal.generate_lut(cam)
        else:
            corr = cam.temp_correction
            thermal.read_camera_config(cam, None)
            cam.temp_correction = corr
            thermal.generate_lut(cam)

    def _fix_config(self, log):
        if not thermal.config_valid(self.cam):
            log("The camera's stored configuration is invalid; defaults are used.", "warning")
            c = self.cam
            c.ambient, c.reflected, c.humidity = C.DEFAULT_AMBIENT, C.DEFAULT_REFLECTED, C.DEFAULT_HUMIDITY
            c.emissivity, c.distance = C.DEFAULT_EMISSIVITY, C.DEFAULT_DISTANCE

    def read(self):
        return self.dev.read(1.0, self.want_size)

    def fps(self):
        return self.dev.fps() if self.dev else 0.0

    def calibrate(self, log):
        cam = self.cam
        if cam.pool == C.POOL_1:
            with self.lock:
                self.dev.set_zoom(C.POOL13_NUC)
                log("Thermal camera is calibrating...")
                time.sleep(C.SHUTTER_CLOSE_MS / 1000)
                raw = self.dev.read(1.0)
                if raw:
                    td = thermal.decode_frame(cam, raw)
                    thermal.read_metadata(cam, td)
                    thermal.read_calibration(cam, td)
                thermal.generate_lut(cam)
            log("Calibration finished; new temperature look-up table generated.", "success")
            return True
        if cam.pool == C.POOL_3:
            with self.lock:
                self.dev.set_zoom(C.POOL13_NUC)
                log("Thermal camera is calibrating (non-uniformity correction)...")
                time.sleep(C.SHUTTER_CLOSE_MS / 1000)
                raw = self.dev.read(1.0)
                if raw:
                    thermal.nuc_map_from_closed_shutter(cam, raw)
                time.sleep(C.SHUTTER_OPEN_MS / 1000)
                raw = self.dev.read(1.0)
                if raw:
                    td = thermal.decode_frame(cam, raw)
                    thermal.read_metadata(cam, td)
                    thermal.read_calibration(cam, td)
                thermal.generate_lut(cam)
            log("Calibration finished.", "success")
            return True
        return super().calibrate(log)

    def set_range(self, high, log):
        cam = self.cam
        if cam.pool in (C.POOL_1, C.POOL_3) and self.model.high_range:
            self.dev.set_zoom(C.POOL13_HIGH_RANGE if high else C.POOL13_LOW_RANGE)
            time.sleep((C.POOL1_RANGE_SWITCH_MS if cam.pool == C.POOL_1 else C.POOL3_RANGE_SWITCH_MS) / 1000)
            cam.range_flag = 2 if high else 1
            self.calibrate(log)
            return True
        if self.p2 is not None:
            # P2 Pro: high gain = low temperature range
            self.p2.set_high_gain(not high)
            cam.range_flag = 2 if high else 1
            log(f"P2 Pro switched to the {'high' if high else 'low'} temperature range.", "success")
            return True
        return super().set_range(high, log)

    def current_high_range(self):
        if self.p2 is not None:
            with self.lock:
                return not self.p2.high_gain()  # high gain = low range
        return None

    def write_config(self, log):
        cam = self.cam
        if cam.pool in (C.POOL_1, C.POOL_3):
            for reg, val in ((C.REG_REFL_TEMP, cam.reflected), (C.REG_AMB_TEMP, cam.ambient),
                             (C.REG_HUMIDITY, cam.humidity), (C.REG_EMISSIVITY, cam.emissivity),
                             (C.REG_DISTANCE, cam.distance)):
                b = struct.pack("<f", float(val))
                for k in range(4):
                    self.dev.set_zoom((((reg + k) & 0x7F) << 8) | b[k])
        thermal.generate_lut(cam)

    def close(self):
        if self.dev:
            self.dev.close()
            self.dev = None
        if self.p2:
            self.p2.close()
            self.p2 = None


class P3Source(Source):
    can_calibrate = True
    can_switch_range = True

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.name = model.name
        from .p3 import P3Camera
        self.p3 = P3Camera()

    def open(self, log):
        try:
            self.p3.open()
        except OSError as e:
            raise SourceError(f"Could not open the Thermal Master P3: {e}. See README for the udev rule.")
        self._geometry(C.POOL4_SIZE[0], C.POOL4_SIZE[1], C.POOL_5)
        thermal.read_camera_config(self.cam, None)
        thermal.generate_lut(self.cam)
        log("Starting the P3 video stream (a cold camera can take ~10 s)...")
        self.p3.start()

    def read(self):
        return self.p3.read()

    def fps(self):
        return self.p3.fps()

    def calibrate(self, log):
        self.p3.shutter()
        time.sleep((C.SHUTTER_CLOSE_MS + C.SHUTTER_OPEN_MS) / 1000)
        log("Shutter calibration done.", "success")
        return True

    def set_range(self, high, log):
        self.p3.set_high_range(high)
        time.sleep(C.POOL4_RANGE_SWITCH_MS / 1000)
        self.cam.range_flag = 2 if high else 1
        self.calibrate(log)
        return True

    def close(self):
        self.p3.close()


class DemoSource(Source):
    """A simulated P2 Pro (pool 2 frame format) so the app can be tried without a camera."""
    can_calibrate = True
    can_switch_range = True
    name = C.DEMO_CAMERA

    def open(self, log):
        self._geometry(*C.POOL2_SIZE, C.POOL_2)
        thermal.read_camera_config(self.cam, None)
        thermal.generate_lut(self.cam)
        self.t0 = time.monotonic()
        self.last = 0.0
        self._fps_t = []
        h, w = 192, 256
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
        self.yy, self.xx = yy, xx
        rng = np.random.default_rng(1)
        self.fpn = rng.normal(0, 0.05, (h, w))
        self.offset = 0.0
        log("Demo camera started (simulated P2 Pro frames).")

    def read(self):
        now = time.monotonic()
        wait = 1 / 25 - (now - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        self._fps_t = [t for t in self._fps_t + [self.last] if self.last - t < 2.0]
        t = self.last - self.t0
        xx, yy = self.xx, self.yy
        scene = 22.0 + 4.0 * (yy / 192) + 1.5 * np.sin(xx / 25.0)
        cx, cy = 128 + 70 * math.cos(t * 0.4), 96 + 45 * math.sin(t * 0.55)
        scene += 48.0 * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 14.0 ** 2)))
        scene += 30.0 * ((np.abs(xx - 60) < 18) & (np.abs(yy - 140) < 10))  # a warm "component"
        scene -= 14.0 * np.exp(-(((xx - 200) ** 2 + (yy - 50) ** 2) / (2 * 10.0 ** 2)))
        scene += self.fpn + np.random.normal(0, 0.06, scene.shape) + self.offset
        raw16 = np.clip((scene + 273.15) * 64.0, 0, 65535).astype("<u2")
        frame = np.zeros((384, 256), "<u2")
        frame[:192] = (raw16 >> 6).astype("<u2")  # fake "image" half
        frame[192:] = raw16
        return frame.tobytes()

    def fps(self):
        t = self._fps_t
        return (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else 0.0

    def calibrate(self, log):
        time.sleep(0.3)
        self.fpn *= 0.3
        log("Demo camera: simulated shutter calibration done.", "success")
        return True

    def set_range(self, high, log):
        self.cam.range_flag = 2 if high else 1
        log(f"Demo camera: {'high' if high else 'low'} range selected.", "success")
        return True


class SnapshotSource(Source):
    live = False
    nominal_fps = 1.0

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.name = "Snapshot Analysis"

    def open(self, log):
        try:
            rgb, w, h = rawfiles.load_png(self.path)
        except Exception as e:  # noqa: BLE001 - any decoder error
            raise SourceError(f"Cannot read {self.path}: {e}")
        meta = rawfiles.read_id_row(rgb, w, h)
        if meta is None:
            raise SourceError("This PNG is not an IRCAM RAW snapshot (look for files named SnapShotRAW_...).")
        self._load(rgb, w, h, meta)
        log(f"RAW snapshot loaded: {w}x{h}, camera pool {meta.pool}.", "success")

    def _load(self, rgb, w, h, meta):
        cam = self.cam
        cam.pool, cam.width, cam.height = meta.pool, w, h
        cam.meta_rows, cam.width_offset, cam.height_offset = meta.meta_rows, meta.width_offset, meta.height_offset
        cam.temp_correction, cam.ambient, cam.reflected = meta.temp_correction, meta.ambient, meta.reflected
        cam.humidity, cam.emissivity, cam.distance = meta.humidity, meta.emissivity, float(meta.distance)
        cam.init_constants()
        self.raw = rawfiles.unpack_loaded(rgb, w, h, meta)
        td = thermal.decode_frame(cam, self.raw)
        thermal.read_metadata(cam, td)
        thermal.read_calibration(cam, td)
        thermal.generate_lut(cam)

    def read(self):
        time.sleep(0.04)
        return self.raw


class RecordingSource(SnapshotSource):
    def __init__(self, path):
        super().__init__(path)
        self.name = "Recording Analysis"
        self.position = 0
        self.playing = False
        self.speed = 1.0
        self.loop = True
        self._last = time.monotonic()

    def open(self, log):
        try:
            self.avi = rawfiles.AviReader(self.path)
        except (OSError, ValueError) as e:
            raise SourceError(f"Cannot open {self.path}: {e}")
        w, h = self.avi.w, self.avi.h
        rgb = self.avi.read(0)
        meta = rawfiles.read_id_row(rgb, w, h)
        if meta is None:
            raise SourceError("This video is not an IRCAM RAW recording (look for RAWRecording_...).")
        self.meta = meta
        self.nominal_fps = self.avi.fps or 25.0
        self._load(rgb, w, h, meta)
        log(f"RAW recording loaded: {len(self.avi)} frames, {self.nominal_fps:.1f} fps.", "success")

    @property
    def frames(self):
        return len(self.avi)

    def seek(self, i):
        self.position = max(0, min(int(i), self.frames - 1))

    def read(self):
        now = time.monotonic()
        period = 1.0 / (self.nominal_fps * max(self.speed, 0.01))
        wait = period - (now - self._last)
        if wait > 0:
            time.sleep(min(wait, 0.1))
        self._last = time.monotonic()
        if self.playing:
            nxt = self.position + 1
            if nxt >= self.frames:
                nxt = 0 if self.loop else self.frames - 1
                if not self.loop:
                    self.playing = False
            self.position = nxt
        rgb = self.avi.read(self.position)
        return rawfiles.unpack_loaded(rgb, self.cam.width, self.cam.height, self.meta)

    def fps(self):
        return self.nominal_fps * self.speed if self.playing else 0.0

    def close(self):
        self.avi.close()


def make_source(name, settings, path=None):
    if name == C.DEMO_CAMERA:
        return DemoSource()
    if name == C.SNAPSHOT_ANALYSIS:
        return SnapshotSource(path)
    if name == C.RECORDING_ANALYSIS:
        return RecordingSource(path)
    model = C.MODEL_BY_NAME[name]
    if model.pool == C.POOL_5:
        return P3Source(model)
    return V4L2Source(model, settings.get("v4l2_device", "auto"))
