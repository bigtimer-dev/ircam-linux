"""Thermal Master P3 (USB 3474:45A2): vendor protocol over libusb.

Port of p3_winusb_camera.cpp. Interface 0 = vendor control, interface 1 = bulk video
(endpoint 0x81, active in alternate setting 1). Frames are 256 x 386 x 2 bytes, preceded
by two 12-byte packets that are discarded.
"""
import time

from .usb import Device, USBError

VID, PID = 0x3474, 0x45A2
EP_IN = 0x81
WIDTH, HEIGHT = 256, 386
FRAME_BYTES = WIDTH * HEIGHT * 2


def crc16_xmodem(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


class P3Camera:
    def __init__(self):
        self.dev = None
        self.capturing = False
        self._fps_t = []

    def open(self):
        self.dev = Device(VID, PID)
        self.dev.claim(0)
        self.dev.claim(1)

    def _command(self, op_hi, op_lo, param, resp_len=0, byte2=0x81):
        p = bytearray(18)
        p[0], p[1], p[2], p[3], p[4] = op_hi, op_lo, byte2, 0, param
        p[12:16] = resp_len.to_bytes(4, "little")
        c = crc16_xmodem(p[:16])
        p[16], p[17] = c & 0xFF, c >> 8
        self.dev.control_out(0x41, 0x20, 0, 0, bytes(p), 1000)
        # "ready" poll - can take ~0.7 s while the shutter moves
        self.dev.control_in(0xC1, 0x22, 0, 0, 1, 3000)
        data = b""
        if 0 < resp_len <= 64:
            data = self.dev.control_in(0xC1, 0x21, 0, 0, resp_len, 1000)
            try:
                self.dev.control_in(0xC1, 0x22, 0, 0, 1, 300)
            except USBError:
                pass
        return data

    def start(self):
        if self.capturing:
            return
        self.dev.set_alt(1, 1)
        try:
            self.dev.control_out(0x40, 0xEE, 0, 1, b"", 300)  # device "unlock"
        except USBError:
            pass
        last = None
        for _ in range(20):
            try:
                self._command(0x01, 0x2F, 0x00, 1)
                self.capturing = True
                return
            except USBError as e:
                last = e
                time.sleep(0.5)
        raise USBError(f"start-stream command failed: {last}")

    def stop(self):
        if self.dev and self.capturing:
            try:
                self.dev.set_alt(1, 0)
            except USBError:
                pass
        self.capturing = False

    def shutter(self):
        self._command(0x01, 0x36, 0x00, 0, byte2=0x43)

    def set_high_range(self, high):
        self._command(0x01, 0x2F, 0x00 if high else 0x01, 0, byte2=0x41)

    def read(self, timeout=1.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data = self.dev.bulk_read(EP_IN, FRAME_BYTES, 500)
            except USBError:
                continue
            if len(data) == FRAME_BYTES:
                now = time.monotonic()
                self._fps_t = [t for t in self._fps_t + [now] if now - t < 2.0]
                # the 6-pixel width offset reads 12 bytes past the end: duplicate the tail
                return data + data[-12:]
        return None

    def fps(self):
        t = self._fps_t
        return (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else 0.0

    def close(self):
        self.stop()
        if self.dev:
            self.dev.close()
            self.dev = None
