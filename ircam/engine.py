"""Processing engine: acquisition thread, image pipeline, measurements, alarms, data
logging, periodic triggers, recording and automatic calibration.

Port of RMH_Application_ThermalViewer.cpp (ImageProcessingSequence, SecondaryProcessing,
alarms/triggers/data logging) with the WinForms parts left to the GTK user interface.
"""
import math
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from . import cameras as C
from . import imageproc as ip
from . import palettes, rawfiles, thermal

UNITS = {"C": (1.0, 0.0, "°C"), "F": (1.8, 32.0, "°F"), "K": (1.0, 273.15, "K")}
MAX_POINTS, MAX_LINES, MAX_ROIS = 10, 5, 10

ALARM_TYPES = ["Above", "Below", "Window"]
EVENTS = ["None", "Start Data Logging", "Stop Data Logging", "Start Video Recording",
          "Stop Video Recording", "Save Snapshot", "Save Frame Temp Data"]


def data_source_names(include_drift=False):
    n = ["Maximum Temp", "Minimum Temp", "Average Temp", "Center Temp"]
    n += [f"Temp Point {i}" for i in range(1, MAX_POINTS + 1)]
    for i in range(1, MAX_LINES + 1):
        n += [f"Line {i} Max Temp", f"Line {i} Min Temp", f"Line {i} Avg Temp"]
    for i in range(1, MAX_ROIS + 1):
        n += [f"ROI {i} Max Temp", f"ROI {i} Min Temp", f"ROI {i} Avg Temp"]
    n += ["Mouse Position Temp"]
    if include_drift:
        n += ["Camera Temp Drift"]
    return n


def timestamp():
    """HHmmssfffddMMyyyy, the original's file-name time stamp."""
    now = datetime.now()
    return now.strftime("%H%M%S") + f"{now.microsecond // 1000:03d}" + now.strftime("%d%m%Y")


@dataclass
class Measure:
    max: float = float("nan")
    min: float = float("nan")
    avg: float = float("nan")
    max_pos: tuple = (0, 0)
    min_pos: tuple = (0, 0)
    profile: np.ndarray = None


@dataclass
class FrameResult:
    rgb: np.ndarray                  # displayed image (possibly ultra-resolution)
    scale: int                       # rgb pixels per thermal pixel
    temps: np.ndarray                # (h, w) temperatures in the selected unit
    width: int
    height: int
    unit: str
    tmax: float
    tmin: float
    tcenter: float
    tavg: float
    max_pos: tuple
    min_pos: tuple
    center_pos: tuple
    bar_lo: float                    # colour bar range
    bar_hi: float
    palette: np.ndarray              # formatted live palette (16384, 3)
    dual_palette: np.ndarray
    hist: np.ndarray = None
    hist_range: tuple = (0.0, 1.0)
    points: list = field(default_factory=list)   # [(x, y, temp)]
    lines: list = field(default_factory=list)    # [Measure]
    rois: list = field(default_factory=list)     # [Measure]
    zoom: Measure = None
    cursor: tuple = None             # (x, y, temp)
    sources: dict = field(default_factory=dict)
    alarms: list = field(default_factory=list)   # triggered flags
    frame_no: int = 0


class Engine:
    def __init__(self, settings, log, notify):
        self.s = settings
        self.log = log              # log(message, kind)
        self.notify = notify        # notify(kind, payload) - called from the engine thread
        self.source = None
        self.thread = None
        self.cmds = queue.Queue()
        self.alive = False
        self.running = True          # live view run/stop
        self.single = False
        self.lock = threading.RLock()
        self.latest = None
        self.last_raw = None
        self.last_td = None
        self.frames = 0
        self.cal_fpa = 0.0
        self.high_range = False
        # measurement objects (thermal image coordinates)
        self.points = []
        self.lines = []
        self.rois = []
        self.dual_rect = None
        self.zoom_rect = None
        self.cursor = None
        # colour bar
        self.bar_mode = "manual" if settings["colorbar_manual"] else "auto"
        self.tag_hi = 1.0
        self.tag_lo = 0.0
        # statistics
        self.peak_max = -1e9
        self.peak_min = 1e9
        # 2D plot ring buffer: list of (t, [values])
        self.plot_t0 = time.monotonic()
        self.plot = []
        # data logging
        self.logging = False
        self.log_file = None
        self.log_sample = 0
        self.log_elapsed_ms = 0
        self.log_next = 0.0
        # recording
        self.recording = False
        self.rec_raw = None
        self.rec_video = None
        self.rec_frames = 0
        self.rec_next = 0.0
        # alarms / triggers
        self.alarm_state = [False] * 5
        self.alarm_event_done = [False] * 5
        self.alarm_event_next = [0.0] * 5
        self.trigger_next = [0.0] * 5
        self.trigger_done = [False] * 5
        self.auto_cal_next = 0.0
        self.drift_next = 0.0
        self.range_busy = False
        self._kernel = None
        self._kernel_sigma = None
        self.alarm_sound_next = 0.0

    # ------------------------------------------------------------------ lifecycle
    @property
    def cam(self):
        return self.source.cam if self.source else None

    def unit(self):
        return UNITS[self.s["temp_unit"]]

    def connect(self, source):
        self.disconnect()
        self.source = source
        source.cam.temp_correction = float(self.s.get("temp_correction", 0.0))
        source.open(self.log)
        self.frames = 0
        self.cal_fpa = source.cam.temp_fpa
        self.peak_max, self.peak_min = -1e9, 1e9
        self.high_range = False
        try:
            hr = source.current_high_range()
        except Exception as e:  # noqa: BLE001 - the stream works without it
            hr = None
            self.log(f"Could not read the camera's temperature range: {e}", "warning")
        if hr is not None:
            self.high_range = hr
            source.cam.range_flag = 2 if hr else 1
            self.log(f"Camera is in the {'HIGH' if hr else 'LOW'} temperature range.")
        self.notify("range", self.high_range)
        self.range_busy = False
        self.alive = True
        self.running = True
        self.plot = []
        self.plot_t0 = time.monotonic()
        now = time.monotonic()
        self.auto_cal_next = now + self.s["auto_cal_period"] * 60
        self.drift_next = now + 1
        self.thread = threading.Thread(target=self._loop, daemon=True, name="ircam-engine")
        self.thread.start()

    def disconnect(self):
        self.alive = False
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)
        self.thread = None
        self.stop_logging()
        self.stop_recording()
        if self.source:
            try:
                self.source.close()
            except Exception:  # noqa: BLE001
                pass
        self.source = None
        self.latest = None

    def submit(self, fn):
        """Run fn() on the engine thread (camera commands must not race the frame reads)."""
        if self.alive:
            self.cmds.put(fn)
        else:
            fn()

    # ------------------------------------------------------------------ camera functions
    def calibrate(self):
        src = self.source
        if not src:
            return

        def run():
            self.notify("busy", "Calibrating…")
            try:
                src.calibrate(self.log)
                self.cal_fpa = src.cam.temp_fpa
            except Exception as e:  # noqa: BLE001
                self.log(f"Calibration failed: {e}", "error")
            self.notify("busy", None)
        self.submit(run)

    def toggle_range(self):
        src = self.source
        if not src:
            return
        if not src.can_switch_range:
            self.log("The selected camera does not support a high temperature range.", "warning")
            return
        self.set_range(not self.high_range)

    def set_range(self, target):
        src = self.source
        if not src or self.range_busy or target == self.high_range:
            return
        if not src.can_switch_range:
            self.log("The selected camera does not support a high temperature range.", "warning")
            self.notify("range", self.high_range)
            return
        self.range_busy = True

        def run():
            self.notify("busy", "Switching temperature range…")
            try:
                if src.set_range(target, self.log):
                    self.high_range = target
                    self.cal_fpa = src.cam.temp_fpa
            except Exception as e:  # noqa: BLE001
                self.log(f"Range switch failed: {e}", "error")
            self.notify("busy", None)
            self.notify("range", self.high_range)
            self.range_busy = False
        self.submit(run)

    def apply_config(self, values):
        """values: dict with temp_correction, ambient, reflected, humidity, emissivity, distance
        (temperatures in °C)."""
        src = self.source
        if not src:
            return

        def run():
            c = src.cam
            for k, v in values.items():
                setattr(c, k, float(v))
            try:
                src.write_config(self.log)
                self.log("Camera configuration set; temperature calculation updated.", "success")
            except Exception as e:  # noqa: BLE001
                self.log(f"Could not set the configuration: {e}", "error")
            self.notify("config", None)
        self.submit(run)

    # ------------------------------------------------------------------ main loop
    def _loop(self):
        err_count = 0
        while self.alive:
            try:
                while True:
                    fn = self.cmds.get_nowait()
                    fn()
            except queue.Empty:
                pass
            try:
                if self.running or self.single or self.latest is None or not self.source.live:
                    raw = self.source.read()
                    if raw is None:
                        err_count += 1
                        if err_count == 5 and self.source.live:
                            self.log("No frames from the camera…", "warning")
                        if err_count > 30 and self.source.live:
                            self.log("The thermal camera stopped delivering frames (disconnected?).", "error")
                            self.notify("disconnected", None)
                            self.alive = False
                            break
                        continue
                    err_count = 0
                    if self.running or self.single or self.latest is None:
                        self.single = False
                        self.last_raw = raw
                        self.frames += 1
                res = self.process(self.last_raw)
                self.latest = res
                self.notify("frame", res)
                self._after_frame(res)
                if not self.running and self.source.live:
                    time.sleep(0.05)
            except OSError as e:
                self.log(f"Camera error: {e}", "error")
                self.notify("disconnected", None)
                self.alive = False
                break
            except Exception:  # noqa: BLE001
                self.log("Processing error:\n" + traceback.format_exc(limit=3), "error")
                time.sleep(0.2)

    # ------------------------------------------------------------------ pipeline
    def process(self, raw):
        s = self.s
        cam = self.cam
        scale_u, off_u, unit = self.unit()
        with self.source.lock:
            td = thermal.decode_frame(cam, raw)
            thermal.read_metadata(cam, td)
            self.last_td = td
            w, ih = cam.width, cam.image_height
            img_raw = td[: w * ih].reshape(ih, w)
            temps = thermal.to_celsius(cam, img_raw) * scale_u + off_u
        conv = lambda r: float(thermal.to_celsius(cam, r)) * scale_u + off_u
        tmax, tmin = conv(cam.tmax_raw), conv(cam.tmin_raw)
        tcenter = conv(cam.center_raw)
        tavg = conv(int(cam.tavg_raw) if cam.pool in (C.POOL_1, C.POOL_3) else cam.tavg_raw)
        # display orientation
        rot = (s["rotation"] // 90) % 4
        if rot:
            temps = np.rot90(temps, -rot)
            img_raw = np.rot90(img_raw, -rot)
        h, w = temps.shape
        if cam.pool == C.POOL_1:
            # pool 1 reports its own max/min positions in sensor coordinates; recompute in view
            pass
        imax = int(np.argmax(temps))
        imin = int(np.argmin(temps))
        max_pos, min_pos = (imax % w, imax // w), (imin % w, imin // w)
        center_pos = (w // 2, h // 2)

        # colour bar range
        mode = self.bar_mode
        if mode == "manual":
            lo, hi = s["colorbar_manual_min"], s["colorbar_manual_max"]
        elif mode == "manual_high":
            lo, hi = tmin, s["colorbar_manual_max"]
        elif mode == "manual_low":
            lo, hi = s["colorbar_manual_min"], tmax
        else:
            lo, hi = tmin, tmax
        if hi <= lo:
            hi = lo + 0.1
        if mode == "auto":
            if cam.tmax_raw != cam.tmin_raw and cam.pool not in (C.POOL_1, C.POOL_3):
                grey = ip.agc_linear(img_raw.astype(np.float64), float(cam.tmax_raw), float(cam.tmin_raw))
            else:
                grey = ip.agc_linear(temps, tmax, tmin)
        else:
            grey = ip.agc_linear(temps, hi, lo)
        if s["sharpening"] or s["focus_assist"]:
            if self._kernel is None or self._kernel_sigma != s["sharpen_sigma"]:
                self._kernel_sigma = s["sharpen_sigma"]
                self._kernel = ip.unsharp_kernel(max(0.2, min(2.0, s["sharpen_sigma"])))
            grey = ip.unsharp_sharpen(grey, self._kernel, s["sharpen_strength"], s["focus_assist"])

        # palettes inside the colour bar arrows
        main = palettes.get(s["palette"])
        back = palettes.get(s["back_palette"])
        dual = palettes.get(s["dual_palette"])
        t_lo, t_hi = (self.tag_lo, self.tag_hi) if s["palette_range_live"] else (0.0, 1.0)
        pal = ip.format_palette(main, s["invert_palette"], back, s["invert_back"],
                                t_lo * ip.RES14, t_hi * ip.RES14, s["adapt_full_palette"])
        d_lo, d_hi = (self.tag_lo, self.tag_hi) if s["palette_range_dual"] else (0.0, 1.0)
        dpal = ip.format_palette(dual, s["invert_dual"], back, s["invert_back"],
                                 d_lo * ip.RES14, d_hi * ip.RES14, s["adapt_full_palette"])
        factor = 1
        g = grey
        if s["ultra_resolution"]:
            factor = 2
            g = ip.bilinear(grey, factor)
        if s["dual_palette_box"] and self.dual_rect:
            x, y, rw, rh = self.dual_rect
            rgb = ip.apply_dual_palette(g, pal, dpal, (x * factor, y * factor, rw * factor, rh * factor))
        else:
            rgb = ip.apply_palette(g, pal)

        res = FrameResult(rgb=rgb, scale=factor, temps=temps, width=w, height=h, unit=unit,
                          tmax=tmax, tmin=tmin, tcenter=tcenter, tavg=tavg, max_pos=max_pos,
                          min_pos=min_pos, center_pos=center_pos, bar_lo=lo, bar_hi=hi,
                          palette=pal, dual_palette=dpal, frame_no=self.frames)
        self._measure(res)
        if s["histogram"]:
            self._histogram(res)
        return res

    def _clip_pt(self, x, y, w, h):
        return int(min(max(0, round(x)), w - 1)), int(min(max(0, round(y)), h - 1))

    def _area(self, temps, rect):
        h, w = temps.shape
        x, y, rw, rh = rect
        x0, y0 = self._clip_pt(x, y, w, h)
        x1, y1 = self._clip_pt(x + rw, y + rh, w, h)
        a = temps[y0:max(y1, y0 + 1), x0:max(x1, x0 + 1)]
        m = Measure()
        if a.size:
            i, j = int(np.argmax(a)), int(np.argmin(a))
            m.max, m.min, m.avg = float(a.flat[i]), float(a.flat[j]), float(a.mean())
            m.max_pos = (x0 + i % a.shape[1], y0 + i // a.shape[1])
            m.min_pos = (x0 + j % a.shape[1], y0 + j // a.shape[1])
            m.profile = a
        return m

    def _measure(self, r):
        temps = r.temps
        h, w = temps.shape
        with self.lock:
            points, lines, rois = list(self.points), list(self.lines), list(self.rois)
            zoom, cursor = self.zoom_rect, self.cursor
        for (x, y) in points:
            px, py = self._clip_pt(x, y, w, h)
            r.points.append((x, y, float(temps[py, px])))
        for (x0, y0, x1, y1) in lines:
            xs, ys = ip.line_pixels(*self._clip_pt(x0, y0, w, h), *self._clip_pt(x1, y1, w, h))
            prof = temps[ys, xs]
            m = Measure(float(prof.max()), float(prof.min()), float(prof.mean()))
            i, j = int(np.argmax(prof)), int(np.argmin(prof))
            m.max_pos, m.min_pos = (int(xs[i]), int(ys[i])), (int(xs[j]), int(ys[j]))
            m.profile = prof
            r.lines.append(m)
        for rect in rois:
            r.rois.append(self._area(temps, rect))
        if zoom:
            r.zoom = self._area(temps, zoom)
        if cursor:
            px, py = self._clip_pt(cursor[0], cursor[1], w, h)
            r.cursor = (px, py, float(temps[py, px]))
        src = {"Maximum Temp": r.tmax, "Minimum Temp": r.tmin, "Average Temp": r.tavg,
               "Center Temp": r.tcenter}
        nan = float("nan")
        for i in range(MAX_POINTS):
            src[f"Temp Point {i + 1}"] = r.points[i][2] if i < len(r.points) else nan
        for i in range(MAX_LINES):
            m = r.lines[i] if i < len(r.lines) else Measure()
            src[f"Line {i + 1} Max Temp"], src[f"Line {i + 1} Min Temp"], src[f"Line {i + 1} Avg Temp"] = m.max, m.min, m.avg
        for i in range(MAX_ROIS):
            m = r.rois[i] if i < len(r.rois) else Measure()
            src[f"ROI {i + 1} Max Temp"], src[f"ROI {i + 1} Min Temp"], src[f"ROI {i + 1} Avg Temp"] = m.max, m.min, m.avg
        src["Mouse Position Temp"] = r.cursor[2] if r.cursor else nan
        src["Camera Temp Drift"] = self.drift() * self.unit()[0]
        r.sources = src

    def _histogram(self, r):
        s = self.s
        src = s["histogram_source"]
        vals = r.temps
        if src == "zoom" and r.zoom and r.zoom.profile is not None:
            vals = r.zoom.profile
        elif src.startswith("roi "):
            i = int(src[4:]) - 1
            if i < len(r.rois) and r.rois[i].profile is not None:
                vals = r.rois[i].profile
        elif src.startswith("line "):
            i = int(src[5:]) - 1
            if i < len(r.lines):
                vals = r.lines[i].profile
        r.hist_range = (r.bar_lo, r.bar_hi)
        r.hist = ip.histogram(vals, int(s["histogram_bins"]), r.bar_lo, r.bar_hi)

    def drift(self):
        cam = self.cam
        return (self.cal_fpa - cam.temp_fpa) if cam else 0.0

    # ------------------------------------------------------------------ per-frame side work
    def _after_frame(self, r):
        s = self.s
        now = time.monotonic()
        # statistics
        self.peak_max = max(self.peak_max, r.tmax)
        self.peak_min = min(self.peak_min, r.tmin)
        # 2D plot
        sets = s["plot_sets"]
        vals = [r.sources.get(ps["source"], float("nan")) if ps["enabled"] else None for ps in sets]
        self.plot.append((now - self.plot_t0, vals))
        horizon = max(5, s["plot_seconds"])
        while self.plot and self.plot[0][0] < (now - self.plot_t0) - horizon:
            self.plot.pop(0)
        # alarms
        r.alarms = self._alarms(r, now)
        # data logging
        if self.logging and now >= self.log_next:
            self._log_row(r)
            self.log_next += s["log_interval"]
            if self.log_next < now:
                self.log_next = now + s["log_interval"]
        # recording
        if self.recording and now >= self.rec_next:
            self.rec_next += 1.0 / max(0.1, s["record_fps"])
            if self.rec_next < now:
                self.rec_next = now + 1.0 / max(0.1, s["record_fps"])
            self._record_frame(r)
        # periodic triggers
        if s["triggers_running"]:
            for i, t in enumerate(s["triggers"]):
                if not t["enabled"] or t["event"] == "None" or self.trigger_done[i]:
                    continue
                if self.trigger_next[i] == 0.0:
                    self.trigger_next[i] = now + t["period"]
                elif now >= self.trigger_next[i]:
                    self.trigger_next[i] = now + max(0.1, t["period"])
                    self.notify("event", t["event"])
                    if t["once"]:
                        self.trigger_done[i] = True
        # automatic calibration
        if self.source and self.source.live:
            if s["auto_cal"] and now >= self.auto_cal_next:
                self.auto_cal_next = now + max(0.1, s["auto_cal_period"]) * 60
                self.calibrate()
            if s["drift_cal"] and now >= self.drift_next:
                self.drift_next = now + 1.0
                if abs(self.drift() * self.unit()[0]) >= s["drift_cal_value"]:
                    self.calibrate()

    def reset_triggers(self):
        self.trigger_next = [0.0] * 5
        self.trigger_done = [False] * 5

    def _alarms(self, r, now):
        s = self.s
        out = []
        for i, a in enumerate(s["alarms"]):
            trig = False
            if a["enabled"]:
                v = r.sources.get(a["source"], float("nan"))
                if not math.isnan(v):
                    if a["type"] == "Above":
                        trig = v >= a["high"]
                    elif a["type"] == "Below":
                        trig = v <= a["low"]
                    else:
                        trig = v >= a["high"] or v <= a["low"]
            if trig and not self.alarm_state[i]:
                self.log(f"Temperature alarm {i + 1} triggered ({a['source']}).", "error")
            self.alarm_state[i] = trig
            if trig and s["alarm_events"] and a["action"] != "None" and not self.alarm_event_done[i]:
                if now >= self.alarm_event_next[i]:
                    self.alarm_event_next[i] = now + max(0.1, s["alarm_event_interval"])
                    self.notify("event", a["action"])
                    self.alarm_event_done[i] = True
            out.append(trig)
        if any(out) and s["alarm_sound"] and now >= self.alarm_sound_next:
            self.alarm_sound_next = now + 1.0
            self.notify("beep", None)
        return out

    def reset_alarm_event(self, i):
        self.alarm_event_done[i] = False
        self.alarm_event_next[i] = 0.0

    # ------------------------------------------------------------------ data logging
    def start_logging(self):
        if self.logging:
            self.log("Data logging is already running!", "warning")
            return
        s = self.s
        sets = [ps for ps in s["plot_sets"] if ps["enabled"]]
        if not sets:
            self.log("Enable at least one 2D plot data set to log.", "warning")
            return
        d = s["log_delimiter"]
        path = Path(s["log_dir"]) / f"DataLogSession_{timestamp()}.txt"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.log_file = open(path, "w", encoding="utf-8")
        except OSError as e:
            self.log(f"Cannot create the log file: {e}", "error")
            return
        self.log_file.write(d.join(["Sample", "Time(ms)"] + [ps["source"] for ps in sets]) + "\n")
        self.log_file.flush()
        self.log_sets = [ps["source"] for ps in sets]
        self.log_sample = 0
        self.log_elapsed_ms = 0
        self.log_duration_ms = int((s["log_hours"] * 3600 + s["log_minutes"] * 60 + s["log_seconds"]) * 1000)
        self.log_next = time.monotonic()
        self.log_path = path
        self.logging = True
        self.log(f"Data logging has started: {path}", "success")
        self.notify("logging", True)

    def _log_row(self, r):
        s = self.s
        d = s["log_delimiter"]
        self.log_sample += 1
        self.log_elapsed_ms += int(round(s["log_interval"] * 1000))
        vals = [r.sources.get(n, float("nan")) for n in self.log_sets]
        self.log_file.write(d.join([str(self.log_sample), str(self.log_elapsed_ms)] + [f"{v:.5f}" for v in vals]) + "\n")
        self.log_file.flush()
        if self.log_duration_ms and self.log_elapsed_ms >= self.log_duration_ms:
            self.stop_logging()

    def stop_logging(self):
        if not self.logging:
            return
        self.logging = False
        try:
            self.log_file.close()
        except OSError:
            pass
        self.log(f"Data logging session has stopped ({self.log_sample} samples).", "warning")
        self.notify("logging", False)

    # ------------------------------------------------------------------ files
    def save_frame_csv(self):
        r = self.latest
        if r is None:
            return None
        s = self.s
        d = s["frame_csv_delimiter"]
        path = Path(s["snapshot_dir"]) / f"FrameTempData_{timestamp()}.txt"
        now = datetime.now()
        stamp = now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}" + now.strftime(" %d-%m-%Y")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"Time And Data For Captured Data: {stamp}\n\n")
                for row in r.temps:
                    f.write(d.join(f"{v:.5f}" for v in row) + "\n")
        except OSError as e:
            self.log(f"Cannot save the frame data: {e}", "error")
            return None
        self.log(f"Full frame temperature data saved: {path}", "success")
        return path

    def raw_snapshot_bytes(self):
        """(rgb bytes, width, height) in the RAW snapshot layout, or None."""
        src, raw = self.source, self.last_raw
        if not src or raw is None:
            return None
        cam = src.cam
        meta = src.snapshot_meta()
        if cam.pool == C.POOL_5:
            buf, h = rawfiles.build_pool5_frame(raw, cam.width, cam.image_height, meta)
            return buf, cam.width, h
        if cam.pool == C.POOL_3 and self.last_td is not None:
            raw = thermal.thermal_to_yuy2(self.last_td, cam.width, cam.height)
        return rawfiles.build_raw_frame(raw, cam.width, cam.height, meta), cam.width, cam.height

    def save_raw_snapshot(self):
        d = self.raw_snapshot_bytes()
        if d is None:
            return None
        buf, w, h = d
        path = Path(self.s["snapshot_dir"]) / f"SnapShotRAW_{timestamp()}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            rawfiles.save_png(path, buf, w, h)
        except Exception as e:  # noqa: BLE001
            self.log(f"Cannot save the RAW snapshot: {e}", "error")
            return None
        return path

    # ------------------------------------------------------------------ recording
    def start_recording(self):
        if self.recording or not self.latest:
            return
        s = self.s
        out = Path(s["video_dir"])
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.log(f"Cannot create {out}: {e}", "error")
            return
        stamp = timestamp()
        fps = float(s["record_fps"])
        r = self.latest
        try:
            self.rec_video = rawfiles.VideoWriter(out / f"Recording_{stamp}", r.rgb.shape[1], r.rgb.shape[0], fps)
            if s["record_raw"] and self.source.live:
                d = self.raw_snapshot_bytes()
                _, w, h = d
                self.rec_raw = rawfiles.AviWriter(out / f"RAWRecording_{stamp}.avi", w, h, fps)
        except OSError as e:
            self.log(f"Cannot start recording: {e}", "error")
            return
        self.rec_frames = 0
        self.rec_next = time.monotonic()
        self.recording = True
        self.log(f"Recording started ({out}).", "success")
        self.notify("recording", True)

    def _record_frame(self, r):
        try:
            if self.rec_video:
                self.rec_video.write(r.rgb)
            if self.rec_raw:
                d = self.raw_snapshot_bytes()
                if d:
                    self.rec_raw.write(d[0])
            self.rec_frames += 1
        except Exception as e:  # noqa: BLE001
            self.log(f"Recording error: {e}", "error")
            self.stop_recording()

    def stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        paths = []
        for wr in (self.rec_video, self.rec_raw):
            if wr is not None:
                try:
                    wr.close()
                    paths.append(getattr(wr, "path", None) or wr.f.name)
                except Exception as e:  # noqa: BLE001
                    self.log(f"Error closing the video file: {e}", "error")
        self.rec_video = self.rec_raw = None
        self.log(f"Recording stopped, {self.rec_frames} frames: " + ", ".join(str(p) for p in paths), "success")
        self.notify("recording", False)
