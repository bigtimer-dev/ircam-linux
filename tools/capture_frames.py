#!/usr/bin/env python3
"""Capture P2 Pro temperature halves (256x192 u16) to a .npy file for benchmarking.

    python3 tools/capture_frames.py out.npy --seconds 10
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ircam import cameras as C, v4l2  # noqa: E402
from ircam.sources import find_v4l2_device  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=10)
    a = ap.parse_args()
    model = next(m for m in C.MODELS if m.pool == C.POOL_2 and (0x0BDA, 0x5830) in (m.usb_ids or ()))
    dev, _ = find_v4l2_device(model)
    if not dev:
        sys.exit("P2 Pro not found")
    cam = v4l2.V4L2Camera(dev.path)
    cam.set_format(256, 384, v4l2.YUYV)
    cam.set_fps(25)
    cam.start()
    frames, t_end = [], time.monotonic() + a.seconds
    while time.monotonic() < t_end:
        raw = cam.read(1.0)
        if raw and len(raw) == 196608:
            frames.append(np.frombuffer(raw, "<u2")[256 * 192:].reshape(192, 256).copy())
    cam.close()
    arr = np.stack(frames)
    np.save(a.out, arr)
    t = arr / 64 - 273.15
    print(f"{len(arr)} frames ({len(arr) / a.seconds:.1f} fps), {t.min():.1f}..{t.max():.1f} C -> {a.out}")


if __name__ == "__main__":
    main()
