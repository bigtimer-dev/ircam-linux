"""Image processing: gain control, sharpening, interpolation and palette handling.

Port of RMH_ImageProcessing_Library.cpp (vectorised with numpy).
"""
import numpy as np

RES14 = 16383

_K3X = np.array([-1, 0, 1, -1, 0, 1, -1, 0, 1], dtype=np.float64)
_K3Y = np.array([-1, -1, -1, 0, 0, 0, 1, 1, 1], dtype=np.float64)


def agc_linear(values, max_in, min_in, max_out=RES14, min_out=0):
    """Linear automatic gain control (both the Raw and the Temp variant).

    Values are clipped to [min_in, max_in] (the Temp variant clips, the Raw one is fed
    the frame's own max/min so clipping is a no-op) and mapped linearly to
    [min_out, max_out]. Returns uint16 grey levels where 16383 is the hottest.
    """
    if max_in == min_in:
        return np.full(np.shape(values), min_out, dtype=np.uint16)
    scale = (min_out - max_out) / (min_in - max_in)
    off = -scale * max_in + max_out
    v = np.clip(values, min(min_in, max_in), max(min_in, max_in))
    return np.clip(scale * v + off, 0, 65535).astype(np.uint16)


def unsharp_kernel(sigma):
    """RMH_ImageProcessing_GenerateUnsharpKernelMask (3x3)."""
    g = (1 / (2 * np.pi * sigma ** 2)) * np.exp(-(_K3X ** 2 + _K3Y ** 2) / (2 * sigma ** 2))
    # the original rescales with the running minimum; the result is normalised later anyway
    return (g / g.min()).reshape(3, 3)


def unsharp_sharpen(img, kernel, strength, output_mask=False, resolution=RES14):
    """RMH_ImageProcessing_2DUnsharpMaskKernelImageSharpening (edge pixels clamped)."""
    f = img.astype(np.float64)
    p = np.pad(f, 1, mode="edge")
    h, w = f.shape
    blur = np.zeros_like(f)
    for dy in range(3):
        for dx in range(3):
            blur += kernel[dy, dx] * p[dy:dy + h, dx:dx + w]
    blur /= kernel.sum()
    mask = (f - blur) * strength
    out = mask if output_mask else f + mask
    if output_mask:
        # the mask is centred on zero; lift it so it is visible as an image (focus assist)
        out = np.abs(out) * 4
    return np.clip(out, 0, resolution).astype(np.uint16)


def bilinear(img, factor):
    """RMH_ImageProcessing_2DBilinearInterpolation, corner-aligned like the original."""
    h, w = img.shape
    nh, nw = h * factor, w * factor
    ys = np.linspace(0, h - 1, nh)
    xs = np.linspace(0, w - 1, nw)
    y0 = np.floor(ys).astype(int)
    x0 = np.floor(xs).astype(int)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    wy = (ys - y0)[:, None]
    wx = (xs - x0)[None, :]
    f = img.astype(np.float64)
    top = f[y0][:, x0] * (1 - wx) + f[y0][:, x1] * wx
    bot = f[y1][:, x0] * (1 - wx) + f[y1][:, x1] * wx
    return (top * (1 - wy) + bot * wy).astype(np.uint16)


def format_palette(main, main_invert, back, back_invert, lo, hi, adapt_full):
    """RMH_ImageProcessing_FormatColorPaletteRangeInsideBackgroundPalette.

    main/back: (16384, 3) uint8 palettes, index 0 = coldest.
    lo/hi: grey-level window (0..16383) the main palette is shown in; outside it the
    background palette is used. adapt_full squeezes the whole main palette into the window.
    Returns a (16384, 3) palette already including inversion.
    """
    m = main[::-1] if main_invert else main
    b = back[::-1] if back_invert else back
    lo = int(max(0, min(RES14, lo)))
    hi = int(max(0, min(RES14, hi)))
    if hi < lo:
        lo, hi = hi, lo
    out = b.copy()
    idx = np.arange(lo, hi + 1)
    if adapt_full and hi > lo:
        src = ((idx - lo) * (RES14 / (hi - lo))).astype(int)
        out[lo:hi + 1] = m[np.clip(src, 0, RES14)]
    else:
        out[lo:hi + 1] = m[lo:hi + 1]
    return out


def apply_palette(grey, palette):
    """Map uint16 grey levels (0..16383) to RGB uint8."""
    return palette[np.clip(grey, 0, RES14)]


def apply_dual_palette(grey, main_pal, dual_pal, rect):
    """RMH_ImageProcessing_ApplyOverlayedPaletteToGrayScaleImageData. rect = (x, y, w, h)."""
    rgb = apply_palette(grey, main_pal)
    x, y, w, h = [int(round(v)) for v in rect]
    H, W = grey.shape
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 > x0 and y1 > y0:
        rgb[y0:y1, x0:x1] = apply_palette(grey[y0:y1, x0:x1], dual_pal)
    return rgb


def histogram(values, bins, lo, hi):
    if hi <= lo:
        hi = lo + 1e-6
    h, _ = np.histogram(np.asarray(values, dtype=np.float64).ravel(), bins=bins, range=(lo, hi))
    return h


def line_pixels(x0, y0, x1, y1):
    """Integer pixel coordinates along a line (DDA), like the movable temperature lines."""
    n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    xs = np.rint(np.linspace(x0, x1, n)).astype(int)
    ys = np.rint(np.linspace(y0, y1, n)).astype(int)
    return xs, ys
