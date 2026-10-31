"""Secondary windows: 3D surface plot, statistics, emissivity table, palette creator."""
import json
import math
from datetime import datetime
from pathlib import Path

import cairo
import numpy as np
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import palettes
from .common import add_action, fmt_t, label, palette_strip, popover_menu, rgb_surface

DATA = Path(__file__).resolve().parent.parent / "data"


# --------------------------------------------------------------------- 3D surface plot
class SurfaceView(Gtk.DrawingArea):
    """Thermal frame as a 3D surface (RMH_OpenGL_3DSurfacePlot.h), drawn with cairo."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_content_width(720)
        self.set_content_height(520)
        self.set_draw_func(self._draw)
        self.reset_view()
        for button, kind in ((1, "rot_xz"), (2, "pan"), (3, "rot_y")):
            d = Gtk.GestureDrag(button=button)
            d.connect("drag-begin", lambda g, x, y, k=kind: self._begin(k))
            d.connect("drag-update", lambda g, dx, dy, k=kind: self._update(k, dx, dy))
            self.add_controller(d)
        sc = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        sc.connect("scroll", self._scroll)
        self.add_controller(sc)

    def reset_view(self):
        self.ax, self.az, self.ay = 0.95, -0.6, 0.0
        self.pan = [0.0, 0.0]
        self.zoom = 1.0
        self.queue_draw()

    def _begin(self, kind):
        self._start = (self.ax, self.az, self.ay, list(self.pan))

    def _update(self, kind, dx, dy):
        ax, az, ay, pan = self._start
        if kind == "rot_xz":
            self.az = az + dx * 0.01
            self.ax = min(1.55, max(0.0, ax + dy * 0.01))
        elif kind == "rot_y":
            self.ay = ay + dx * 0.01
        else:
            self.pan = [pan[0] + dx, pan[1] + dy]
        self.queue_draw()

    def _scroll(self, c, dx, dy):
        self.zoom = min(5.0, max(0.2, self.zoom * (0.9 if dy > 0 else 1.1)))
        self.queue_draw()
        return True

    def _draw(self, area, cr, w, h):
        self.render(cr, w, h, self.app.engine.latest)

    def render(self, cr, w, h, r):
        cr.set_source_rgb(0.07, 0.07, 0.08)
        cr.paint()
        if r is None:
            return
        s = self.app.settings
        t = r.temps
        step = max(1, int(math.ceil(max(t.shape) / 80)))
        g = t[::step, ::step]
        gh, gw = g.shape
        lo, hi = r.bar_lo, r.bar_hi
        span = max(hi - lo, 1e-6)
        zn = np.clip((g - lo) / span, 0, 1)
        ys, xs = np.mgrid[0:gh, 0:gw].astype(np.float64)
        aspect = gh / gw
        X = xs / max(gw - 1, 1) - 0.5
        Y = (ys / max(gh - 1, 1) - 0.5) * aspect
        Z = zn * 0.35 * s["surface_z_scale"]
        # rotate about the vertical axis (az), the view tilt (ax) and roll (ay)
        ca, sa = math.cos(self.az), math.sin(self.az)
        x1 = X * ca - Y * sa
        y1 = X * sa + Y * ca
        cx, sx = math.cos(self.ax), math.sin(self.ax)
        y2 = y1 * cx - Z * sx
        z2 = y1 * sx + Z * cx
        cy, sy = math.cos(self.ay), math.sin(self.ay)
        x3 = x1 * cy - y2 * sy
        y3 = x1 * sy + y2 * cy
        scale = min(w, h) * 0.95 * self.zoom
        px = w / 2 + self.pan[0] + x3 * scale
        py = h / 2 + self.pan[1] + y3 * scale
        depth = z2
        cols = r.palette[np.clip((((g - lo) / span) * (len(r.palette) - 1)).astype(int), 0, len(r.palette) - 1)]
        mode = s["surface_mode"]
        cr.set_line_width(0.6)
        if mode == "points":
            ps = s["surface_point_size"]
            order = np.argsort(depth, axis=None)  # far (low z) first
            for k in order:
                j, i = divmod(int(k), gw)
                c = cols[j, i]
                cr.set_source_rgb(c[0] / 255, c[1] / 255, c[2] / 255)
                cr.rectangle(px[j, i] - ps / 2, py[j, i] - ps / 2, ps, ps)
                cr.fill()
        else:
            qd = (depth[:-1, :-1] + depth[1:, 1:] + depth[:-1, 1:] + depth[1:, :-1]) / 4
            order = np.argsort(qd, axis=None)
            qw = gw - 1
            for k in order:
                j, i = divmod(int(k), qw)
                cr.move_to(px[j, i], py[j, i])
                cr.line_to(px[j, i + 1], py[j, i + 1])
                cr.line_to(px[j + 1, i + 1], py[j + 1, i + 1])
                cr.line_to(px[j + 1, i], py[j + 1, i])
                cr.close_path()
                c = cols[j, i]
                cr.set_source_rgb(c[0] / 255, c[1] / 255, c[2] / 255)
                if mode == "fill":
                    cr.fill_preserve()
                    cr.set_source_rgba(0, 0, 0, 0.15)
                    cr.stroke()
                else:
                    cr.stroke()
        label(cr, 8, 8, f"MAX {fmt_t(r.tmax, r.unit)}   MIN {fmt_t(r.tmin, r.unit)}", "#ffffff", size=12)
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.set_font_size(10)
        cr.move_to(8, h - 8)
        cr.show_text("Left drag: rotate X/Z · Right drag: rotate Y · Middle drag: pan · Wheel: zoom")


class SurfaceWindow(Adw.Window):
    def __init__(self, app):
        super().__init__(title="3D Surface Plot", default_width=820, default_height=640)
        self.app = app
        self.view = SurfaceView(app)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        s = app.settings
        mode = Gtk.DropDown.new_from_strings(["Fill", "Points", "Lines"])
        mode.set_selected(["fill", "points", "lines"].index(s["surface_mode"]))
        mode.connect("notify::selected", lambda d, p: (s.__setitem__("surface_mode", ["fill", "points", "lines"][d.get_selected()]), self.view.queue_draw()))
        mode.set_tooltip_text("Surface rendering")
        hb.pack_start(mode)
        zs = Gtk.SpinButton.new_with_range(0.1, 5.0, 0.1)
        zs.set_value(s["surface_z_scale"])
        zs.set_tooltip_text("Z-axis scale")
        zs.connect("value-changed", lambda b: (s.__setitem__("surface_z_scale", b.get_value()), self.view.queue_draw()))
        hb.pack_start(zs)
        ps = Gtk.SpinButton.new_with_range(1, 10, 0.5)
        ps.set_value(s["surface_point_size"])
        ps.set_tooltip_text("Point size")
        ps.connect("value-changed", lambda b: s.__setitem__("surface_point_size", b.get_value()))
        hb.pack_start(ps)
        reset = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reset view")
        reset.connect("clicked", lambda b: self.view.reset_view())
        hb.pack_end(reset)
        snap = Gtk.Button(icon_name="camera-photo-symbolic", tooltip_text="Save plot snapshot")
        snap.connect("clicked", lambda b: self.snapshot())
        hb.pack_end(snap)
        tv.add_top_bar(hb)
        tv.set_content(self.view)
        self.set_content(tv)
        self.timer = GLib.timeout_add(100, self._tick)
        self.connect("close-request", self._close)

    def _tick(self):
        self.view.queue_draw()
        return True

    def _close(self, *a):
        GLib.source_remove(self.timer)
        self.app.surface_win = None
        return False

    def snapshot(self):
        w, h = max(1, self.view.get_width()), max(1, self.view.get_height())
        surf = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
        self.view.render(cairo.Context(surf), w, h, self.app.engine.latest)
        from ..engine import timestamp
        path = Path(self.app.settings["snapshot_dir"]) / f"3DPlotSnapshot_{timestamp()}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        surf.write_to_png(str(path))
        self.app.log(f"3D plot snapshot saved: {path}", "success")


# --------------------------------------------------------------------- statistics
class StatisticsWindow(Adw.Window):
    """Live view statistics (CalLiveViewStatisticsData)."""

    def __init__(self, app):
        super().__init__(title="Statistics", default_width=380, default_height=560)
        self.app = app
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        reset = Gtk.Button(label="Reset Peaks")
        reset.connect("clicked", lambda b: self._reset())
        hb.pack_start(reset)
        tv.add_top_bar(hb)
        self.grid = Gtk.Grid(column_spacing=18, row_spacing=4, margin_start=16, margin_end=16, margin_top=10, margin_bottom=16)
        self.rows = {}
        names = ["Camera", "Frame size", "Frame rate", "Frames", "Run state", "Maximum temp", "Minimum temp",
                 "Average temp", "Center temp", "Peak maximum", "Peak minimum", "Temp span",
                 "Color bar range", "Color bar range usage", "Calibration FPA temp", "Current FPA temp",
                 "Camera temp drift", "Temp range", "Emissivity", "Ambient / reflected", "Humidity",
                 "Distance", "Temp correction", "Data logging", "Recording", "Active alarms"]
        for i, n in enumerate(names):
            k = Gtk.Label(label=n, xalign=0)
            k.add_css_class("dim-label")
            v = Gtk.Label(label="—", xalign=0, selectable=True)
            v.add_css_class("monospace")
            self.grid.attach(k, 0, i, 1, 1)
            self.grid.attach(v, 1, i, 1, 1)
            self.rows[n] = v
        sw = Gtk.ScrolledWindow(child=self.grid, vexpand=True)
        tv.set_content(sw)
        self.set_content(tv)
        self.timer = GLib.timeout_add(250, self._tick)
        self.connect("close-request", self._close)

    def _reset(self):
        self.app.engine.peak_max, self.app.engine.peak_min = -1e9, 1e9

    def _close(self, *a):
        GLib.source_remove(self.timer)
        self.app.stats_win = None
        return False

    def _tick(self):
        eng = self.app.engine
        r = eng.latest
        src = eng.source
        set_ = lambda k, v: self.rows[k].set_label(str(v))
        if r is None or src is None:
            set_("Camera", "not connected")
            return True
        c = src.cam
        u = r.unit
        us, uo, _ = eng.unit()
        set_("Camera", src.name)
        set_("Frame size", f"{c.width} × {c.image_height} (pool {c.pool})")
        set_("Frame rate", f"{src.fps():.1f} fps")
        set_("Frames", eng.frames)
        set_("Run state", "running" if eng.running else "stopped")
        set_("Maximum temp", fmt_t(r.tmax, u, 2))
        set_("Minimum temp", fmt_t(r.tmin, u, 2))
        set_("Average temp", fmt_t(r.tavg, u, 2))
        set_("Center temp", fmt_t(r.tcenter, u, 2))
        set_("Peak maximum", fmt_t(eng.peak_max, u, 2))
        set_("Peak minimum", fmt_t(eng.peak_min, u, 2))
        set_("Temp span", fmt_t(r.tmax - r.tmin, u.replace("°", "Δ°") if u != "K" else "K", 2))
        set_("Color bar range", f"{r.bar_lo:.1f} … {r.bar_hi:.1f}{u}  ({eng.bar_mode})")
        usage = (min(r.tmax, r.bar_hi) - max(r.tmin, r.bar_lo)) / max(r.bar_hi - r.bar_lo, 1e-9) * 100
        set_("Color bar range usage", f"{max(0.0, usage):.1f} %")
        set_("Calibration FPA temp", fmt_t(eng.cal_fpa * us + uo, u, 2))
        set_("Current FPA temp", fmt_t(c.temp_fpa * us + uo, u, 2))
        set_("Camera temp drift", fmt_t(eng.drift() * us, u, 2))
        set_("Temp range", "high" if eng.high_range else "low")
        set_("Emissivity", f"{c.emissivity:.3f}")
        set_("Ambient / reflected", f"{c.ambient * us + uo:.1f} / {c.reflected * us + uo:.1f}{u}")
        set_("Humidity", f"{c.humidity * 100:.0f} %")
        set_("Distance", f"{c.distance:.1f} m")
        set_("Temp correction", f"{c.temp_correction:+.2f} °C")
        set_("Data logging", f"on, {eng.log_sample} samples" if eng.logging else "off")
        set_("Recording", f"on, {eng.rec_frames} frames" if eng.recording else "off")
        on = [str(i + 1) for i, a in enumerate(r.alarms) if a]
        set_("Active alarms", ", ".join(on) or "none")
        return True


# --------------------------------------------------------------------- emissivity table
class EmissivityWindow(Adw.Window):
    def __init__(self, app):
        super().__init__(title="Emissivity Table", default_width=440, default_height=620)
        self.app = app
        items = json.loads((DATA / "emissivity.json").read_text())
        store = Gtk.StringList()
        self.items = items
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        tv.add_top_bar(hb)
        search = Gtk.SearchEntry(placeholder_text="Search materials", margin_start=8, margin_end=8, margin_top=6, margin_bottom=6)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(search)
        for name, val in items:
            store.append(f"{name}\t{val}")
        filt = Gtk.CustomFilter.new(lambda it: search.get_text().lower() in it.get_string().lower())
        fm = Gtk.FilterListModel(model=store, filter=filt)
        search.connect("search-changed", lambda e: filt.changed(Gtk.FilterChange.DIFFERENT))
        sel = Gtk.SingleSelection(model=fm)
        cv = Gtk.ColumnView(model=sel, vexpand=True)
        for title, idx, expand in (("Material", 0, True), ("Emissivity", 1, False)):
            f = Gtk.SignalListItemFactory()
            f.connect("setup", lambda fac, li: li.set_child(Gtk.Label(xalign=0)))
            f.connect("bind", lambda fac, li, i=idx: li.get_child().set_label(li.get_item().get_string().split("\t")[i]))
            col = Gtk.ColumnViewColumn(title=title, factory=f)
            col.set_expand(expand)
            cv.append_column(col)
        cv.connect("activate", lambda v, pos: self._apply(fm.get_item(pos).get_string()))
        box.append(Gtk.ScrolledWindow(child=cv, vexpand=True))
        hint = Gtk.Label(label="Double-click a material to apply its emissivity.", margin_top=6, margin_bottom=6)
        hint.add_css_class("dim-label")
        box.append(hint)
        tv.set_content(box)
        self.set_content(tv)

    def _apply(self, s):
        name, val = s.split("\t")
        self.app.apply_emissivity(float(val), name)


# --------------------------------------------------------------------- palette creator
class PaletteCreator(Adw.Window):
    """Create custom colour palettes from colour stops (the Palette Creator)."""

    def __init__(self, app):
        super().__init__(title="Palette Creator", default_width=640, default_height=360)
        self.app = app
        self.stops = palettes.builtin_stops(app.settings["palette"], 6)
        self.sel = 0
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=14, margin_end=14, margin_top=10, margin_bottom=14)
        row = Gtk.Box(spacing=8)
        row.append(Gtk.Label(label="Start from"))
        self.base = Gtk.DropDown.new_from_strings(palettes.names())
        self.base.set_selected(max(0, palettes.names().index(app.settings["palette"]) if app.settings["palette"] in palettes.names() else 0))
        self.base.connect("notify::selected", self._load_base)
        row.append(self.base)
        row.append(Gtk.Label(label="Stops"))
        self.nstops = Gtk.SpinButton.new_with_range(2, 16, 1)
        self.nstops.set_value(6)
        self.nstops.connect("value-changed", self._load_base)
        row.append(self.nstops)
        box.append(row)
        self.area = Gtk.DrawingArea(content_height=90, hexpand=True)
        self.area.set_draw_func(self._draw)
        click = Gtk.GestureClick()
        click.connect("pressed", self._click)
        self.area.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-update", self._drag)
        self.area.add_controller(drag)
        box.append(self.area)
        row = Gtk.Box(spacing=8)
        self.color = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False))
        self.color.connect("notify::rgba", self._color_changed)
        row.append(Gtk.Label(label="Selected stop colour"))
        row.append(self.color)
        add = Gtk.Button(label="Add Stop")
        add.connect("clicked", self._add)
        rem = Gtk.Button(label="Remove Stop")
        rem.connect("clicked", self._remove)
        row.append(add)
        row.append(rem)
        box.append(row)
        hint = Gtk.Label(label="Click a stop to select it, drag to move it.", xalign=0)
        hint.add_css_class("dim-label")
        box.append(hint)
        row = Gtk.Box(spacing=8)
        self.name = Gtk.Entry(placeholder_text="Palette name", hexpand=True)
        row.append(self.name)
        save = Gtk.Button(label="Save Palette")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        row.append(save)
        delete = Gtk.Button(label="Delete Custom…")
        delete.connect("clicked", self._delete)
        row.append(delete)
        box.append(row)
        tv.set_content(box)
        self.set_content(tv)
        self._sync_color()

    def _load_base(self, *a):
        name = self.base.get_selected_item().get_string()
        self.stops = palettes.builtin_stops(name, int(self.nstops.get_value()))
        self.sel = 0
        self._sync_color()
        self.area.queue_draw()

    def _sync_color(self):
        c = self.stops[self.sel][1]
        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue, rgba.alpha = c[0] / 255, c[1] / 255, c[2] / 255, 1.0
        self._syncing = True
        self.color.set_rgba(rgba)
        self._syncing = False

    def _color_changed(self, *a):
        if getattr(self, "_syncing", False):
            return
        c = self.color.get_rgba()
        self.stops[self.sel] = (self.stops[self.sel][0], (int(c.red * 255), int(c.green * 255), int(c.blue * 255)))
        self.area.queue_draw()

    def _x(self, pos):
        return 10 + pos * (self.area.get_width() - 20)

    def _click(self, g, n, x, y):
        best = min(range(len(self.stops)), key=lambda i: abs(self._x(self.stops[i][0]) - x))
        self.sel = best
        self._drag_start = self.stops[best][0]
        self._sync_color()
        self.area.queue_draw()

    def _drag(self, g, dx, dy):
        if self.sel in (0, len(self.stops) - 1):
            return  # end stops stay at 0 and 1
        ok, sx, sy = g.get_start_point()
        pos = min(0.999, max(0.001, (sx + dx - 10) / max(1, self.area.get_width() - 20)))
        self.stops[self.sel] = (pos, self.stops[self.sel][1])
        self.area.queue_draw()

    def _add(self, b):
        st = sorted(self.stops, key=lambda s: s[0])
        gaps = [(st[i + 1][0] - st[i][0], i) for i in range(len(st) - 1)]
        _, i = max(gaps)
        pos = (st[i][0] + st[i + 1][0]) / 2
        pal = palettes.stops_to_palette(st)
        col = tuple(int(v) for v in pal[int(pos * (len(pal) - 1))])
        st.append((pos, col))
        self.stops = sorted(st, key=lambda s: s[0])
        self.sel = self.stops.index((pos, col))
        self.area.queue_draw()

    def _remove(self, b):
        if len(self.stops) > 2 and self.sel not in (0, len(self.stops) - 1):
            del self.stops[self.sel]
            self.sel = 0
            self._sync_color()
            self.area.queue_draw()

    def _draw(self, area, cr, w, h):
        cr.set_source_rgb(0.12, 0.12, 0.13)
        cr.paint()
        pal = palettes.stops_to_palette(self.stops)
        surf = palette_strip(pal, 512, vertical=False)
        cr.save()
        cr.rectangle(10, 10, w - 20, h - 40)
        cr.clip()
        cr.translate(10, 10)
        cr.scale((w - 20) / 512, h - 40)
        cr.set_source_surface(surf, 0, 0)
        cr.get_source().set_extend(cairo.EXTEND_PAD)
        cr.paint()
        cr.restore()
        for i, (pos, col) in enumerate(self.stops):
            x = self._x(pos)
            cr.move_to(x, h - 28)
            cr.line_to(x - 8, h - 12)
            cr.line_to(x + 8, h - 12)
            cr.close_path()
            cr.set_source_rgb(col[0] / 255, col[1] / 255, col[2] / 255)
            cr.fill_preserve()
            cr.set_source_rgb(1, 1, 1) if i == self.sel else cr.set_source_rgb(0.4, 0.4, 0.4)
            cr.set_line_width(2.5 if i == self.sel else 1)
            cr.stroke()

    def _save(self, b):
        name = self.name.get_text().strip()
        if not name:
            self.app.log("Enter a palette name first.", "warning")
            return
        palettes.save_custom(name, [(float(p), list(c)) for p, c in sorted(self.stops, key=lambda s: s[0])])
        self.app.reload_palettes(select=name)
        self.app.log(f"Custom palette '{name}' saved.", "success")

    def _delete(self, b):
        name = self.name.get_text().strip()
        if name in palettes.custom_palettes():
            palettes.delete_custom(name)
            self.app.reload_palettes()
            self.app.log(f"Custom palette '{name}' deleted.", "success")
        else:
            self.app.log("Type the name of a custom palette to delete it.", "warning")
