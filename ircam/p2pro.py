"""InfiRay P2 Pro vendor command channel (USB vendor control transfers to "mailboxes").

The Windows program has no range switching for pool 2 cameras. On Linux the P2 Pro's
gain (temperature range) can be selected with the camera's own vendor protocol, which
works alongside the uvcvideo driver because vendor requests are not tied to an interface.
Protocol as implemented by p2pro-rs (p2pro-hw crate).
"""
import time

from .usb import Device, USBError

VID, PID = 0x0BDA, 0x5830

REQ_TYPE_OUT, REQ_TYPE_IN = 0x41, 0xC1
REQ_OUT, REQ_IN, REQ_VALUE = 0x45, 0x44, 0x0078
MB_STATUS, MB_HEADER_B, MB_DATA, MB_LONG_RESULT = 0x0200, 0x9D00, 0x1D08, 0x1D10
STATUS_BUSY, STATUS_ERROR = 0x03, 0xFC
SET_FLAG = 0x4000
CMD_TPD = 0x8514
TPD_GAIN_SELECT = 5


class P2ProControl:
    def __init__(self):
        self.dev = Device(VID, PID)

    def _out(self, mailbox, data):
        self.dev.control_out(REQ_TYPE_OUT, REQ_OUT, REQ_VALUE, mailbox, data)

    def _wait_ready(self, timeout=5.0):
        end = time.monotonic() + timeout
        while True:
            st = self.dev.control_in(REQ_TYPE_IN, REQ_IN, REQ_VALUE, MB_STATUS, 1)[0]
            if not st & STATUS_BUSY:
                return
            if st & STATUS_ERROR:
                raise USBError(f"camera reported error status 0x{st:02x}")
            if time.monotonic() > end:
                raise USBError("timeout waiting for camera")
            time.sleep(0.01)

    @staticmethod
    def _long_header(code, p1, p2):
        return code.to_bytes(2, "little") + p1.to_bytes(2, "big") + p2.to_bytes(4, "big")

    def _long_write(self, code, p1, p2, p3=0, p4=0):
        self._out(MB_HEADER_B, self._long_header(code, p1, p2))
        self._out(MB_DATA, p3.to_bytes(4, "big") + p4.to_bytes(4, "big"))
        self._wait_ready()

    def _long_read(self, code, p1, p2, length):
        self._out(MB_HEADER_B, self._long_header(code, p1, p2))
        self._out(MB_DATA, (0).to_bytes(4, "big") + length.to_bytes(4, "big"))
        self._wait_ready()
        data = self.dev.control_in(REQ_TYPE_IN, REQ_IN, REQ_VALUE, MB_LONG_RESULT, length)
        self._wait_ready()
        return data

    def high_gain(self):
        return int.from_bytes(self._long_read(CMD_TPD, TPD_GAIN_SELECT, 0, 2), "big") != 0

    def set_high_gain(self, high):
        """High gain = low temperature range (about -20..150 C); low gain = high range."""
        self._long_write(CMD_TPD | SET_FLAG, TPD_GAIN_SELECT, 1 if high else 0)

    def close(self):
        self.dev.close()
