"""Extract palettes and the emissivity table from the OpenRMH-IRCAM C++ headers.

Usage: python3 tools/extract_resources.py <path-to-OpenRMH-IRCAM/src>
Writes ircam/data/palettes.npz and ircam/data/emissivity.json.
"""
import json
import re
import sys
from pathlib import Path

import numpy as np

src = Path(sys.argv[1])
out = Path(__file__).resolve().parent.parent / "ircam" / "data"

text = (src / "RMH_CustomColorPalette_Resources.h").read_text(errors="replace")
names = re.findall(r'"([^"]+)"', text.split("ColorPaletteNames", 1)[1].split("};", 1)[0])
arrays = {}
for m in re.finditer(r"RMH_CustomPalette_(\w+)\[3\]\[16384\]\s*=\s*\{(.*?)\};", text, re.S):
    nums = np.array([int(v) for v in re.findall(r"\d+", m.group(2))], dtype=np.uint32)
    if nums.size != 3 * 16384:
        raise SystemExit(f"palette {m.group(1)} has {nums.size} values")
    # stored as 16-bit per channel; keep 8-bit (display depth)
    arrays[m.group(1)] = (nums.reshape(3, 16384).T >> 8).astype(np.uint8)
missing = [n for n in names if n not in arrays]
if missing:
    raise SystemExit(f"missing palette data: {missing}")
np.savez_compressed(out / "palettes.npz", names=np.array(names), **{n: arrays[n] for n in names})
print(f"{len(names)} palettes")

t = (src / "RMH_EmissivityTable_Resources.h").read_text(errors="replace")
mat = re.findall(r'"([^"]+)"', t.split("EmissivityMaterialNames", 1)[1].split("};", 1)[0])
vals_block = t.split("EmissivityMaterialNames", 1)[1].split("};", 1)[1]
vals = [float(v) for v in re.findall(r"[-+]?\d*\.\d+|\d+", vals_block.split("{", 1)[1].split("}", 1)[0])]
if len(mat) != len(vals):
    raise SystemExit(f"emissivity mismatch {len(mat)} names vs {len(vals)} values")
(out / "emissivity.json").write_text(json.dumps([[n, v] for n, v in zip(mat, vals)], indent=1))
print(f"{len(mat)} emissivity entries")
