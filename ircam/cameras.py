"""Supported camera models and their protocol "pools".

Ported from RMH_SupportedIRCameras_Resources.h. On Windows the original program finds a
camera by its DirectShow friendly name; on Linux the V4L2 card name plays the same role,
and USB vendor/product IDs are used as an extra hint when the name is the generic
"USB Camera".
"""
from dataclasses import dataclass, field

POOL_1 = 1  # InfiRay T2/T3 family, HT-301 - raw mode switched with zoom 0x8004, LUT maths
POOL_2 = 2  # P2/P2 Pro/TC001/... - 256x384 frame, lower half radiometric (K*64)
POOL_3 = 3  # InfiRay "V2" models - raw mode zoom 0x8005, own NUC, LUT maths
POOL_4 = 4  # Thermal Master P2 - 256x386 frame
POOL_5 = 5  # Thermal Master P3 - vendor USB protocol, same frame layout as pool 4
POOL_6 = 6  # Thermal Master THOR001 - "H264"-labelled raw 16-bit stream (K*10)

SNAPSHOT_ANALYSIS = "Snapshot Analysis Mode"
RECORDING_ANALYSIS = "Recording Analysis Mode"
DEMO_CAMERA = "Demo Camera (Simulated P2 Pro)"
WIRELESS_P2PRO = "InfiRay P2 Pro (Wireless)"

# Timings (ms), RMH_SupportedIRCameras_Resources.h
SHUTTER_CLOSE_MS = 500
SHUTTER_OPEN_MS = 850
POOL1_RANGE_SWITCH_MS = 500
POOL3_READY_MS = 4000
POOL3_RANGE_SWITCH_MS = 5000
POOL4_RANGE_SWITCH_MS = 5000

# Default camera configuration
DEFAULT_TEMP_CORRECTION = 0.0
DEFAULT_AMBIENT = 25.1
DEFAULT_REFLECTED = 25.1
DEFAULT_HUMIDITY = 0.45
DEFAULT_EMISSIVITY = 0.98
DEFAULT_DISTANCE = 1.0

# Pool 1/3 vendor "zoom" register commands
POOL13_HIGH_RANGE = 0x8021
POOL13_LOW_RANGE = 0x8020
POOL13_NUC = 0x8000
POOL1_RAW_MODE = 0x8004
POOL3_RAW_MODE = 0x8005
REG_TEMP_CORR = 0x0000
REG_REFL_TEMP = 0x0004
REG_AMB_TEMP = 0x0008
REG_HUMIDITY = 0x000C
REG_EMISSIVITY = 0x0010
REG_DISTANCE = 0x0014


@dataclass(frozen=True)
class CameraModel:
    name: str
    pool: int
    device_names: tuple
    high_range: bool
    fps: float = 25.0
    usb_ids: tuple = field(default=())  # ((vid, pid), ...) used on Linux as a hint


MODELS = [
    CameraModel("InfiRay T2L", POOL_1, ("T2L-A4L", "T2L-A6L", "T2L-A8L", "T2L", "T2L-A4L_R", "T2L-A4L_A", "T2L-A4L_C"), False),
    CameraModel("InfiRay T2L V2", POOL_3, ("T2L-A4L", "T2L-A6L", "T2L-A8L", "T2L_V2", "T2L", "T2L_A2", "T2L-A4L_A2", "T2L-A4L_V2"), False),
    CameraModel("InfiRay T2-Search", POOL_1, ("T2-Search", "T2", "T2S"), False),
    CameraModel("InfiRay T2-Search V2", POOL_3, ("T2_V2", "T2-Search", "T2", "T2S", "T2_A2", "T2S_A2"), False),
    CameraModel("InfiRay T2S+", POOL_1, ("T2S+", "T2Sp", "T2S+_A", "T2S+_C", "T2S+_R", "T2SPro"), True),
    CameraModel("InfiRay T2S+ V2", POOL_3, ("T2S+_V2", "T2S+_A", "T2S+_A2", "T2S+", "T2Sp", "T2SPro"), True),
    CameraModel("InfiRay T2Pro And T2SPro", POOL_1, ("T2Pro", "T2+", "T2p", "T2P", "T2SPro"), False),
    CameraModel("InfiRay T2Pro And T2SPro V2", POOL_3, ("T2Pro_V2", "T2Pro_A1", "T2Pro_A2", "T2Pro", "T2pro", "T2P", "T2SPro"), False),
    CameraModel("InfiRay T3-Search", POOL_1, ("T3-Search", "T3"), False),
    CameraModel("InfiRay T3S", POOL_1, ("Xtherm-T3S", "T3S-A68", "T3S-A13", "T3S", "T3S-A13_A", "T3S-A13_C", "T3S-A13_R"), True),
    CameraModel("InfiRay T3Pro", POOL_1, ("T3Pro-A13", "T3Pro-A68", "T3Pro", "T3Pro-A13_C", "T3Pro-A13_A", "T3Pro-A13_R"), True),
    CameraModel("InfiRay P2", POOL_2, ("USB Camera", "Camera"), True, usb_ids=((0x0BDA, 0x5830),)),
    CameraModel("InfiRay Or Thermal Master P2Pro", POOL_2, ("USB Camera", "Camera"), True, usb_ids=((0x0BDA, 0x5830),)),
    CameraModel("InfiRay DV-DL13", POOL_1, ("VirtualBox Webcam - DV-DL13", "DV-DL13"), True),
    CameraModel("InfiRay S0 Series", POOL_1, ("S0-90W", "S0-40", "S0-68", "S0-90"), True),
    CameraModel("InfiRay Tiny1-C", POOL_2, ("USB Camera", "Tiny1C"), True, usb_ids=((0x0BDA, 0x5830),)),
    CameraModel("Thermal Master P2", POOL_4, ("Camera",), True),
    CameraModel("HTI HT-301", POOL_1, ("T3", "T3-317-13", "T3-317-68", "HT-301"), True),
    CameraModel("UNI-T UTi260M", POOL_2, ("USB Camera",), True),
    CameraModel("TOPDON TC001 or TS001", POOL_2, ("USB Camera", "TC001"), True, usb_ids=((0x0BDA, 0x5830),)),
    CameraModel("TOPDON TC002 or TC003", POOL_2, ("USB Camera", "TC002", "TC003"), True),
    CameraModel("Victor 328B", POOL_2, ("USB Camera",), True),
    CameraModel("LODESTAR L2", POOL_2, ("USB Camera",), True),
    CameraModel("Thermal Master P3", POOL_5, ("P3",), True, usb_ids=((0x3474, 0x45A2),)),
    CameraModel("Thermal Master THOR001", POOL_6, ("THOR001",), False, usb_ids=((0x1D6B, 0x1102),)),
]

MODEL_BY_NAME = {m.name: m for m in MODELS}


def source_names():
    """List shown in the camera drop-down: the two analysis modes, a demo source, then
    the cameras sorted alphabetically (same order as RMH_BuildCameraDisplayOrder), with
    the wireless P2 Pro bridge listed next to the wired P2 Pro."""
    return [SNAPSHOT_ANALYSIS, RECORDING_ANALYSIS, DEMO_CAMERA] + sorted(
        [m.name for m in MODELS] + [WIRELESS_P2PRO], key=str.lower)


# Frame geometry per pool, RMH_SupportedIRCameras_Resources.h
POOL2_SIZE = (256, 384)
POOL2_HEIGHT_OFFSET = 192
POOL4_SIZE = (256, 386)
POOL4_HEIGHT_OFFSET = 194
POOL5_WIDTH_OFFSET = 6
POOL6_SIZE = (256, 192)
POOL6_WIDTH_OFFSET = 5
POOL6_RAW_BYTES = 98314
ASPECT = 4.0 / 3.0


def metadata_rows(width, height):
    """Rows below the image that carry metadata (FrameMetadataSize)."""
    return int(round(height - width / ASPECT))
