#!/usr/bin/env python3
"""ircam-bridge: streams an InfiRay P2 Pro's temperature data over WiFi (IRW1 protocol).

Runs on the Raspberry Pi Zero 2 W the camera is plugged into (or on any Linux box for
testing). The viewer (ircam-linux, "InfiRay P2 Pro (Wireless)") connects to TCP 47801 and
subscribes from its UDP socket to port 47800; frames are sent back to that socket.

    python3 ircam_bridge.py               # real camera, auto-detected
    python3 ircam_bridge.py --demo        # simulated P2 Pro, no camera needed
"""
import argparse
import json
import logging
import os
import secrets
import socket
import struct
import sys
import threading
import time
from pathlib import Path

here = Path(__file__).resolve().parent
for p in (here, here.parent):          # installed (/opt/ircam-bridge) or repo checkout
    if (p / "ircam").is_dir():
        sys.path.insert(0, str(p))
        break

import numpy as np  # noqa: E402

from ircam.net import protocol as P  # noqa: E402

__version__ = "0.1.0"
log = logging.getLogger("ircam-bridge")
FRAME_BYTES = P.WIDTH * P.FRAME_ROWS * 2
SUB_TIMEOUT = 3.0

# Local tap (--tap): a Unix socket for other programs on the Pi (ircam_web.py, the browser
# viewer). It is separate from the IRW1 viewer slot, so it never takes the stream away from
# ircam-linux / ircam-android. Messages to the client: u32 length + 1 byte kind + body.
#   kind B: u16 count, then per IRW1 datagram: u16 length + the datagram. These are the very
#           same codec-1 datagrams the IRW1 viewer gets: each frame is compressed only once.
#   kind J: JSON (replies to commands, which arrive as JSON lines)
TAP_LEN = struct.Struct("<Ic")
U16 = struct.Struct("<H")


def now_us():
    return time.monotonic_ns() // 1000


def hangup(sock):
    """Close a socket another thread may be blocked on (close() alone sends no FIN then)."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# ------------------------------------------------------------------------------ cameras
class RealCamera:
    """The P2 Pro through V4L2 plus its vendor channel for range switching."""
    model = "InfiRay P2 Pro"

    def __init__(self, device="auto"):
        self.device = device
        self.dev = None
        self.p2 = None
        self.serial = ""

    def open(self):
        from ircam import cameras as C, v4l2
        from ircam.sources import find_v4l2_device
        model = C.MODEL_BY_NAME["InfiRay Or Thermal Master P2Pro"]
        found, devs = find_v4l2_device(model, self.device)
        if not found:
            raise OSError("P2 Pro not found (video devices: %s)" % (", ".join(d.path for d in devs) or "none"))
        self.dev = v4l2.V4L2Camera(found.path)
        self.dev.set_format(P.WIDTH, P.FRAME_ROWS, v4l2.YUYV)
        if (self.dev.width, self.dev.height) != (P.WIDTH, P.FRAME_ROWS):
            raise OSError(f"camera delivers {self.dev.width}x{self.dev.height}, expected 256x384")
        self.dev.set_fps(25)
        self.serial = found.card
        try:
            from ircam.p2pro import P2ProControl
            self.p2 = P2ProControl()
        except Exception as e:  # noqa: BLE001 - streaming works without it
            log.warning("vendor channel unavailable, no range switching: %s", e)
        self.dev.start()
        log.info("camera open: %s", found.label())

    @property
    def can_range(self):
        return self.p2 is not None

    def read(self):
        raw = self.dev.read(1.0, FRAME_BYTES)
        if raw is None or len(raw) != FRAME_BYTES:
            return None
        return np.frombuffer(raw, "<u2").reshape(P.FRAME_ROWS, P.WIDTH)

    def high_range(self):
        return None if self.p2 is None else not self.p2.high_gain()

    def set_high_range(self, high):
        self.p2.set_high_gain(not high)       # high gain = low temperature range

    def close(self):
        for o in (self.dev, self.p2):
            try:
                if o:
                    o.close()
            except Exception:  # noqa: BLE001
                pass
        self.dev = self.p2 = None


class DemoCamera:
    """The app's simulated P2 Pro, for testing without hardware."""
    model = "Demo P2 Pro"
    can_range = True
    serial = "demo"

    def open(self):
        from ircam.sources import DemoSource
        self.src = DemoSource()
        self.src.open(lambda *a: None)
        self._high = False

    def read(self):
        return np.frombuffer(self.src.read(), "<u2").reshape(P.FRAME_ROWS, P.WIDTH)

    def high_range(self):
        return self._high

    def set_high_range(self, high):
        self._high = bool(high)

    def close(self):
        pass


# ------------------------------------------------------------------------------ system info
def wifi_rssi(iface="wlan0"):
    try:
        for line in Path("/proc/net/wireless").read_text().splitlines()[2:]:
            name, rest = line.split(":", 1)
            if name.strip() == iface:
                return int(float(rest.split()[2]))
    except (OSError, ValueError, IndexError):
        pass
    return None


def cpu_temp():
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


_cpu = {"t": 0.0, "busy": 0, "total": 0, "pct": None}


def cpu_usage():
    """Whole-system CPU use in % since the previous sample (refreshed at most once a second)."""
    now = time.monotonic()
    if now - _cpu["t"] < 1.0:
        return _cpu["pct"]
    try:
        f = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    except (OSError, ValueError):
        return None
    total, idle = sum(f[:8]), f[3] + (f[4] if len(f) > 4 else 0)
    busy = total - idle
    if _cpu["total"] and total > _cpu["total"]:
        _cpu["pct"] = round(100.0 * (busy - _cpu["busy"]) / (total - _cpu["total"]), 1)
    _cpu.update(t=now, busy=busy, total=total)
    return _cpu["pct"]


def pisugar_battery():
    """Battery % from the PiSugar power manager, if installed."""
    try:
        with socket.create_connection(("127.0.0.1", 8423), timeout=0.2) as s:
            s.sendall(b"get battery\n")
            return round(float(s.recv(64).decode().split(":")[1]), 1)
    except (OSError, ValueError, IndexError):
        return None


# ------------------------------------------------------------------------------ bridge
class Bridge:
    def __init__(self, cam, bind="0.0.0.0", data_port=P.DATA_PORT, ctrl_port=P.CTRL_PORT, iface="wlan0"):
        self.cam = cam
        self.iface = iface
        self.alive = True
        self.cam_ok = False
        self.cam_lock = threading.Lock()
        self.cond = threading.Condition()
        self.latest = None            # (frame rows, ts_us, frame_id)
        self.frame_id = 0
        self.cap_times = []
        self.high = False
        # session
        self.session = None           # dict: token, codec, full_frame, fps_limit
        self.client_addr = None
        self.last_sub = 0.0
        self.streaming = False
        self.sent_frames = self.skipped = 0
        self.ctrl_conn = None
        # sockets
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        P.set_video_tos(self.udp)
        self.udp.bind((bind, data_port))
        self.udp.settimeout(1.0)
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind((bind, ctrl_port))
        self.tcp.listen(2)
        self.tcp.settimeout(1.0)
        self.data_port = self.udp.getsockname()[1]
        self.ctrl_port = self.tcp.getsockname()[1]
        self.tap = None
        self.tap_conns = set()
        self.web_viewers = 0          # browsers watching through the tap (reported by ircam-web)
        self.enc_lock = threading.Lock()
        self.enc_key = self.enc_val = None   # newest encoded frame, shared by IRW1 and the tap

    # threads -------------------------------------------------------------------------
    def start(self, status_file=None, tap=None):
        for fn in (self._capture_loop, self._send_loop, self._sub_loop, self._accept_loop):
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
        if status_file:
            threading.Thread(target=self._status_file_loop, args=(status_file,), daemon=True).start()
        if tap:
            self._open_tap(tap)
        log.info("listening: control tcp/%d, data udp/%d", self.ctrl_port, self.data_port)

    def stop(self):
        self.alive = False
        with self.cond:
            self.cond.notify_all()
        for s in (self.ctrl_conn, self.tcp, self.udp, self.tap, *list(self.tap_conns)):
            hangup(s)
        self.cam.close()

    def _capture_loop(self):
        while self.alive:
            try:
                with self.cam_lock:
                    self.cam.open()
                    hr = self.cam.high_range() if self.cam.can_range else None
                self.high = bool(hr)
                self.cam_ok = True
                fails = 0
                while self.alive:
                    rows = self.cam.read()
                    if rows is None:
                        fails += 1
                        if fails > 5:
                            raise OSError("camera stopped delivering frames")
                        continue
                    fails = 0
                    t = now_us()
                    with self.cond:
                        self.frame_id = (self.frame_id + 1) & 0xFFFFFFFF
                        self.latest = (rows, t, self.frame_id)
                        self.cap_times = [x for x in self.cap_times[-60:] if t - x < 2_000_000] + [t]
                        self.cond.notify_all()
            except Exception as e:  # noqa: BLE001 - keep serving, retry the camera
                if self.alive:
                    log.warning("camera: %s; retrying in 2 s", e)
            self.cam_ok = False
            with self.cam_lock:
                self.cam.close()
            time.sleep(2)

    def _send_loop(self):
        last_id, last_sent = None, 0.0
        while self.alive:
            with self.cond:
                self.cond.wait_for(lambda: not self.alive or (self.latest and self.latest[2] != last_id), 1.0)
                item = self.latest
            if not self.alive or item is None or item[2] == last_id:
                continue
            rows, ts, fid = item
            if last_id is not None and ((fid - last_id) & 0xFFFFFFFF) > 1 and self.streaming:
                self.skipped += ((fid - last_id) & 0xFFFFFFFF) - 1
            last_id = fid
            sess, addr = self.session, self.client_addr
            if not (self.streaming and sess and addr):
                continue
            if time.monotonic() - self.last_sub > SUB_TIMEOUT:
                log.info("client %s stopped subscribing; pausing stream", addr)
                self.streaming = False
                continue
            lim = sess["fps_limit"]
            if lim and time.monotonic() - last_sent < 1.0 / lim - 0.004:
                continue
            last_sent = time.monotonic()
            try:
                for dg in self._encoded(item, sess["codec"], sess["full_frame"]):
                    self.udp.sendto(dg, addr)
                self.sent_frames += 1
            except OSError as e:
                log.debug("send: %s", e)

    def _encoded(self, item, codec=P.CODEC_ZLIB, full=False):
        """The frame as IRW1 datagrams, compressed once and shared: when the IRW1 viewer and the
        browser tap want the same frame, the second one gets the cached result."""
        rows, ts, fid = item
        key = (fid, codec, full)
        with self.enc_lock:
            if self.enc_key != key:
                flags = (P.FLAG_HIGH_RANGE if self.high else 0) | (P.FLAG_FULL_FRAME if full else 0)
                self.enc_val = P.packetize(rows if full else rows[P.TEMP_ROW0:], fid, ts, codec, flags)
                self.enc_key = key
            return self.enc_val

    def _sub_loop(self):
        while self.alive:
            try:
                data, addr = self.udp.recvfrom(256)
            except OSError:
                continue
            tok = P.parse_subscribe(data)
            sess = self.session
            if tok is not None and sess and secrets.compare_digest(tok, sess["token"]):
                if addr != self.client_addr:
                    log.info("streaming to %s:%d", *addr)
                self.client_addr = addr
                self.last_sub = time.monotonic()

    def _accept_loop(self):
        while self.alive:
            try:
                conn, addr = self.tcp.accept()
            except OSError:
                continue
            old, self.ctrl_conn = self.ctrl_conn, conn
            if old:                          # one viewer at a time: the newest wins
                log.info("new viewer %s replaces the previous one", addr[0])
                hangup(old)
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True).start()

    def status(self):
        t = self.cap_times
        fps = (len(t) - 1) / ((t[-1] - t[0]) / 1e6) if len(t) > 1 and t[-1] > t[0] else 0.0
        return {"camera_ok": self.cam_ok, "camera": self.cam.model, "cam_fps": round(fps, 1),
                "viewer": self.client_addr[0] if (self.streaming and self.client_addr) else None,
                "sent": self.sent_frames, "skipped": self.skipped, "rssi_dbm": wifi_rssi(self.iface),
                "cpu_temp": cpu_temp(), "cpu_pct": cpu_usage(), "battery_pct": pisugar_battery(), "bridge": __version__,
                "web_viewers": self.web_viewers if self.tap_conns else 0}

    def _status_file_loop(self, path):
        """Publish the status for the setup page (it must not take over the viewer slot)."""
        path = Path(path)
        tmp = path.with_suffix(".tmp")
        while self.alive:
            try:
                tmp.write_text(json.dumps({**self.status(), "time": time.time()}))
                os.replace(tmp, path)
            except OSError as e:
                log.debug("status file: %s", e)
            time.sleep(2)

    # local tap -----------------------------------------------------------------------
    def _open_tap(self, path):
        path = Path(path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.tap = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.tap.bind(str(path))
        os.chmod(path, 0o660)                 # the bridge user and its group only
        self.tap.listen(4)
        self.tap.settimeout(1.0)
        threading.Thread(target=self._tap_accept, daemon=True, name="tap").start()
        log.info("local tap on %s", path)

    def _tap_accept(self):
        while self.alive:
            try:
                conn, _ = self.tap.accept()
            except OSError:
                continue
            if len(self.tap_conns) >= 4:
                hangup(conn)
                continue
            self.tap_conns.add(conn)
            threading.Thread(target=self._tap_serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _tap_send(conn, lock, kind, body):
        with lock:
            conn.sendall(TAP_LEN.pack(len(body) + 1, kind) + body)

    def _tap_serve(self, conn):
        """Frames go out as they are captured (the newest one; a slow reader skips frames).
        Commands are read on a second thread."""
        lock = threading.Lock()
        conn.settimeout(5.0)
        log.info("tap client connected")

        def commands():
            rd = P.LineReader(conn)
            try:
                while self.alive:
                    try:
                        msg = rd.read()
                    except socket.timeout:
                        continue
                    reply = self._tap_cmd(msg)
                    reply["id"] = msg.get("id")
                    self._tap_send(conn, lock, b"J", json.dumps(reply, separators=(",", ":")).encode())
            except (OSError, P.ProtocolError):
                pass
            hangup(conn)

        threading.Thread(target=commands, daemon=True).start()
        last_id = None
        try:
            while self.alive:
                with self.cond:
                    self.cond.wait_for(lambda: not self.alive or (self.latest and self.latest[2] != last_id), 1.0)
                    item = self.latest
                if not self.alive or item is None or item[2] == last_id:
                    continue
                last_id = item[2]
                dgs = self._encoded(item)
                body = U16.pack(len(dgs)) + b"".join(U16.pack(len(d)) + d for d in dgs)
                self._tap_send(conn, lock, b"B", body)
        except OSError:
            pass
        finally:
            self.tap_conns.discard(conn)
            hangup(conn)
            log.info("tap client gone")

    def _tap_cmd(self, msg):
        cmd = msg.get("cmd")
        if cmd == "status":
            return {"ok": True, **self.status(), "high": self.high, "can_range": bool(self.cam.can_range),
                    "model": self.cam.model}
        if cmd == "viewers":
            self.web_viewers = max(0, int(msg.get("n", 0) or 0))
            return {"ok": True}
        if cmd == "set_range":
            if not self.cam.can_range:
                return {"ok": False, "error": "range switching unavailable"}
            high = bool(msg.get("high"))
            with self.cam_lock:
                self.cam.set_high_range(high)
            self.high = high
            log.info("range set to %s (browser viewer)", "high" if high else "low")
            return {"ok": True, "high": high}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    # control -------------------------------------------------------------------------
    def _serve(self, conn, addr):
        log.info("viewer connected: %s", addr[0])
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(SUB_TIMEOUT + 2)
        rd = P.LineReader(conn)
        try:
            while self.alive:
                msg = rd.read()
                reply = self._handle(msg)
                reply["id"] = msg.get("id")
                P.send_msg(conn, reply)
        except (OSError, P.ProtocolError) as e:
            log.info("viewer %s gone (%s)", addr[0], e)
        finally:
            if self.ctrl_conn is conn:
                self.streaming = False
                self.session = None
                self.client_addr = None
                self.ctrl_conn = None
            hangup(conn)

    def _handle(self, msg):
        cmd = msg.get("cmd")
        if cmd == "hello":
            self.session = {"token": secrets.token_hex(8), "codec": P.CODEC_ZLIB, "full_frame": False, "fps_limit": 0}
            self.streaming = False
            self.client_addr = None
            return {"ok": True, "proto": P.VERSION, "bridge": __version__, "model": self.cam.model,
                    "serial": self.cam.serial, "camera_ok": self.cam_ok, "width": P.WIDTH,
                    "height": P.FRAME_ROWS, "temp_row0": P.TEMP_ROW0, "codecs": list(P.CODECS),
                    "high_range": bool(self.cam.can_range), "data_port": self.data_port,
                    "token": self.session["token"]}
        if self.session is None:
            return {"ok": False, "error": "send hello first"}
        if cmd == "start":
            codec = int(msg.get("codec", P.CODEC_ZLIB))
            if codec not in P.CODECS:
                return {"ok": False, "error": f"unsupported codec {codec}"}
            self.session.update(codec=codec, full_frame=bool(msg.get("full_frame", False)),
                                fps_limit=float(msg.get("fps_limit", 0) or 0))
            self.streaming = True
            self.last_sub = time.monotonic()
            return {"ok": True}
        if cmd == "stop":
            self.streaming = False
            return {"ok": True}
        if cmd == "ping":
            return {"ok": True, "t": msg.get("t"), "bridge_us": now_us()}
        if cmd == "get_range":
            if not self.cam.can_range:
                return {"ok": False, "error": "range switching unavailable"}
            with self.cam_lock:
                self.high = bool(self.cam.high_range())
            return {"ok": True, "high": self.high}
        if cmd == "set_range":
            if not self.cam.can_range:
                return {"ok": False, "error": "range switching unavailable"}
            high = bool(msg.get("high"))
            with self.cam_lock:
                self.cam.set_high_range(high)
            self.high = high
            log.info("range set to %s", "high" if high else "low")
            return {"ok": True, "high": high}
        if cmd == "status":
            return {"ok": True, **self.status()}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


def main():
    ap = argparse.ArgumentParser(description="Wireless bridge for the InfiRay P2 Pro (IRW1).")
    ap.add_argument("--demo", action="store_true", help="simulated camera, no hardware")
    ap.add_argument("--device", default="auto", help="V4L2 device (default: auto-detect)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--data-port", type=int, default=P.DATA_PORT)
    ap.add_argument("--ctrl-port", type=int, default=P.CTRL_PORT)
    ap.add_argument("--iface", default="wlan0", help="WiFi interface for the RSSI readout")
    ap.add_argument("--status-file", help="write a JSON status here every 2 s (for the setup page)")
    ap.add_argument("--tap", help="also offer frames on this local Unix socket (for ircam_web.py)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(message)s" if os.environ.get("INVOCATION_ID")
                        else "%(asctime)s %(levelname)s %(message)s")
    cam = DemoCamera() if a.demo else RealCamera(a.device)
    br = Bridge(cam, a.bind, a.data_port, a.ctrl_port, a.iface)
    br.start(a.status_file, a.tap)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        br.stop()


if __name__ == "__main__":
    main()
