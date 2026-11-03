"""IRW1: the wireless P2 Pro protocol (bridge <-> viewer). See docs/WIRELESS_PLAN.md.

Frames travel over UDP as independently decodable row bands of the camera's temperature
half (256x192 u16). Control is newline-delimited JSON over TCP. The viewer always opens both
flows (TCP connect + UDP subscribe), so frames arrive as replies through firewalls and NAT.
Needs only the standard library and numpy.
"""
import json
import socket
import struct
import zlib

import numpy as np

DATA_PORT = 47800
CTRL_PORT = 47801
DEFAULT_HOST = "p2pro.local"

MAGIC = b"IRW1"
SUB_MAGIC = b"IRWS"
VERSION = 1

CODEC_RAW = 0
CODEC_ZLIB = 1          # horizontal delta + zigzag + hi/lo byte planes + zlib level 1
CODECS = (CODEC_RAW, CODEC_ZLIB)

FLAG_HIGH_RANGE = 1
FLAG_FULL_FRAME = 2

HEADER = struct.Struct("<4sBBHIQHH")   # magic ver codec flags frame_id ts_us row_start row_count
MAX_DATAGRAM = 1400
PAYLOAD = MAX_DATAGRAM - HEADER.size
BAND_ROWS = {CODEC_RAW: 2, CODEC_ZLIB: 8}

WIDTH = 256
FRAME_ROWS = 384
TEMP_ROW0 = 192          # first row of the temperature half in the camera frame


class ProtocolError(ValueError):
    pass


# ------------------------------------------------------------------------------ codec
def _planes(rows):
    """Horizontal delta + zigzag, then all high bytes followed by all low bytes. Stays in 16 bits
    (uint16 subtraction wraps exactly like a signed 16-bit delta), which is ~5x faster than going
    through int32 on the Pi; the output is byte-identical to the original formulation."""
    d = rows.copy()
    d[:, 1:] -= rows[:, :-1]
    s = d.view(np.int16)
    z = ((s << 1) ^ (s >> 15)).view(np.uint16).ravel()  # zigzag: small magnitudes -> small values
    out = np.empty(2 * z.size, np.uint8)
    np.right_shift(z, 8, out=out[:z.size], casting="unsafe")
    out[z.size:] = z                                     # low bytes (truncating copy)
    return out


def _encode_zlib(rows):
    return zlib.compress(_planes(rows), 1)


def _decode_zlib(payload, nrows, width):
    n = nrows * width
    try:
        raw = zlib.decompress(payload)
    except zlib.error as e:
        raise ProtocolError(f"bad zlib band: {e}") from None
    if len(raw) != 2 * n:
        raise ProtocolError("zlib band has the wrong size")
    b = np.frombuffer(raw, np.uint8)
    z = (b[:n].astype(np.uint16) << 8) | b[n:]
    s = (z >> 1).astype(np.int32) ^ -(z & 1).astype(np.int32)
    return (np.cumsum(s.reshape(nrows, width), axis=1, dtype=np.int64) & 0xFFFF).astype(np.uint16)


def encode_band(rows, codec):
    rows = np.ascontiguousarray(rows, dtype=np.uint16)
    if codec == CODEC_RAW:
        return rows.astype("<u2").tobytes()
    if codec == CODEC_ZLIB:
        return _encode_zlib(rows)
    raise ProtocolError(f"unknown codec {codec}")


def decode_band(payload, codec, nrows, width=WIDTH):
    if codec == CODEC_RAW:
        if len(payload) != 2 * nrows * width:
            raise ProtocolError("raw band has the wrong size")
        return np.frombuffer(payload, "<u2").reshape(nrows, width).astype(np.uint16)
    if codec == CODEC_ZLIB:
        return _decode_zlib(payload, nrows, width)
    raise ProtocolError(f"unknown codec {codec}")


# ------------------------------------------------------------------------------ frames
def packetize(rows, frame_id, ts_us, codec=CODEC_ZLIB, flags=0):
    """Split a frame (N x 256 u16) into datagrams of at most MAX_DATAGRAM bytes."""
    out = []
    rows = np.ascontiguousarray(rows, dtype=np.uint16)
    n, w = rows.shape
    step = BAND_ROWS[codec]
    if codec == CODEC_ZLIB:
        # delta/zigzag work row by row, so the planes of any band are slices of the whole frame's
        # planes: transform once, then only compress per band (same bytes as encode_band)
        planes = _planes(rows)
        hi, lo = planes[:n * w], planes[n * w:]

        def enc(r, cnt):
            return zlib.compress(np.concatenate((hi[r * w:(r + cnt) * w], lo[r * w:(r + cnt) * w])), 1)
    else:
        def enc(r, cnt):
            return encode_band(rows[r:r + cnt], codec)
    r = 0
    while r < n:
        cnt = min(step, n - r)
        c = codec
        payload = enc(r, cnt)
        while len(payload) > PAYLOAD and cnt > 1:   # rare: noisy band, halve it
            cnt //= 2
            payload = enc(r, cnt)
        if len(payload) > PAYLOAD:                   # a single row that won't compress: send raw
            c = CODEC_RAW
            payload = encode_band(rows[r:r + cnt], c)
        out.append(HEADER.pack(MAGIC, VERSION, c, flags, frame_id & 0xFFFFFFFF, ts_us, r, cnt) + payload)
        r += cnt
    return out


def parse(datagram):
    if len(datagram) < HEADER.size:
        raise ProtocolError("short datagram")
    magic, ver, codec, flags, fid, ts, r0, cnt = HEADER.unpack_from(datagram)
    if magic != MAGIC or ver != VERSION:
        raise ProtocolError("not an IRW1 datagram")
    return codec, flags, fid, ts, r0, cnt, datagram[HEADER.size:]


def _newer(a, b):
    """True when frame id a is after b (32-bit wrap-around)."""
    return 0 < ((a - b) & 0xFFFFFFFF) < 0x80000000


class Assembler:
    """Collects row bands into frames. A frame is published when all rows have arrived, or
    when a newer frame starts (missing rows keep the previous frame's values). No waiting
    for retransmissions: latency first."""

    def __init__(self, rows, width=WIDTH):
        self.rows, self.width = rows, width
        self.buf = np.zeros((rows, width), np.uint16)
        self.have = np.zeros(rows, bool)
        self.fid = None
        self.done = False           # current fid already published
        self.ts = 0
        self.flags = 0
        self.frames = self.partial = self.lost_rows = self.bad = 0

    def _publish(self):
        missing = int(self.rows - self.have.sum())
        self.frames += 1
        if missing:
            self.partial += 1
            self.lost_rows += missing
        self.done = True
        return self.buf.copy(), self.fid, self.ts, self.flags

    def feed(self, datagram):
        """Returns a list of finished frames (0, 1 or 2) as (rows, frame_id, ts_us, flags)."""
        try:
            codec, flags, fid, ts, r0, cnt, payload = parse(datagram)
            if r0 + cnt > self.rows or cnt == 0:
                raise ProtocolError("band outside the frame")
            band = decode_band(payload, codec, cnt, self.width)
        except ProtocolError:
            self.bad += 1
            return []
        out = []
        if self.fid is None or _newer(fid, self.fid):
            if self.fid is not None and not self.done:
                out.append(self._publish())
            self.fid, self.ts, self.flags, self.done = fid, ts, flags, False
            self.have[:] = False
        elif fid != self.fid or self.done:
            return out              # late band from an older frame, or a duplicate
        self.buf[r0:r0 + cnt] = band
        self.have[r0:r0 + cnt] = True
        if self.have.all():
            out.append(self._publish())
        return out

    def loss(self):
        """Fraction of rows lost so far."""
        return self.lost_rows / (self.frames * self.rows) if self.frames else 0.0


# ------------------------------------------------------------------------------ control
def subscribe_packet(token):
    return SUB_MAGIC + token.encode()[:16]


def parse_subscribe(data):
    if data.startswith(SUB_MAGIC):
        return data[len(SUB_MAGIC):].decode(errors="replace")
    return None


def send_msg(sock, obj):
    sock.sendall(json.dumps(obj, separators=(",", ":")).encode() + b"\n")


class LineReader:
    """Reads newline-delimited JSON objects from a blocking socket."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def read(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self.buf += chunk
            if len(self.buf) > 1 << 20:
                raise ProtocolError("control line too long")
        line, self.buf = self.buf.split(b"\n", 1)
        try:
            msg = json.loads(line)
        except ValueError:
            raise ProtocolError("bad control message") from None
        if not isinstance(msg, dict):
            raise ProtocolError("bad control message")
        return msg


def split_host(text, default_port=CTRL_PORT):
    """'host', 'host:port' or '[v6]:port' -> (host, port)."""
    text = (text or DEFAULT_HOST).strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if text.count(":") == 1:
        host, port = text.split(":")
        return host, int(port)
    return text, default_port


def set_video_tos(sock):
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xA0)   # CS5 -> WMM video queue
    except OSError:
        pass
