"""Small ctypes binding for libusb-1.0 (control and bulk transfers only)."""
import ctypes as ct
import ctypes.util

_lib = None


class USBError(OSError):
    pass


def lib():
    global _lib
    if _lib is None:
        name = ctypes.util.find_library("usb-1.0") or "libusb-1.0.so.0"
        _lib = ct.CDLL(name)
        _lib.libusb_init.argtypes = [ct.POINTER(ct.c_void_p)]
        _lib.libusb_open_device_with_vid_pid.restype = ct.c_void_p
        _lib.libusb_open_device_with_vid_pid.argtypes = [ct.c_void_p, ct.c_uint16, ct.c_uint16]
        _lib.libusb_close.argtypes = [ct.c_void_p]
        _lib.libusb_control_transfer.argtypes = [ct.c_void_p, ct.c_uint8, ct.c_uint8, ct.c_uint16,
                                                 ct.c_uint16, ct.c_void_p, ct.c_uint16, ct.c_uint]
        _lib.libusb_bulk_transfer.argtypes = [ct.c_void_p, ct.c_ubyte, ct.c_void_p, ct.c_int,
                                              ct.POINTER(ct.c_int), ct.c_uint]
        _lib.libusb_claim_interface.argtypes = [ct.c_void_p, ct.c_int]
        _lib.libusb_release_interface.argtypes = [ct.c_void_p, ct.c_int]
        _lib.libusb_set_interface_alt_setting.argtypes = [ct.c_void_p, ct.c_int, ct.c_int]
        _lib.libusb_set_auto_detach_kernel_driver.argtypes = [ct.c_void_p, ct.c_int]
        _lib.libusb_error_name.restype = ct.c_char_p
        _lib.libusb_error_name.argtypes = [ct.c_int]
    return _lib


_ctx = None


def _context():
    global _ctx
    if _ctx is None:
        c = ct.c_void_p()
        r = lib().libusb_init(ct.byref(c))
        if r != 0:
            raise USBError(f"libusb_init failed ({r})")
        _ctx = c
    return _ctx


def _err(r):
    return lib().libusb_error_name(r).decode()


class Device:
    def __init__(self, vid, pid):
        self.h = lib().libusb_open_device_with_vid_pid(_context(), vid, pid)
        if not self.h:
            raise USBError(f"USB device {vid:04x}:{pid:04x} not found or no permission")
        self.claimed = set()

    def control_out(self, request_type, request, value, index, data=b"", timeout_ms=1000):
        buf = ct.create_string_buffer(bytes(data), len(data)) if data else None
        r = lib().libusb_control_transfer(self.h, request_type, request, value, index, buf, len(data), timeout_ms)
        if r < 0:
            raise USBError(f"control OUT failed: {_err(r)}")
        return r

    def control_in(self, request_type, request, value, index, length, timeout_ms=1000):
        buf = ct.create_string_buffer(length)
        r = lib().libusb_control_transfer(self.h, request_type, request, value, index, buf, length, timeout_ms)
        if r < 0:
            raise USBError(f"control IN failed: {_err(r)}")
        return buf.raw[:r]

    def claim(self, iface):
        lib().libusb_set_auto_detach_kernel_driver(self.h, 1)
        r = lib().libusb_claim_interface(self.h, iface)
        if r < 0:
            raise USBError(f"claim interface {iface} failed: {_err(r)}")
        self.claimed.add(iface)

    def set_alt(self, iface, alt):
        r = lib().libusb_set_interface_alt_setting(self.h, iface, alt)
        if r < 0:
            raise USBError(f"set alt setting {iface}/{alt} failed: {_err(r)}")

    def bulk_read(self, ep, length, timeout_ms=500):
        buf = ct.create_string_buffer(length)
        got = ct.c_int(0)
        r = lib().libusb_bulk_transfer(self.h, ep, buf, length, ct.byref(got), timeout_ms)
        if r < 0 and got.value == 0:
            raise USBError(f"bulk read failed: {_err(r)}")
        return buf.raw[: got.value]

    def close(self):
        if self.h:
            for i in list(self.claimed):
                lib().libusb_release_interface(self.h, i)
            lib().libusb_close(self.h)
            self.h = None
