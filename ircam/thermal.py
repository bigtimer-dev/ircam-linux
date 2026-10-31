"""Raw frame decoding and temperature maths.

A numpy port of RMH_ThermalCameraSupport_Library.cpp (frame conversion, metadata reading,
calibration parameters, atmospheric model and the look-up table / per-pixel formulas).
The flat-array indexing of the original is kept on purpose so results match it.
"""
import math
import struct
from dataclasses import dataclass, field

import numpy as np

from . import cameras as C

RES14 = 16383


def u16x2_to_float(hi, lo):
    return struct.unpack("<f", struct.pack("<I", ((int(hi) & 0xFFFF) << 16) | (int(lo) & 0xFFFF)))[0]


@dataclass
class CameraState:
    """Mirror of ThermalCameraDevice::IRCameraDeviceFormat (the parts that matter here)."""
    pool: int = 0
    width: int = 0          # FrameWidth
    height: int = 0         # FrameHeight (including metadata rows)
    meta_rows: int = 0      # FrameMetadataSize
    width_offset: int = 0   # FrameWidthPixelOffset
    height_offset: int = 0  # FrameHeightPixelOffset
    range_flag: int = 1     # 1 = low range, 2 = high range
    # pool 1/3 constants
    fpa_off: float = 0.0
    fpa_div: float = 0.0
    cal0_offset: float = 0.0
    cal0_fpamul: float = 0.0
    md1: int = 0
    md2: int = 0
    md3: int = 0
    # per frame metadata
    tmax_x: int = 0
    tmax_y: int = 0
    tmax_raw: int = 0
    tmin_x: int = 0
    tmin_y: int = 0
    tmin_raw: int = 0
    tavg_raw: float = 0.0
    center_raw: int = 0
    fpa_raw: int = 0
    shutter_raw: int = 0
    core_raw: int = 0
    temp_fpa: float = 0.0
    temp_shutter: float = 0.0
    temp_core: float = 0.0
    cal: list = field(default_factory=lambda: [0.0] * 6)
    # configuration
    temp_correction: float = C.DEFAULT_TEMP_CORRECTION
    reflected: float = C.DEFAULT_REFLECTED
    ambient: float = C.DEFAULT_AMBIENT
    humidity: float = C.DEFAULT_HUMIDITY
    emissivity: float = C.DEFAULT_EMISSIVITY
    distance: float = C.DEFAULT_DISTANCE
    # derived
    corr_factor: float = 1.0
    corr_offset: float = 0.0
    lut: np.ndarray = field(default_factory=lambda: np.zeros(16384))
    nuc_map: np.ndarray = None  # pool 3 non-uniformity map

    @property
    def image_height(self):
        return self.height - self.meta_rows

    def frame_offsets_for_pool(self):
        if self.pool == C.POOL_2:
            self.width_offset, self.height_offset = 0, C.POOL2_HEIGHT_OFFSET
        elif self.pool in (C.POOL_4, C.POOL_5):
            self.width_offset, self.height_offset = 0, C.POOL4_HEIGHT_OFFSET
            if self.pool == C.POOL_5:
                self.width_offset = C.POOL5_WIDTH_OFFSET
        elif self.pool == C.POOL_6:
            self.width_offset, self.height_offset = C.POOL6_WIDTH_OFFSET, 0
        else:
            self.width_offset = self.height_offset = 0

    def init_constants(self):
        """RMH_IRThermalCamera_InitIRCameraConstants."""
        w, h, m = self.width, self.height, self.meta_rows
        hm = h - m
        if self.pool in (C.POOL_1, C.POOL_3):
            self.cal0_offset, self.cal0_fpamul = 390.0, 7.05
            md1 = w * hm
            md2 = md1
            table = {
                640: (6867, 33.8, 3),
                384: (7800, 36.0, 3),
                256: (8617, 37.682 if self.pool == C.POOL_1 else 13.9, 1),
                240: (7800, 36.0, 1),
            }
            if w in table:
                self.fpa_off, self.fpa_div, rows = table[w]
                md2 = md1 + w * rows
            if self.pool == C.POOL_1 and w == 256:
                self.cal0_offset, self.cal0_fpamul = 170.0, 0.0
            self.md1, self.md2 = md1 - 1, md2 - 1
            if self.pool == C.POOL_3:
                self.md3 = w * hm
        elif self.pool == C.POOL_2:
            self.md1 = w * hm
        else:
            self.md1 = w * hm
            self.md2 = self.md1 + w


def decode_frame(cam: CameraState, raw: bytes):
    """RMH_IRThermalCamera_ConvertYUY2To14BitThermalDataArray.

    Returns the flat uint16 array (width*height values, metadata rows included) and stores
    the frame average in cam.tavg_raw. For pools 2-6 the max/min/centre pixel data is written
    into the first metadata words, exactly like the original.
    """
    w, h = cam.width, cam.height
    n = w * h
    start = (cam.height_offset * w + cam.width_offset) * 2
    buf = np.frombuffer(raw, dtype=np.uint8)
    need = start + n * 2
    if buf.size < need:
        buf = np.concatenate([buf, np.zeros(need - buf.size, np.uint8)])
    td = buf[start:need].view("<u2").astype(np.uint16).copy()
    img_n = w * (h - cam.meta_rows)
    if cam.pool == C.POOL_3 and cam.nuc_map is not None:
        v = td[:img_n].astype(np.float64) - cam.nuc_map[:img_n]
        v = np.mod(np.floor(v), 65536)  # unsigned short cast wrap-around
        td[:img_n] = np.minimum(v, RES14).astype(np.uint16)
    cam.tavg_raw = float(td[:img_n].mean()) if img_n else 0.0
    if cam.pool != C.POOL_1 and img_n > 0:
        img = td[:img_n]
        imax = int(np.argmax(img))
        imin = int(np.argmin(img))
        hm = h - cam.meta_rows
        center = int((hm * 0.5)) * w + int(w * 0.5)
        center_val = int(img[min(center, img_n - 1)])
        td[img_n:img_n + 7] = [imax % w, imax // w, img[imax], imin % w, imin // w, img[imin], center_val][: max(0, min(7, n - img_n))]
    return td


def read_metadata(cam: CameraState, td):
    """RMH_IRThermalCamera_ReadCalFrameMetaData."""
    if cam.meta_rows >= cam.height:
        return
    g = lambda i: int(td[i]) if 0 <= i < td.size else 0
    if cam.pool in (C.POOL_1, C.POOL_3):
        cam.fpa_raw = g(cam.md1 + 2)
        cam.temp_fpa = 20.0 - (cam.fpa_raw - cam.fpa_off) / cam.fpa_div if cam.fpa_div else 0.0
        cam.shutter_raw = g(cam.md2 + 2)
        cam.core_raw = g(cam.md2 + 3)
        if cam.pool == C.POOL_1:
            cam.temp_shutter = cam.shutter_raw / 10.0 - 273.15
            cam.temp_core = cam.core_raw / 10.0 - 273.15
            base = cam.md1 + 3
            idx = [base, base + 1, base + 2, base + 3, base + 4, base + 5, cam.md1 + 13]
        else:
            cam.temp_shutter = ((cam.shutter_raw * 0.625 + 2731.5) / 10.0) - 273.15
            cam.temp_core = ((cam.core_raw * 0.625 + 2731.5) / 10.0) - 273.15
            idx = [cam.md3 + i for i in range(7)]
    else:
        cam.fpa_raw = cam.shutter_raw = cam.core_raw = 1
        cam.temp_fpa = cam.temp_shutter = cam.temp_core = 1.0
        idx = [cam.md1 + i for i in range(7)]
    (cam.tmax_x, cam.tmax_y, cam.tmax_raw, cam.tmin_x, cam.tmin_y, cam.tmin_raw,
     cam.center_raw) = [g(i) for i in idx]


def read_calibration(cam: CameraState, td):
    """RMH_IRThermalCamera_ReadCalibrationParameters."""
    if cam.pool in (C.POOL_1, C.POOL_3):
        g = lambda i: int(td[i]) if 0 <= i < td.size else 0
        b = cam.md2 + 2
        cam.cal = [float(g(cam.md2 + 1))] + [
            u16x2_to_float(g(b + 3 + 2 * k), g(b + 4 + 2 * k)) for k in range(5)]
    else:
        cam.cal = [0.0] * 6


def read_camera_config(cam: CameraState, td):
    """RMH_IRThermalCamera_ReadCameraConfigParameters (pool 1/3 read them from the frame)."""
    if cam.pool in (C.POOL_1, C.POOL_3):
        g = lambda i: int(td[i]) if 0 <= i < td.size else 0
        b = cam.md2 + 2
        f = lambda k: u16x2_to_float(g(b + k), g(b + k + 1))
        cam.reflected, cam.ambient, cam.humidity, cam.emissivity, cam.distance = (
            f(129), f(131), f(133), f(135), f(137))
    else:
        cam.temp_correction = C.DEFAULT_TEMP_CORRECTION
        cam.ambient = C.DEFAULT_AMBIENT
        cam.reflected = C.DEFAULT_REFLECTED
        cam.humidity = C.DEFAULT_HUMIDITY
        cam.emissivity = C.DEFAULT_EMISSIVITY
        cam.distance = C.DEFAULT_DISTANCE


def config_valid(cam: CameraState):
    vals = (cam.reflected, cam.ambient, cam.humidity, cam.emissivity, cam.distance)
    if any(not math.isfinite(v) for v in vals):
        return False
    return (-50 <= cam.ambient <= 200 and -50 <= cam.reflected <= 1000 and 0 <= cam.humidity <= 1
            and 0.01 <= cam.emissivity <= 1 and 0 <= cam.distance <= 1000)


def water_vapour(humidity, ambient):
    h1, h2, h3, h4 = 1.5587, 0.069390, -0.000278160, 0.68455e-6
    return humidity * math.exp(ambient ** 3 * h4 + ambient ** 2 * h3 + ambient * h2 + h1)


def attenuation(omega, distance_m):
    d = float(int(max(0, distance_m)) & 0xFFFF)  # the original takes an unsigned short
    katm, a1, a2, b1, b2 = 1.9, 0.0066, 0.0126, -0.0023, -0.0067
    so = math.sqrt(max(omega, 0.0))
    return (katm * math.exp(-math.sqrt(d) * (a1 + b1 * so))
            + (1 - katm) * math.exp(-math.sqrt(d) * (a2 + b2 * so)))


def generate_lut(cam: CameraState):
    """RMH_IRThermalCamera_GenerateThermoGrapicLookUpTable."""
    omega = water_vapour(cam.humidity, cam.ambient)
    tau = attenuation(omega, cam.distance)
    if cam.pool in (C.POOL_1, C.POOL_3):
        c0, c1, c2, c3, c4, c5 = cam.cal
        if c1 == 0 or cam.emissivity * tau == 0:
            cam.lut = np.zeros(16384)
            return
        num = (1.0 - cam.emissivity) * tau * (cam.reflected + 273.15) ** 4 + (1.0 - tau) * (cam.ambient + 273.15) ** 4
        den = cam.emissivity * tau
        a = c2 / (c1 + c1)
        b = c2 * c2 / (c1 * c1 * 4.0)
        cc = c1 * cam.temp_shutter ** 2 + cam.temp_shutter * c2
        d = c3 * cam.temp_fpa ** 2 + c4 * cam.temp_fpa + c5
        corr = 0.0
        if cam.range_flag == 1:
            corr = max(0.0, cam.cal0_offset - cam.temp_fpa * cam.cal0_fpamul)
        off = c0 - corr
        i = np.arange(16384, dtype=np.float64)
        s = ((i - off) * d + cc) / c1 + b
        n = np.sqrt(np.abs(s))
        wtot = (n - a + 273.15) ** 4
        with np.errstate(invalid="ignore"):
            ttot = np.power((wtot - num) / den, 0.25) - 273.15
        cam.lut = np.nan_to_num(ttot + (cam.distance * 0.85 - 1.125) * (ttot - cam.ambient) / 100.0
                                + cam.temp_correction)
    else:
        sigma = 0.0000000567
        e = cam.emissivity
        refl = (1.0 - e) * tau * sigma * cam.reflected
        amb = (1.0 - tau) * sigma * cam.ambient
        core = (1.0 - cam.temp_shutter / cam.temp_core) * sigma * cam.temp_shutter if cam.temp_core else 0.0
        ets = 1.0 / (e * tau * sigma) if e * tau else 0.0
        cam.corr_offset = -(refl * ets) - (amb * ets) - (core * ets)
        cam.corr_factor = ets * sigma * e


def to_celsius(cam: CameraState, raw):
    """Vectorised RMH_IRThermalCamera_ReadPixelTemperature. raw: int or ndarray."""
    raw = np.asarray(raw)
    if cam.pool in (C.POOL_1, C.POOL_3):
        return cam.lut[raw.astype(np.int64) & 0x3FFF]
    if cam.pool == C.POOL_6:
        return raw / 10.0 - 273.15 + cam.temp_correction
    t = raw * 0.015625 - 273.15
    return t * cam.corr_factor + cam.corr_offset + cam.temp_correction


# --- Pool 3 non-uniformity correction ------------------------------------------------

def nuc_map_from_closed_shutter(cam: CameraState, raw: bytes):
    """RMH_ImageNonUniformityCorrection_* - baseline image while the shutter is closed."""
    w, h = cam.width, cam.height
    buf = np.frombuffer(raw, dtype=np.uint8)
    need = w * h * 2
    if buf.size < need:
        buf = np.concatenate([buf, np.zeros(need - buf.size, np.uint8)])
    base = buf[:need].view("<u2").astype(np.float64)
    img_n = w * (h - cam.meta_rows)
    mean = base[:img_n].mean()
    m = np.zeros(w * h)
    m[:img_n] = base[:img_n] - mean
    cam.nuc_map = m
    return mean


# --- RAW frame bytes <-> 16-bit (pool 3 stores its corrected data) ------------------------

def thermal_to_yuy2(td, width, height):
    return np.asarray(td[: width * height], dtype="<u2").tobytes()
