#!/usr/bin/env python3
"""ircam-web: browser viewer for the wireless P2 Pro bridge (iPhone, iPad, any browser).

An optional add-on. It reads frames from the bridge's local tap (a Unix socket on the Pi), so
it never takes the IRW1 stream away from ircam-linux / ircam-android; both can watch at once.
It serves the viewer page on port 8080 and streams to it over a WebSocket: one message per
frame, the temperature half compressed with the IRW1 codec 1 (row delta + zigzag + byte planes
+ zlib). The bridge only does work for it while a browser is connected.

    python3 ircam_web.py                                    # on the Pi (systemd: ircam-web)
    python3 ircam_bridge.py --demo --tap /tmp/tap.sock &    # try it on a PC
    python3 ircam_web.py --tap /tmp/tap.sock --port 8080

Turned on and off from the setup page (http://p2pro.local), or: systemctl enable --now ircam-web
"""
import argparse
import base64
import hashlib
import http.server
import json
import logging
import os
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
log = logging.getLogger("ircam-web")
TAP_PATH = "/run/ircam-bridge/tap.sock"
WEB_PORT = 8080
MAX_CLIENTS = 6
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# to the browser, per frame: magic, frame id, flags, rows, width, then the zlib payload
WS_FRAME = struct.Struct("<4sIBBH")
TAP_LEN = struct.Struct("<Ic")
TAP_FRAME = struct.Struct("<IQB")
TEMP_BYTES = (P.FRAME_ROWS - P.TEMP_ROW0) * P.WIDTH * 2
STATIC = {   # path -> (file in web/, content type)
    "/": ("viewer.html", "text/html; charset=utf-8"),
    "/index.html": ("viewer.html", "text/html; charset=utf-8"),
    "/palettes.bin": ("palettes.bin", "application/octet-stream"),
    "/palettes.json": ("palettes.json", "application/json"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon-180.png": ("icon-180.png", "image/png"),
    "/favicon.ico": ("icon-180.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}


def hangup(sock):
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


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return bytes(buf)


# ------------------------------------------------------------------------------ tap -> hub
class Hub:
    """Holds the tap connection while at least one browser is connected, compresses each frame
    once and hands the newest one to every browser."""

    def __init__(self, tap_path):
        self.tap_path = tap_path
        self.cond = threading.Condition()
        self.clients = set()
        self.frame = None              # (seq, bytes) ready to send
        self.seq = 0
        self.status = {"bridge": False}
        self.status_seq = 0
        self.sock = None
        self.send_lock = threading.Lock()
        self.alive = True
        self.fps_times = []
        threading.Thread(target=self._run, daemon=True, name="tap").start()

    # browsers ------------------------------------------------------------------------
    def join(self, c):
        with self.cond:
            self.clients.add(c)
            self.cond.notify_all()
        self.command({"cmd": "viewers", "n": len(self.clients)})

    def leave(self, c):
        with self.cond:
            self.clients.discard(c)
        self.command({"cmd": "viewers", "n": len(self.clients)})

    def command(self, msg):
        s = self.sock
        if s is None:
            return False
        try:
            with self.send_lock:
                s.sendall(json.dumps(msg).encode() + b"\n")
            return True
        except OSError:
            return False

    def _set_status(self, st):
        with self.cond:
            self.status = st
            self.status_seq += 1
            self.cond.notify_all()

    # tap -----------------------------------------------------------------------------
    def _run(self):
        while self.alive:
            with self.cond:
                self.cond.wait_for(lambda: self.clients or not self.alive)
            if not self.alive:
                return
            try:
                self._session()
            except (OSError, ConnectionError, ValueError) as e:
                log.info("bridge tap: %s", e)
            self.sock = None
            self._set_status({"bridge": False})
            if self.alive and self.clients:
                time.sleep(2)

    def _session(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(self.tap_path)
        self.sock = s
        log.info("connected to the bridge tap")
        self.command({"cmd": "viewers", "n": len(self.clients)})

        def poll_status():                    # also keeps the tap busy while no frames come
            while self.sock is s and self.alive:
                if not self.command({"cmd": "status", "id": "st"}):
                    return
                time.sleep(2)

        threading.Thread(target=poll_status, daemon=True).start()
        try:
            while self.alive and self.clients:
                n, kind = TAP_LEN.unpack(recv_exact(s, TAP_LEN.size))
                if not 1 <= n <= 1 << 20:
                    raise ValueError("bad tap message")
                body = recv_exact(s, n - 1)
                if kind == b"F":
                    self._frame(body)
                elif kind == b"J":
                    msg = json.loads(body)
                    if msg.get("id") == "st":
                        msg.pop("id", None)
                        self._set_status({**msg, "bridge_version": msg.get("bridge"), "bridge": True,
                                          "web_fps": self._fps()})
                    else:
                        self._set_status({**self.status, "reply": msg})
        finally:
            self.sock = None
            hangup(s)
            log.info("tap closed (%d browsers)", len(self.clients))

    def _frame(self, body):
        if len(body) != TAP_FRAME.size + TEMP_BYTES:
            raise ValueError("bad tap frame size")
        fid, _ts, flags = TAP_FRAME.unpack_from(body)
        rows = np.frombuffer(body, "<u2", offset=TAP_FRAME.size).reshape(-1, P.WIDTH)
        payload = P.encode_band(rows, P.CODEC_ZLIB)
        msg = WS_FRAME.pack(b"IRWB", fid, flags, rows.shape[0] & 0xFF, P.WIDTH) + payload
        t = time.monotonic()
        self.fps_times = [x for x in self.fps_times[-50:] if t - x < 2] + [t]
        with self.cond:
            self.seq += 1
            self.frame = (self.seq, msg)
            self.cond.notify_all()

    def _fps(self):
        t = self.fps_times
        return round((len(t) - 1) / (t[-1] - t[0]), 1) if len(t) > 1 and t[-1] > t[0] else 0.0

    def stop(self):
        self.alive = False
        with self.cond:
            self.cond.notify_all()
        hangup(self.sock)


# ------------------------------------------------------------------------------ websocket
class WebSocket:
    """Minimal RFC 6455 server side: unfragmented text/binary out, masked frames in."""

    def __init__(self, sock):
        self.sock = sock
        self.lock = threading.Lock()

    def send(self, data, binary=True):
        if isinstance(data, str):
            data, binary = data.encode(), False
        n = len(data)
        head = bytes([0x82 if binary else 0x81])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += b"\x7e" + struct.pack(">H", n)
        else:
            head += b"\x7f" + struct.pack(">Q", n)
        with self.lock:
            self.sock.sendall(head + data)

    def _control(self, op, data=b""):
        with self.lock:
            self.sock.sendall(bytes([0x80 | op, len(data)]) + data)

    def recv(self):
        """Next text/binary message, or None when the browser closed the socket."""
        parts = []
        while True:
            b0, b1 = recv_exact(self.sock, 2)
            op, fin, n = b0 & 0x0F, b0 & 0x80, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", recv_exact(self.sock, 2))[0]
            elif n == 127:
                n = struct.unpack(">Q", recv_exact(self.sock, 8))[0]
            if n > 65536:
                raise ValueError("message too large")
            mask = recv_exact(self.sock, 4) if b1 & 0x80 else b"\0\0\0\0"
            data = bytes(x ^ mask[i & 3] for i, x in enumerate(recv_exact(self.sock, n)))
            if op == 8:
                try:
                    self._control(8)
                except OSError:
                    pass
                return None
            if op == 9:
                self._control(10, data[:125])
                continue
            if op == 10:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts)


# ------------------------------------------------------------------------------ http
class Client:
    __slots__ = ("ws", "alive")

    def __init__(self, ws):
        self.ws, self.alive = ws, True


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, hub, web_dir):
        super().__init__(addr, Handler)
        self.hub, self.web_dir = hub, Path(web_dir)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ircam-web/" + __version__
    protocol_version = "HTTP/1.1"       # browsers reject a WebSocket upgrade answered as 1.0

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    def _send(self, code, ctype, body, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ws":
            return self._websocket()
        if path == "/api/status":
            body = json.dumps({**self.server.hub.status, "web": __version__,
                               "browsers": len(self.server.hub.clients)}).encode()
            return self._send(200, "application/json", body)
        if path in STATIC:
            name, ctype = STATIC[path]
            try:
                body = (self.server.web_dir / name).read_bytes()
            except OSError:
                return self._send(404, "text/plain", b"missing " + name.encode())
            return self._send(200, ctype, body, "no-cache")
        self._send(404, "text/plain", b"not found")

    def _websocket(self):
        key = (self.headers.get("Sec-WebSocket-Key") or "").strip()
        if not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
            return self._send(400, "text/plain", b"websocket expected")
        hub = self.server.hub
        if len(hub.clients) >= MAX_CLIENTS:
            return self._send(503, "text/plain", b"too many viewers")
        accept = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        sock = self.connection
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ws = WebSocket(sock)
        client = Client(ws)
        peer = self.client_address[0]
        log.info("browser connected: %s", peer)
        hub.join(client)
        sender = threading.Thread(target=self._pump, args=(hub, client), daemon=True)
        sender.start()
        try:
            while client.alive:
                data = ws.recv()
                if data is None:
                    break
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("cmd") == "set_range":
                    hub.command({"cmd": "set_range", "high": bool(msg.get("high")), "id": "range"})
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            client.alive = False
            hub.leave(client)
            with hub.cond:
                hub.cond.notify_all()
            log.info("browser gone: %s", peer)

    @staticmethod
    def _pump(hub, client):
        """Send the newest frame and any new status; a slow phone simply skips frames."""
        ws = client.ws
        sent_seq = sent_status = -1
        try:
            while client.alive and hub.alive:
                with hub.cond:
                    hub.cond.wait_for(lambda: not client.alive or not hub.alive or hub.status_seq != sent_status
                                      or (hub.frame and hub.frame[0] != sent_seq), 1.0)
                    frame, st, st_seq = hub.frame, hub.status, hub.status_seq
                if st_seq != sent_status:
                    sent_status = st_seq
                    ws.send(json.dumps({"type": "status", **st, "web": __version__}))
                if frame and frame[0] != sent_seq:
                    sent_seq = frame[0]
                    ws.send(frame[1])
        except OSError:
            pass
        client.alive = False
        hangup(ws.sock)


def main():
    ap = argparse.ArgumentParser(description="Browser viewer for the wireless P2 Pro bridge.")
    ap.add_argument("--tap", default=TAP_PATH, help="the bridge's local tap socket")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=WEB_PORT)
    ap.add_argument("--web-dir", default=str(here / "web"))
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(message)s" if os.environ.get("INVOCATION_ID")
                        else "%(asctime)s %(levelname)s %(message)s")
    hub = Hub(a.tap)
    srv = Server((a.bind, a.port), hub, a.web_dir)
    log.info("browser viewer on http://%s:%d/", a.bind, a.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()


if __name__ == "__main__":
    main()
