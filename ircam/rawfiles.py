"""RAW snapshot / RAW recording files and processed video output.

RAW snapshot (SnapShotRAW_*.png) uses the original program's layout so files are
interchangeable with IRCAM Thermal Viewer on Windows: an RGB24 PNG of the camera frame size
whose byte stream is the raw camera frame, with an identification row ("IRCAM.RAM" + camera
pool, geometry and the camera configuration) written into the last pixel row
(RMH_AnalysisMode_AddIDAndMetaDataToFrameArray).

RAW recording (RAWRecording_*.avi) stores the same frame layout per video frame as
uncompressed 24-bit RGB in an AVI container.
"""
import shutil
import struct
import subprocess
from dataclasses import dataclass

import numpy as np

from . import cameras as C

ID_STRING = b"IRCAM.RAM"


@dataclass
class RawMeta:
    pool: int
    meta_rows: int
    width_offset: int
    height_offset: int
    temp_correction: float
    ambient: float
    reflected: float
    humidity: float
    emissivity: float
    distance: int


def _f2b(v):
    return struct.pack(">f", float(v))


def add_id_row(buf, width, height, m: RawMeta):
    """buf: bytearray of width*height*3. Writes the ID row into the last pixel row."""
    s = (height - 1) * width * 3
    row = bytearray(ID_STRING)  # bytes 0-8 (the original only checks 0-7)
    row += bytes([m.pool & 0xFF])  # byte 9
    row += struct.pack(">HHH", m.meta_rows & 0xFFFF, m.width_offset & 0xFFFF, m.height_offset & 0xFFFF)
    row += _f2b(m.temp_correction) + _f2b(m.ambient) + _f2b(m.reflected) + _f2b(m.humidity) + _f2b(m.emissivity)
    row += struct.pack(">H", int(m.distance) & 0xFFFF)
    row += b"!"
    buf[s:s + len(row)] = row


def read_id_row(buf, width, height):
    s = (height - 1) * width * 3
    r = bytes(buf[s:s + 39])
    if len(r) < 39 or r[:8] != ID_STRING[:8]:
        return None
    pool = r[9]
    meta, wo, ho = struct.unpack(">HHH", r[10:16])
    tc, amb, refl, hum, emi = struct.unpack(">fffff", r[16:36])
    dist = struct.unpack(">H", r[36:38])[0]
    m = RawMeta(pool, meta, wo, ho, tc, amb, refl, hum, emi, dist)
    if not (1 <= pool <= 6) or meta >= height or wo >= width or ho >= height:
        return None
    return m


def build_raw_frame(raw, width, height, meta: RawMeta):
    """Pack raw camera bytes into the W x H RGB24 layout with the ID row."""
    buf = bytearray(width * height * 3)
    n = min(len(raw), len(buf))
    buf[:n] = raw[:n]
    add_id_row(buf, width, height, meta)
    return buf


def build_pool5_frame(raw, width, image_height, meta: RawMeta):
    """Pool 5 (P3): 16-bit image pixels stored as R=low, G=high, B=0; one extra ID row."""
    h = image_height + 1
    off = (C.POOL4_HEIGHT_OFFSET * width + C.POOL5_WIDTH_OFFSET) * 2
    px = np.frombuffer(raw[off:off + width * image_height * 2].ljust(width * image_height * 2, b"\0"), np.uint8)
    buf = bytearray(width * h * 3)
    a = np.frombuffer(buf, np.uint8).reshape(-1, 3)
    a[: width * image_height, 0] = px[0::2]
    a[: width * image_height, 1] = px[1::2]
    m = RawMeta(5, 1, 0, 0, meta.temp_correction, meta.ambient, meta.reflected, meta.humidity,
                meta.emissivity, meta.distance)
    add_id_row(buf, width, h, m)
    return buf, h


def unpack_loaded(buf, width, height, meta):
    """Turn a loaded RGB buffer back into raw camera bytes (pool 5 needs repacking)."""
    if meta.pool == C.POOL_5:
        a = np.frombuffer(bytes(buf), np.uint8).reshape(-1, 3)
        out = np.empty(a.shape[0] * 2, np.uint8)
        out[0::2] = a[:, 0]
        out[1::2] = a[:, 1]
        return out.tobytes()
    return bytes(buf)


# --- PNG I/O (GdkPixbuf) -----------------------------------------------------------------

def save_png(path, rgb_bytes, width, height):
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib
    pb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(bytes(rgb_bytes)), GdkPixbuf.Colorspace.RGB,
                                         False, 8, width, height, width * 3)
    pb.savev(str(path), "png", [], [])


def load_png(path):
    """Returns (rgb bytes, width, height)."""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    pb = GdkPixbuf.Pixbuf.new_from_file(str(path))
    w, h, n, stride = pb.get_width(), pb.get_height(), pb.get_n_channels(), pb.get_rowstride()
    data = np.frombuffer(pb.get_pixels(), np.uint8)
    rows = np.stack([data[y * stride: y * stride + w * n] for y in range(h)])
    rgb = rows.reshape(h, w, n)[:, :, :3]
    return rgb.tobytes(), w, h


def save_rgb_png(path, rgb):
    """rgb: (h, w, 3) uint8 array."""
    h, w = rgb.shape[:2]
    save_png(path, np.ascontiguousarray(rgb, dtype=np.uint8).tobytes(), w, h)


# --- uncompressed AVI ---------------------------------------------------------------------

class AviWriter:
    def __init__(self, path, width, height, fps):
        self.f = open(path, "wb")
        self.w, self.h, self.fps = width, height, fps
        self.stride = (width * 3 + 3) & ~3
        self.frame_size = self.stride * height
        self.index = []
        self.count = 0
        self._write_header()

    def _write_header(self):
        f = self.f
        f.write(b"RIFF\0\0\0\0AVI ")
        hdrl = bytearray()
        us = int(1_000_000 / max(self.fps, 0.001))
        avih = struct.pack("<IIIIIIIIII4I", us, self.frame_size * int(self.fps + 1), 0, 0x10, 0, 0, 1,
                           self.frame_size, self.w, self.h, 0, 0, 0, 0)
        hdrl += b"avih" + struct.pack("<I", len(avih)) + avih
        strh = struct.pack("<4s4sIHHIIIIIIIIhhhh", b"vids", b"DIB ", 0, 0, 0, 0, 1000,
                           int(round(self.fps * 1000)), 0, 0, self.frame_size, 0xFFFFFFFF, 0, 0, 0,
                           self.w, self.h)
        strf = struct.pack("<IiiHHIIiiII", 40, self.w, self.h, 1, 24, 0, self.frame_size, 0, 0, 0, 0)
        strl = b"strh" + struct.pack("<I", len(strh)) + strh + b"strf" + struct.pack("<I", len(strf)) + strf
        hdrl += b"LIST" + struct.pack("<I", len(strl) + 4) + b"strl" + strl
        f.write(b"LIST" + struct.pack("<I", len(hdrl) + 4) + b"hdrl" + hdrl)
        self._movi_pos = f.tell()
        f.write(b"LIST\0\0\0\0movi")

    def write(self, rgb_rows):
        """rgb_rows: bytes of h*w*3 (top-down RGB)."""
        a = np.frombuffer(bytes(rgb_rows), np.uint8)[: self.w * self.h * 3].reshape(self.h, self.w, 3)
        a = a[::-1, :, ::-1]  # DIB: bottom-up BGR
        out = np.zeros((self.h, self.stride), np.uint8)
        out[:, : self.w * 3] = a.reshape(self.h, -1)
        off = self.f.tell() - (self._movi_pos + 8)
        self.f.write(b"00db" + struct.pack("<I", self.frame_size) + out.tobytes())
        self.index.append(off)
        self.count += 1

    def close(self):
        f = self.f
        end_movi = f.tell()
        f.write(b"idx1" + struct.pack("<I", 16 * len(self.index)))
        for off in self.index:
            f.write(b"00db" + struct.pack("<III", 0x10, off, self.frame_size))
        end = f.tell()
        f.seek(4)
        f.write(struct.pack("<I", end - 8))
        f.seek(self._movi_pos + 4)
        f.write(struct.pack("<I", end_movi - self._movi_pos - 8))
        # total frames in avih and strh length
        f.seek(12 + 8 + 4 + 8 + 16)
        f.write(struct.pack("<I", self.count))
        f.close()


class AviReader:
    """Reads uncompressed 24/32-bit DIB AVI files (frames returned top-down RGB)."""

    def __init__(self, path):
        self.f = open(path, "rb")
        data = self.f.read(12)
        if data[:4] != b"RIFF" or data[8:12] != b"AVI ":
            raise ValueError("not an AVI file")
        self.frames = []
        self.w = self.h = 0
        self.bpp = 24
        self.fps = 25.0
        self._parse(12, None)
        if not self.frames or not self.w:
            raise ValueError("no uncompressed video frames found")
        self.stride = ((self.w * self.bpp // 8) + 3) & ~3

    def _parse(self, pos, end):
        f = self.f
        f.seek(0, 2)
        fend = f.tell() if end is None else end
        while pos + 8 <= fend:
            f.seek(pos)
            cid, size = struct.unpack("<4sI", f.read(8))
            if cid == b"LIST":
                kind = f.read(4)
                if kind in (b"hdrl", b"strl", b"movi", b"rec "):
                    self._parse(pos + 12, pos + 8 + size)
            elif cid == b"strh":
                d = f.read(size)
                if d[:4] == b"vids":
                    scale, rate = struct.unpack("<II", d[20:28])
                    if scale:
                        self.fps = rate / scale
            elif cid == b"strf":
                d = f.read(size)
                if len(d) >= 16 and not self.w:
                    _, w, h, _, bpp, comp = struct.unpack("<IiiHHI", d[:20])
                    if comp not in (0,):
                        raise ValueError("compressed AVI video is not a RAW recording")
                    self.w, self.h, self.bpp = w, abs(h), bpp
                    self.bottom_up = h > 0
            elif cid[2:] in (b"db", b"dc") and size:
                self.frames.append((pos + 8, size))
            pos += 8 + size + (size & 1)

    def __len__(self):
        return len(self.frames)

    def read(self, i):
        off, size = self.frames[max(0, min(i, len(self.frames) - 1))]
        self.f.seek(off)
        d = np.frombuffer(self.f.read(size), np.uint8)
        bpc = self.bpp // 8
        a = d[: self.stride * self.h].reshape(self.h, self.stride)[:, : self.w * bpc].reshape(self.h, self.w, bpc)
        if getattr(self, "bottom_up", True):
            a = a[::-1]
        return a[:, :, 2::-1].tobytes()  # BGR(A) -> RGB

    def close(self):
        self.f.close()


# --- processed video (ffmpeg) -------------------------------------------------------------

class VideoWriter:
    """Writes the processed live view to a video file through ffmpeg (H.264 MP4), falling back
    to an uncompressed AVI when ffmpeg is missing."""

    def __init__(self, path, width, height, fps):
        self.w, self.h = width - width % 2, height - height % 2
        self.proc = None
        self.avi = None
        if shutil.which("ffmpeg"):
            self.path = str(path) + ".mp4"
            self.proc = subprocess.Popen(
                ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                 "-s", f"{self.w}x{self.h}", "-r", f"{fps:.3f}", "-i", "-", "-c:v", "libx264",
                 "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "18", self.path],
                stdin=subprocess.PIPE)
        else:
            self.path = str(path) + ".avi"
            self.avi = AviWriter(self.path, self.w, self.h, fps)

    def write(self, rgb):
        a = np.ascontiguousarray(rgb[: self.h, : self.w, :3], dtype=np.uint8)
        if a.shape[0] != self.h or a.shape[1] != self.w:
            return
        if self.proc:
            try:
                self.proc.stdin.write(a.tobytes())
            except (BrokenPipeError, ValueError):
                pass
        else:
            self.avi.write(a.tobytes())

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()
            except (BrokenPipeError, ValueError):
                pass
            self.proc.wait(timeout=30)
        elif self.avi:
            self.avi.close()
