"""Minimal V4L2 capture (mmap streaming) and controls, using only the standard library.

This replaces the DirectShow layer (ds_camera*, uvc_camera*) of the Windows program.
"""
import ctypes as ct
import errno
import fcntl
import mmap
import os
import select
import time
from dataclasses import dataclass
from pathlib import Path

_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(d, nr, size):
    return (d << 30) | (size << 16) | (ord("V") << 8) | nr


class Capability(ct.Structure):
    _fields_ = [("driver", ct.c_char * 16), ("card", ct.c_char * 32), ("bus_info", ct.c_char * 32),
                ("version", ct.c_uint32), ("capabilities", ct.c_uint32), ("device_caps", ct.c_uint32),
                ("reserved", ct.c_uint32 * 3)]


class PixFormat(ct.Structure):
    _fields_ = [("width", ct.c_uint32), ("height", ct.c_uint32), ("pixelformat", ct.c_uint32),
                ("field", ct.c_uint32), ("bytesperline", ct.c_uint32), ("sizeimage", ct.c_uint32),
                ("colorspace", ct.c_uint32), ("priv", ct.c_uint32), ("flags", ct.c_uint32),
                ("ycbcr_enc", ct.c_uint32), ("quantization", ct.c_uint32), ("xfer_func", ct.c_uint32)]


class _FmtUnion(ct.Union):
    # the kernel union also holds struct v4l2_window (pointers): pointer alignment, so 204 bytes
    # in all on 32-bit ARM and 208 on 64-bit
    _fields_ = [("pix", PixFormat), ("raw", ct.c_uint8 * 200), ("_align", ct.c_void_p)]


class Format(ct.Structure):
    _fields_ = [("type", ct.c_uint32), ("fmt", _FmtUnion)]


class FmtDesc(ct.Structure):
    _fields_ = [("index", ct.c_uint32), ("type", ct.c_uint32), ("flags", ct.c_uint32),
                ("description", ct.c_char * 32), ("pixelformat", ct.c_uint32),
                ("mbus_code", ct.c_uint32), ("reserved", ct.c_uint32 * 3)]


class FrmSizeDiscrete(ct.Structure):
    _fields_ = [("width", ct.c_uint32), ("height", ct.c_uint32)]


class _FrmSizeUnion(ct.Union):
    _fields_ = [("discrete", FrmSizeDiscrete), ("stepwise", ct.c_uint32 * 6)]


class FrmSizeEnum(ct.Structure):
    _fields_ = [("index", ct.c_uint32), ("pixel_format", ct.c_uint32), ("type", ct.c_uint32),
                ("u", _FrmSizeUnion), ("reserved", ct.c_uint32 * 2)]


class RequestBuffers(ct.Structure):
    _fields_ = [("count", ct.c_uint32), ("type", ct.c_uint32), ("memory", ct.c_uint32),
                ("capabilities", ct.c_uint32), ("flags", ct.c_uint8), ("reserved", ct.c_uint8 * 3)]


class Timeval(ct.Structure):
    # the kernel's native v4l2 timeval is 2 x 64 bits on every architecture (32-bit systems with
    # 64-bit time_t, e.g. Debian trixie armhf, use it); same as long on 64-bit
    _fields_ = [("sec", ct.c_int64), ("usec", ct.c_int64)]


class Timecode(ct.Structure):
    _fields_ = [("type", ct.c_uint32), ("flags", ct.c_uint32), ("frames", ct.c_uint8),
                ("seconds", ct.c_uint8), ("minutes", ct.c_uint8), ("hours", ct.c_uint8),
                ("userbits", ct.c_uint8 * 4)]


class _BufM(ct.Union):
    _fields_ = [("offset", ct.c_uint32), ("userptr", ct.c_ulong), ("planes", ct.c_void_p), ("fd", ct.c_int32)]


class Buffer(ct.Structure):
    _fields_ = [("index", ct.c_uint32), ("type", ct.c_uint32), ("bytesused", ct.c_uint32),
                ("flags", ct.c_uint32), ("field", ct.c_uint32), ("timestamp", Timeval),
                ("timecode", Timecode), ("sequence", ct.c_uint32), ("memory", ct.c_uint32),
                ("m", _BufM), ("length", ct.c_uint32), ("reserved2", ct.c_uint32),
                ("request_fd", ct.c_int32)]


class Control(ct.Structure):
    _fields_ = [("id", ct.c_uint32), ("value", ct.c_int32)]


class Fract(ct.Structure):
    _fields_ = [("numerator", ct.c_uint32), ("denominator", ct.c_uint32)]


class CaptureParm(ct.Structure):
    _fields_ = [("capability", ct.c_uint32), ("capturemode", ct.c_uint32), ("timeperframe", Fract),
                ("extendedmode", ct.c_uint32), ("readbuffers", ct.c_uint32), ("reserved", ct.c_uint32 * 4)]


class _ParmUnion(ct.Union):
    _fields_ = [("capture", CaptureParm), ("raw", ct.c_uint8 * 200)]


class StreamParm(ct.Structure):
    _fields_ = [("type", ct.c_uint32), ("parm", _ParmUnion)]


QUERYCAP = _ioc(_IOC_READ, 0, ct.sizeof(Capability))
ENUM_FMT = _ioc(_IOC_READ | _IOC_WRITE, 2, ct.sizeof(FmtDesc))
G_FMT = _ioc(_IOC_READ | _IOC_WRITE, 4, ct.sizeof(Format))
S_FMT = _ioc(_IOC_READ | _IOC_WRITE, 5, ct.sizeof(Format))
REQBUFS = _ioc(_IOC_READ | _IOC_WRITE, 8, ct.sizeof(RequestBuffers))
QUERYBUF = _ioc(_IOC_READ | _IOC_WRITE, 9, ct.sizeof(Buffer))
QBUF = _ioc(_IOC_READ | _IOC_WRITE, 15, ct.sizeof(Buffer))
DQBUF = _ioc(_IOC_READ | _IOC_WRITE, 17, ct.sizeof(Buffer))
STREAMON = _ioc(_IOC_WRITE, 18, ct.sizeof(ct.c_int))
STREAMOFF = _ioc(_IOC_WRITE, 19, ct.sizeof(ct.c_int))
S_PARM = _ioc(_IOC_READ | _IOC_WRITE, 22, ct.sizeof(StreamParm))
G_CTRL = _ioc(_IOC_READ | _IOC_WRITE, 27, ct.sizeof(Control))
S_CTRL = _ioc(_IOC_READ | _IOC_WRITE, 28, ct.sizeof(Control))
ENUM_FRAMESIZES = _ioc(_IOC_READ | _IOC_WRITE, 74, ct.sizeof(FrmSizeEnum))

BUF_TYPE_CAPTURE = 1
MEMORY_MMAP = 1
CAP_VIDEO_CAPTURE = 0x1
CID_ZOOM_ABSOLUTE = 0x009A090D


def fourcc(s):
    return ord(s[0]) | ord(s[1]) << 8 | ord(s[2]) << 16 | ord(s[3]) << 24


def fourcc_str(v):
    return "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4))


YUYV = fourcc("YUYV")
H264 = fourcc("H264")


def _ioctl(fd, req, arg):
    while True:
        try:
            return fcntl.ioctl(fd, req, arg)
        except InterruptedError:
            continue


@dataclass
class VideoDevice:
    path: str
    card: str
    vid: int = 0
    pid: int = 0
    bus_info: str = ""

    def label(self):
        ids = f" [{self.vid:04x}:{self.pid:04x}]" if self.vid else ""
        return f"{self.card} ({self.path}){ids}"


def _usb_ids(node):
    try:
        p = Path(f"/sys/class/video4linux/{node}/device").resolve()
        for d in (p, *p.parents):
            v, q = d / "idVendor", d / "idProduct"
            if v.exists() and q.exists():
                return int(v.read_text(), 16), int(q.read_text(), 16)
    except (OSError, ValueError):
        pass
    return 0, 0


def list_devices():
    """Capture-capable V4L2 nodes with their card name and USB IDs."""
    out = []
    base = Path("/sys/class/video4linux")
    if not base.is_dir():
        return out
    for node in sorted(base.iterdir(), key=lambda p: (len(p.name), p.name)):
        path = f"/dev/{node.name}"
        try:
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            cap = Capability()
            _ioctl(fd, QUERYCAP, cap)
            caps = cap.device_caps or cap.capabilities
            if not caps & CAP_VIDEO_CAPTURE:
                continue
            vid, pid = _usb_ids(node.name)
            out.append(VideoDevice(path, cap.card.decode(errors="replace").strip(), vid, pid,
                                   cap.bus_info.decode(errors="replace")))
        except OSError:
            continue
        finally:
            os.close(fd)
    return out


class V4L2Camera:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.buffers = []
        self.streaming = False
        self.width = self.height = 0
        self.pixfmt = 0
        self._fps_t = []

    # --- formats -------------------------------------------------------------------
    def formats(self):
        res = []
        i = 0
        while True:
            d = FmtDesc(index=i, type=BUF_TYPE_CAPTURE)
            try:
                _ioctl(self.fd, ENUM_FMT, d)
            except OSError:
                break
            sizes = []
            j = 0
            while True:
                s = FrmSizeEnum(index=j, pixel_format=d.pixelformat)
                try:
                    _ioctl(self.fd, ENUM_FRAMESIZES, s)
                except OSError:
                    break
                if s.type == 1:
                    sizes.append((s.u.discrete.width, s.u.discrete.height))
                j += 1
            res.append((d.pixelformat, sizes))
            i += 1
        return res

    def set_format(self, width=None, height=None, pixfmt=None):
        f = Format(type=BUF_TYPE_CAPTURE)
        _ioctl(self.fd, G_FMT, f)
        if width:
            f.fmt.pix.width = width
        if height:
            f.fmt.pix.height = height
        if pixfmt:
            f.fmt.pix.pixelformat = pixfmt
        _ioctl(self.fd, S_FMT, f)
        self.width, self.height, self.pixfmt = f.fmt.pix.width, f.fmt.pix.height, f.fmt.pix.pixelformat
        return self.width, self.height, self.pixfmt

    def set_fps(self, fps):
        p = StreamParm(type=BUF_TYPE_CAPTURE)
        p.parm.capture.timeperframe.numerator = 1
        p.parm.capture.timeperframe.denominator = int(round(fps))
        try:
            _ioctl(self.fd, S_PARM, p)
        except OSError:
            pass

    # --- controls ------------------------------------------------------------------
    def set_control(self, cid, value):
        c = Control(id=cid, value=ct.c_int32(value).value)
        _ioctl(self.fd, S_CTRL, c)

    def get_control(self, cid):
        c = Control(id=cid)
        _ioctl(self.fd, G_CTRL, c)
        return c.value

    def set_zoom(self, value):
        """IRThermalCamera.setZoom(): the InfiRay vendor command channel (pool 1/3)."""
        self.set_control(CID_ZOOM_ABSOLUTE, value)

    # --- streaming -----------------------------------------------------------------
    def start(self, nbuf=4):
        if self.streaming:
            return
        req = RequestBuffers(count=nbuf, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP)
        _ioctl(self.fd, REQBUFS, req)
        self.buffers = []
        for i in range(req.count):
            b = Buffer(index=i, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP)
            _ioctl(self.fd, QUERYBUF, b)
            mm = mmap.mmap(self.fd, b.length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                           offset=b.m.offset)
            self.buffers.append(mm)
            _ioctl(self.fd, QBUF, b)
        _ioctl(self.fd, STREAMON, ct.c_int(BUF_TYPE_CAPTURE))
        self.streaming = True

    def stop(self):
        if not self.streaming:
            return
        try:
            _ioctl(self.fd, STREAMOFF, ct.c_int(BUF_TYPE_CAPTURE))
        except OSError:
            pass
        for mm in self.buffers:
            try:
                mm.close()
            except (BufferError, ValueError):
                pass
        self.buffers = []
        try:
            _ioctl(self.fd, REQBUFS, RequestBuffers(count=0, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP))
        except OSError:
            pass
        self.streaming = False

    def read(self, timeout=1.0, want_size=None):
        """Return the newest frame's bytes (or None on timeout). Raises OSError(ENODEV) when
        the camera was unplugged. want_size: only accept buffers with this payload size."""
        deadline = time.monotonic() + timeout
        latest = None
        while True:
            r, _, _ = select.select([self.fd], [], [], max(0.0, deadline - time.monotonic()) if latest is None else 0)
            if not r:
                return latest
            b = Buffer(type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP)
            try:
                _ioctl(self.fd, DQBUF, b)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    if latest is not None or time.monotonic() >= deadline:
                        return latest
                    continue
                raise
            data = bytes(self.buffers[b.index][: b.bytesused])
            _ioctl(self.fd, QBUF, b)
            if b.flags & 0x40:  # V4L2_BUF_FLAG_ERROR
                continue
            if want_size is not None and len(data) != want_size:
                continue
            latest = data
            now = time.monotonic()
            self._fps_t.append(now)
            self._fps_t = [t for t in self._fps_t if now - t < 2.0]

    def fps(self):
        t = self._fps_t
        if len(t) < 2:
            return 0.0
        return (len(t) - 1) / (t[-1] - t[0]) if t[-1] > t[0] else 0.0

    def close(self):
        self.stop()
        try:
            os.close(self.fd)
        except OSError:
            pass
