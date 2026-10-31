"""Shared drawing helpers."""
import math

import cairo
import numpy as np
from gi.repository import Gdk, Gio, GLib, Gtk


def rgb_surface(rgb):
    """numpy (h, w, 3) uint8 -> cairo RGB24 ImageSurface (keeps a reference to the data)."""
    h, w = rgb.shape[:2]
    a = np.empty((h, w, 4), np.uint8)
    a[..., 0] = rgb[..., 2]
    a[..., 1] = rgb[..., 1]
    a[..., 2] = rgb[..., 0]
    a[..., 3] = 255
    surf = cairo.ImageSurface.create_for_data(memoryview(a), cairo.FORMAT_RGB24, w, h, w * 4)
    return surf


def palette_strip(pal, n=256, vertical=True):
    """Palette (16384, 3) -> cairo surface, hot at the top when vertical."""
    idx = np.linspace(len(pal) - 1, 0, n).astype(int) if vertical else np.linspace(0, len(pal) - 1, n).astype(int)
    colors = pal[idx]
    arr = colors[:, None, :] if vertical else colors[None, :, :]
    return rgb_surface(np.ascontiguousarray(arr))


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def set_color(cr, hexcol, alpha=1.0):
    r, g, b = hex_rgb(hexcol)
    cr.set_source_rgba(r, g, b, alpha)


def fmt_t(v, unit, digits=1):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:.{digits}f}{unit}"


def label(cr, x, y, text, color="#ffffff", bg=True, size=12, anchor="left", bold=True, bounds=None):
    """Draw a text label with an optional dark background; keeps it inside bounds (w, h)."""
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                        cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    ext = cr.text_extents(text)
    fe = cr.font_extents()
    w, h = ext.x_advance + 6, fe[0] + fe[1] + 2
    if anchor == "right":
        x -= w
    elif anchor == "center":
        x -= w / 2
    if bounds:
        x = max(0, min(x, bounds[0] - w))
        y = max(0, min(y, bounds[1] - h))
    if bg:
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.rectangle(x, y, w, h)
        cr.fill()
    set_color(cr, color)
    cr.move_to(x + 3, y + fe[0] + 1)
    cr.show_text(text)
    return w, h


def popover_menu(widget, menu, x, y):
    pop = Gtk.PopoverMenu.new_from_model(menu)
    pop.set_parent(widget)
    pop.set_has_arrow(False)
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    pop.set_pointing_to(rect)
    pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
    pop.popup()
    return pop


def add_action(group, name, cb, state=None, param=None):
    if state is None:
        a = Gio.SimpleAction.new(name, GLib.VariantType.new(param) if param else None)
        a.connect("activate", lambda a, p: cb(p) if param else cb())
    else:
        a = Gio.SimpleAction.new_stateful(name, GLib.VariantType.new(param) if param else None, state)
        a.connect("change-state", lambda a, v: (a.set_state(v), cb(v)))
        if param is None:
            a.connect("activate", lambda a, p: a.change_state(GLib.Variant.new_boolean(not a.get_state().get_boolean())))
        else:
            a.connect("activate", lambda a, p: a.change_state(p))
    group.add_action(a)
    return a


def nice_ticks(lo, hi, n):
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, n)
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=raw)
    first = math.ceil(lo / step) * step
    out = []
    v = first
    while v <= hi + 1e-9:
        out.append(round(v, 10))
        v += step
    return out
