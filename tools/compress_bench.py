#!/usr/bin/env python3
"""Compare lossless codecs for the wireless P2 Pro stream on captured temperature halves.

    python3 tools/compress_bench.py frames.npy [more.npy ...]

Each row band is compressed independently (so a lost UDP packet only loses its band).
Reports the average bytes per frame, the bitrate at 25 fps, the share of bands whose
compressed size exceeds the 1376 B packet payload, and the encode speed.
"""
import sys
import time
import zlib

import numpy as np

try:
    import lz4.block as lz4b
except ImportError:
    lz4b = None
try:
    from compression import zstd
except ImportError:
    zstd = None

PAYLOAD = 1400 - 24


def pred_none(b):
    return b


def pred_hdelta(b):
    d = b.astype(np.int32)
    d[:, 1:] -= b[:, :-1]
    return (d & 0xFFFF).astype("<u2")


def pred_med(b):
    """LOCO-I / JPEG-LS median predictor inside the band (first band row: left only)."""
    x = b.astype(np.int32)
    a = np.zeros_like(x); a[:, 1:] = x[:, :-1]                  # left
    c = np.zeros_like(x); c[1:, 1:] = x[:-1, :-1]               # up-left
    u = np.zeros_like(x); u[1:] = x[:-1]                        # up
    u[0] = a[0]; c[0] = a[0]
    p = np.where(c >= np.maximum(a, u), np.minimum(a, u),
                 np.where(c <= np.minimum(a, u), np.maximum(a, u), a + u - c))
    return ((x - p) & 0xFFFF).astype("<u2")


def zigzag_planes(d):
    """Map residuals to small unsigned values and split hi/lo byte planes."""
    s = d.astype(np.int16).astype(np.int32)
    z = ((s << 1) ^ (s >> 31)).astype(np.uint16)
    return z.astype(">u2").view(np.uint8).reshape(-1, 2).T.tobytes()


CODECS = {"zlib-1": lambda x: zlib.compress(x, 1)}
if zstd:
    CODECS["zstd-1"] = lambda x: zstd.compress(x, 1)
if lz4b:
    CODECS["lz4"] = lambda x: lz4b.compress(x, store_size=False)
PREDS = {"none": (pred_none, False), "hdelta": (pred_hdelta, False),
         "hdelta+planes": (pred_hdelta, True), "med+planes": (pred_med, True)}


def main():
    frames = np.concatenate([np.load(p) for p in sys.argv[1:]])
    n = len(frames)
    t = frames / 64 - 273.15
    print(f"{n} frames, {t.min():.1f}..{t.max():.1f} C, raw {frames[0].nbytes} B/frame = "
          f"{frames[0].nbytes * 25 * 8 / 1e6:.1f} Mbit/s @25fps\n")
    print(f"{'predictor':15} {'codec':7} {'rows':>4} {'B/frame':>8} {'ratio':>6} {'Mbit/s':>7} "
          f"{'>pkt%':>6} {'enc ms/frame':>12}")
    for pname, (pred, planes) in PREDS.items():
        for cname, comp in CODECS.items():
            for rows in (4, 8, 16):
                total = over = bands = 0
                t0 = time.perf_counter()
                for f in frames:
                    for r in range(0, 192, rows):
                        d = pred(f[r:r + rows])
                        out = comp(zigzag_planes(d) if planes else d.tobytes())
                        total += len(out)
                        bands += 1
                        over += len(out) > PAYLOAD
                ms = (time.perf_counter() - t0) / n * 1000
                bpf = total / n
                print(f"{pname:15} {cname:7} {rows:4} {bpf:8.0f} {frames[0].nbytes / bpf:6.2f} "
                      f"{bpf * 25 * 8 / 1e6:7.2f} {100 * over / bands:6.1f} {ms:12.2f}")


if __name__ == "__main__":
    main()
