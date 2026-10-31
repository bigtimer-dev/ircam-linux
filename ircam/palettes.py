"""Colour palettes: the 52 built-in palettes from the original program plus palettes the
user creates in the palette creator (stored as colour stops in the config directory)."""
import json
from functools import lru_cache
from pathlib import Path

import numpy as np

from .config import config_dir

_DATA = Path(__file__).resolve().parent / "data" / "palettes.npz"
SIZE = 16384


@lru_cache(maxsize=1)
def _builtin():
    z = np.load(_DATA)
    names = [str(n) for n in z["names"]]
    return names, {n: z[n] for n in names}


def custom_dir():
    d = config_dir() / "palettes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stops_to_palette(stops):
    """stops: list of (position 0..1, (r, g, b) 0..255). Linear interpolation."""
    stops = sorted(stops, key=lambda s: s[0])
    pos = np.array([s[0] for s in stops], dtype=np.float64)
    cols = np.array([s[1] for s in stops], dtype=np.float64)
    x = np.linspace(0, 1, SIZE)
    out = np.stack([np.interp(x, pos, cols[:, c]) for c in range(3)], axis=1)
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def custom_palettes():
    res = {}
    for f in sorted(custom_dir().glob("*.json")):
        try:
            d = json.loads(f.read_text())
            res[d["name"]] = stops_to_palette(d["stops"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return res


def save_custom(name, stops):
    safe = "".join(c for c in name if c.isalnum() or c in " -_").strip() or "Custom"
    (custom_dir() / f"{safe}.json").write_text(json.dumps({"name": name, "stops": stops}, indent=1))


def delete_custom(name):
    for f in custom_dir().glob("*.json"):
        try:
            if json.loads(f.read_text()).get("name") == name:
                f.unlink()
        except (OSError, ValueError):
            pass


def names():
    b, _ = _builtin()
    return b + [n for n in custom_palettes() if n not in b]


def get(name):
    _, pals = _builtin()
    if name in pals:
        return pals[name]
    c = custom_palettes()
    if name in c:
        return c[name]
    return pals["Iron"]


def builtin_stops(name, n=8):
    """Sample a palette into editable stops (used to start the creator from an existing one)."""
    p = get(name)
    idx = np.linspace(0, SIZE - 1, n).astype(int)
    return [(float(i / (SIZE - 1)), tuple(int(v) for v in p[i])) for i in idx]
