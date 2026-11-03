#!/usr/bin/env python3
"""Build the browser viewer's assets in bridge/web/ from the app's data:

- palettes.bin / palettes.json: the 52 built-in palettes, 256 RGB entries each (40 KB)
- icon-180.png / icon-512.png: home-screen icons

    python3 tools/make_web_assets.py
"""
import json
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "bridge" / "web"
FEATURED = ["Iron", "InfiRay", "Turbo", "Inferno", "Lava", "Gray", "Rain", "Magma",
            "Medical1", "Hot", "Jet", "Viridis"]
N = 256


def png(path, rgb):
    h, w, _ = rgb.shape
    raw = b"".join(b"\0" + rgb[y].tobytes() for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    path.write_bytes(data)


def icon(size, pal):
    """A warm blob on a cold background, coloured with the Iron palette."""
    yy, xx = np.mgrid[0:size, 0:size] / size
    t = (0.15 + 0.8 * np.exp(-((xx - 0.55) ** 2 + (yy - 0.45) ** 2) / 0.05)
         + 0.25 * np.exp(-((xx - 0.3) ** 2 + (yy - 0.72) ** 2) / 0.02))
    idx = np.clip(t / t.max() * (len(pal) - 1), 0, len(pal) - 1).astype(int)
    return pal[idx]


def main():
    z = np.load(ROOT / "ircam" / "data" / "palettes.npz")
    names = [str(n) for n in z["names"]]
    pick = np.linspace(0, 16383, N).round().astype(int)
    blob = b"".join(np.ascontiguousarray(z[n][pick], np.uint8).tobytes() for n in names)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "palettes.bin").write_bytes(blob)
    (OUT / "palettes.json").write_text(json.dumps(
        {"size": N, "names": names, "featured": [f for f in FEATURED if f in names]}, indent=1) + "\n")
    iron = z["Iron"][pick]
    for s in (180, 512):
        png(OUT / f"icon-{s}.png", icon(s, iron))
    print(f"wrote {OUT}: {len(names)} palettes, {len(blob)} bytes")


if __name__ == "__main__":
    main()
