"""Colour bar, histogram and 2D temperature plot widgets (cairo).

Counterparts of RMH_OpenGL_ColorBar.h, RMH_OpenGL_Histogram.h and RMH_OpenGL_2DPlot.h.
"""
import math

import cairo
import numpy as np
from gi.repository import Gio, GLib, Gtk

from .common import add_action, fmt_t, hex_rgb, label, nice_ticks, palette_strip, popover_menu, set_color

PAD_T, PAD_B = 22, 22


class ColorBar(Gtk.DrawingArea):
    BAR_X, BAR_W = 22, 26

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.set_content_width(118)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)
        self.drag_tag = None
        self.result = None

        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._menu)
        self.add_controller(click)
        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", lambda *a: setattr(self, "drag_tag", None))
        self.add_controller(drag)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda c, x, y: setattr(self, "mouse_y", y))
        self.add_controller(motion)
        self.mouse_y = 0

        g = Gio.SimpleActionGroup()
        s = app.settings
        add_action(g, "mode", self._set_mode, GLib.Variant.new_string("manual" if s["colorbar_manual"] else "auto"), "s")
        add_action(g, "fullrange", lambda v: self._set("adapt_full_palette", v.get_boolean()),
                   GLib.Variant.new_boolean(s["adapt_full_palette"]))
        add_action(g, "ticks", lambda v: self._set("colorbar_ticks", int(v.get_string())),
                   GLib.Variant.new_string(str(s["colorbar_ticks"])), "s")
        add_action(g, "step", lambda v: self._set("colorbar_wheel_step", float(v.get_string())),
                   GLib.Variant.new_string(str(float(s["colorbar_wheel_step"]))), "s")
        add_action(g, "center", lambda v: self._set("colorbar_center_arrow", v.get_boolean()),
                   GLib.Variant.new_boolean(s["colorbar_center_arrow"]))
        add_action(g, "rangelive", lambda v: self._set("palette_range_live", v.get_boolean()),
                   GLib.Variant.new_boolean(s["palette_range_live"]))
        add_action(g, "rangedual", lambda v: self._set("palette_range_dual", v.get_boolean()),
                   GLib.Variant.new_boolean(s["palette_range_dual"]))
        add_action(g, "setrange", lambda: app.dialog_colorbar_range())
        add_action(g, "resettags", self.reset_tags)
        self.actions = g
        self.insert_action_group("cb", g)

    def _set(self, key, val):
        self.app.settings[key] = val
        self.queue_draw()

    def _set_mode(self, v):
        mode = v.get_string()
        eng = self.app.engine
        if mode == "manual" and eng.latest and eng.bar_mode == "auto":
            self.app.settings["colorbar_manual_max"] = round(eng.latest.tmax, 1)
            self.app.settings["colorbar_manual_min"] = round(eng.latest.tmin, 1)
        eng.bar_mode = mode
        self.app.settings["colorbar_manual"] = mode != "auto"

    def sync_mode(self):
        self.actions.lookup_action("mode").set_state(
            GLib.Variant.new_string("manual" if self.app.engine.bar_mode != "auto" else "auto"))

    def reset_tags(self):
        self.app.engine.tag_hi, self.app.engine.tag_lo = 1.0, 0.0
        self.queue_draw()

    # geometry
    def _bar(self):
        h = self.get_height()
        return PAD_T, max(10, h - PAD_T - PAD_B)

    def _y_of(self, frac):
        top, bh = self._bar()
        return top + (1 - frac) * bh

    def _frac_of(self, y):
        top, bh = self._bar()
        return min(1.0, max(0.0, 1 - (y - top) / bh))

    def _drag_begin(self, g, x, y):
        eng = self.app.engine
        dh = abs(y - self._y_of(eng.tag_hi))
        dl = abs(y - self._y_of(eng.tag_lo))
        if x < self.BAR_X + self.BAR_W + 4 and min(dh, dl) < 14:
            self.drag_tag = "hi" if dh <= dl else "lo"
            self.start_y = y

    def _drag_update(self, g, dx, dy):
        if not self.drag_tag:
            return
        eng = self.app.engine
        f = self._frac_of(self.start_y + dy)
        if self.drag_tag == "hi":
            eng.tag_hi = max(f, eng.tag_lo + 0.01)
        else:
            eng.tag_lo = min(f, eng.tag_hi - 0.01)
        self.queue_draw()

    def _scroll(self, c, dx, dy):
        eng = self.app.engine
        s = self.app.settings
        r = eng.latest
        if r is None:
            return False
        step = s["colorbar_wheel_step"] * (-1 if dy > 0 else 1)
        top = self.mouse_y < self.get_height() / 2
        if eng.bar_mode == "auto":
            s["colorbar_manual_max"], s["colorbar_manual_min"] = round(r.tmax, 1), round(r.tmin, 1)
            eng.bar_mode = "manual_high" if top else "manual_low"
        elif eng.bar_mode == "manual_high" and not top:
            s["colorbar_manual_min"] = round(r.tmin, 1)
            eng.bar_mode = "manual"
        elif eng.bar_mode == "manual_low" and top:
            s["colorbar_manual_max"] = round(r.tmax, 1)
            eng.bar_mode = "manual"
        if top:
            s["colorbar_manual_max"] = max(s["colorbar_manual_min"] + 0.1, s["colorbar_manual_max"] + step)
        else:
            s["colorbar_manual_min"] = min(s["colorbar_manual_max"] - 0.1, s["colorbar_manual_min"] + step)
        s["colorbar_manual"] = True
        self.sync_mode()
        return True

    def _menu(self, g, n, x, y):
        m = Gio.Menu()
        sec = Gio.Menu()
        sec.append("Automatic Range", "cb.mode::auto")
        sec.append("Manual Range", "cb.mode::manual")
        sec.append("Set Color Bar Temp Range…", "cb.setrange")
        m.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Full Palette Range Adjustment", "cb.fullrange")
        sec.append("Show Center Temp Arrow", "cb.center")
        sec.append("Reset Palette Range Arrows", "cb.resettags")
        m.append_section(None, sec)
        ticks = Gio.Menu()
        for t in (3, 4, 5, 6, 8, 10, 12, 15):
            ticks.append(str(t), f"cb.ticks::{t}")
        m.append_submenu("Temperature Label Ticks", ticks)
        steps = Gio.Menu()
        for t in (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0):
            steps.append(f"{t:g}°", f"cb.step::{t}")
        m.append_submenu("Mouse Wheel Temp Step Size", steps)
        sec = Gio.Menu()
        sec.append("Live View Palette Follows Range Arrows", "cb.rangelive")
        sec.append("Dual Palette Follows Range Arrows", "cb.rangedual")
        m.append_section(None, sec)
        popover_menu(self, m, x, y)

    def _draw(self, area, cr, w, h):
        self.render(cr, w, h, self.app.engine.latest)

    def render(self, cr, w, h, r):
        cr.set_source_rgb(0.1, 0.1, 0.11)
        cr.paint()
        if r is None:
            return
        s = self.app.settings
        eng = self.app.engine
        top, bh = PAD_T, max(10, h - PAD_T - PAD_B)
        surf = palette_strip(r.palette)
        cr.save()
        cr.rectangle(self.BAR_X, top, self.BAR_W, bh)
        cr.clip()
        cr.translate(self.BAR_X, top)
        cr.scale(self.BAR_W, bh / 256)
        cr.set_source_surface(surf, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_BILINEAR)
        cr.paint()
        cr.restore()
        cr.set_source_rgb(0.5, 0.5, 0.5)
        cr.set_line_width(1)
        cr.rectangle(self.BAR_X + 0.5, top + 0.5, self.BAR_W, bh)
        cr.stroke()
        lo, hi, unit = r.bar_lo, r.bar_hi, r.unit
        span = hi - lo
        yv = lambda v: top + (1 - (v - lo) / span) * bh
        # ticks
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(11)
        for v in nice_ticks(lo, hi, s["colorbar_ticks"]):
            y = yv(v)
            cr.set_source_rgb(0.85, 0.85, 0.85)
            cr.move_to(self.BAR_X + self.BAR_W, y)
            cr.line_to(self.BAR_X + self.BAR_W + 5, y)
            cr.stroke()
            cr.move_to(self.BAR_X + self.BAR_W + 8, y + 4)
            cr.show_text(f"{v:g}")
        label(cr, w / 2, 2, fmt_t(hi, unit), "#ffffff", bg=False, size=12, anchor="center")
        label(cr, w / 2, h - 18, fmt_t(lo, unit), "#ffffff", bg=False, size=12, anchor="center")
        mode = {"auto": "AUTO", "manual": "MANUAL", "manual_high": "MAN-HI", "manual_low": "MAN-LO"}[eng.bar_mode]
        cr.set_font_size(9)
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.move_to(w - cr.text_extents(mode).x_advance - 3, h - 4)
        cr.show_text(mode)
        # palette range tags (left)
        for frac, name in ((eng.tag_hi, "MAX"), (eng.tag_lo, "MIN")):
            y = top + (1 - frac) * bh
            cr.move_to(self.BAR_X - 2, y)
            cr.line_to(self.BAR_X - 14, y - 7)
            cr.line_to(self.BAR_X - 14, y + 7)
            cr.close_path()
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(0, 0, 0)
            cr.stroke()
            at_end = frac >= 0.999 or frac <= 0.001
            txt = name if at_end else f"{lo + frac * span:.1f}"
            label(cr, 0, y - 22 if name == "MAX" else y + 8, txt, "#ffffff", size=9, bounds=(w, h))
        # max / min / center arrows inside the bar (manual range shows out-of-range arrows)
        marks = [(r.tmax, s["colors"]["max"]), (r.tmin, s["colors"]["min"])]
        if s["colorbar_center_arrow"]:
            marks.append((r.tcenter, s["colors"]["center"]))
        for v, col in marks:
            out = v > hi or v < lo
            y = min(max(yv(v), top), top + bh)
            x = self.BAR_X + self.BAR_W
            set_color(cr, "#ffffff" if out else col)
            if out:
                d = -1 if v > hi else 1
                cr.move_to(x + 2, y)
                cr.line_to(x + 10, y)
                cr.line_to(x + 6, y + 6 * d)
                cr.close_path()
            else:
                cr.move_to(x - 1, y)
                cr.line_to(x - 9, y - 5)
                cr.line_to(x - 9, y + 5)
                cr.close_path()
            cr.fill()


class Histogram(Gtk.DrawingArea):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.set_content_width(110)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._menu)
        self.add_controller(click)
        s = app.settings
        g = Gio.SimpleActionGroup()
        add_action(g, "source", lambda v: s.__setitem__("histogram_source", v.get_string()),
                   GLib.Variant.new_string(s["histogram_source"]), "s")
        add_action(g, "bins", lambda v: s.__setitem__("histogram_bins", int(v.get_string())),
                   GLib.Variant.new_string(str(s["histogram_bins"])), "s")
        add_action(g, "dual", lambda v: s.__setitem__("histogram_dual_palette", v.get_boolean()),
                   GLib.Variant.new_boolean(s["histogram_dual_palette"]))
        add_action(g, "ranged", lambda v: s.__setitem__("histogram_ranged_palette", v.get_boolean()),
                   GLib.Variant.new_boolean(s["histogram_ranged_palette"]))
        self.insert_action_group("hg", g)

    def _menu(self, g, n, x, y):
        eng = self.app.engine
        m = Gio.Menu()
        src = Gio.Menu()
        src.append("Whole Frame", "hg.source::frame")
        if eng.zoom_rect:
            src.append("Zoom Window ROI", "hg.source::zoom")
        for i in range(len(eng.rois)):
            src.append(f"ROI {i + 1}", f"hg.source::roi {i + 1}")
        for i in range(len(eng.lines)):
            src.append(f"Line {i + 1}", f"hg.source::line {i + 1}")
        m.append_submenu("Histogram Source", src)
        bins = Gio.Menu()
        for b in (16, 32, 64, 128, 256):
            bins.append(str(b), f"hg.bins::{b}")
        m.append_submenu("Number Of Bins", bins)
        sec = Gio.Menu()
        sec.append("Use Dual Palette Colors", "hg.dual")
        sec.append("Show Ranged Palette", "hg.ranged")
        m.append_section(None, sec)
        popover_menu(self, m, x, y)

    def _draw(self, area, cr, w, h):
        self.render(cr, w, h, self.app.engine.latest)

    def render(self, cr, w, h, r):
        cr.set_source_rgb(0.1, 0.1, 0.11)
        cr.paint()
        if r is None or r.hist is None:
            return
        s = self.app.settings
        from .. import palettes
        if s["histogram_ranged_palette"]:
            pal = r.dual_palette if s["histogram_dual_palette"] else r.palette
        else:
            pal = palettes.get(s["dual_palette"] if s["histogram_dual_palette"] else s["palette"])
        top, bh = PAD_T, max(10, h - PAD_T - PAD_B)
        hist = r.hist
        n = len(hist)
        mx = max(1, hist.max())
        bar_h = bh / n
        for i, c in enumerate(hist):
            frac = (i + 0.5) / n
            col = pal[int(frac * (len(pal) - 1))]
            cr.set_source_rgb(col[0] / 255, col[1] / 255, col[2] / 255)
            length = (w - 8) * (c / mx)
            y = top + bh - (i + 1) * bar_h
            cr.rectangle(w - 4 - length, y, length, max(1, bar_h - 0.5))
            cr.fill()
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10)
        cr.move_to(4, 14)
        src = s["histogram_source"]
        cr.show_text("Histogram" + ("" if src == "frame" else f" · {src.upper()}"))


class Plot2D(Gtk.DrawingArea):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.set_content_height(220)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.cursor_x = None
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._click)
        self.add_controller(click)
        s = app.settings
        g = Gio.SimpleActionGroup()
        add_action(g, "tx", lambda v: s.__setitem__("plot_ticks_x", int(v.get_string())),
                   GLib.Variant.new_string(str(s["plot_ticks_x"])), "s")
        add_action(g, "ty", lambda v: s.__setitem__("plot_ticks_y", int(v.get_string())),
                   GLib.Variant.new_string(str(s["plot_ticks_y"])), "s")
        add_action(g, "border", lambda v: s.__setitem__("plot_border", v.get_boolean()),
                   GLib.Variant.new_boolean(s["plot_border"]))
        add_action(g, "grid", lambda v: s.__setitem__("plot_grid", v.get_boolean()),
                   GLib.Variant.new_boolean(s["plot_grid"]))
        add_action(g, "window", lambda v: s.__setitem__("plot_seconds", int(v.get_string())),
                   GLib.Variant.new_string(str(s["plot_seconds"])), "s")
        add_action(g, "clear", self.clear)
        add_action(g, "logstart", lambda: app.engine.start_logging())
        add_action(g, "logstop", lambda: app.engine.stop_logging())
        self.insert_action_group("pl", g)

    def clear(self):
        import time
        eng = self.app.engine
        eng.plot = []
        eng.plot_t0 = time.monotonic()
        self.queue_draw()

    def _click(self, g, n, x, y):
        if g.get_current_button() == 3:
            m = Gio.Menu()
            tx, ty = Gio.Menu(), Gio.Menu()
            for t in (4, 6, 8, 10, 12, 16, 20):
                tx.append(str(t), f"pl.tx::{t}")
                ty.append(str(t), f"pl.ty::{t}")
            m.append_submenu("X-Axis Ticks", tx)
            m.append_submenu("Y-Axis Ticks", ty)
            win = Gio.Menu()
            for t in (10, 30, 60, 120, 300, 600, 1800, 3600):
                win.append(f"{t} s" if t < 120 else f"{t // 60} min", f"pl.window::{t}")
            m.append_submenu("Time Window", win)
            sec = Gio.Menu()
            sec.append("Show Plot Border", "pl.border")
            sec.append("Show Plot Grid", "pl.grid")
            sec.append("Clear Plot Data", "pl.clear")
            m.append_section(None, sec)
            sec = Gio.Menu()
            sec.append("Start Data Logging", "pl.logstart")
            sec.append("Stop Data Logging", "pl.logstop")
            m.append_section(None, sec)
            popover_menu(self, m, x, y)
        elif g.get_current_button() == 1:
            self.cursor_x = None if self.cursor_x is not None else x
            self.queue_draw()

    def _draw(self, area, cr, w, h):
        cr.set_source_rgb(0.08, 0.08, 0.09)
        cr.paint()
        eng = self.app.engine
        s = self.app.settings
        data = list(eng.plot)
        unit = eng.unit()[2]
        L, R, T, B = 56, 12, 26, 24
        pw, ph = w - L - R, h - T - B
        if pw < 20 or ph < 20:
            return
        sets = s["plot_sets"]
        if data:
            t1 = data[-1][0]
            t0 = max(0.0, t1 - s["plot_seconds"])
            vals = [v for _, row in data for v in row if v is not None and not math.isnan(v)]
        else:
            t0, t1, vals = 0.0, float(s["plot_seconds"]), []
        t1 = max(t1, t0 + 1)
        lo, hi = (min(vals), max(vals)) if vals else (0.0, 1.0)
        if hi - lo < 0.5:
            mid = (hi + lo) / 2
            lo, hi = mid - 0.25, mid + 0.25
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad
        X = lambda t: L + (t - t0) / (t1 - t0) * pw
        Y = lambda v: T + (1 - (v - lo) / (hi - lo)) * ph
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10)
        cr.set_line_width(1)
        for v in nice_ticks(lo, hi, s["plot_ticks_y"]):
            if s["plot_grid"]:
                cr.set_source_rgba(1, 1, 1, 0.09)
                cr.move_to(L, Y(v))
                cr.line_to(L + pw, Y(v))
                cr.stroke()
            cr.set_source_rgb(0.75, 0.75, 0.75)
            txt = f"{v:g}"
            cr.move_to(L - 6 - cr.text_extents(txt).x_advance, Y(v) + 3)
            cr.show_text(txt)
        for t in nice_ticks(t0, t1, s["plot_ticks_x"]):
            if s["plot_grid"]:
                cr.set_source_rgba(1, 1, 1, 0.09)
                cr.move_to(X(t), T)
                cr.line_to(X(t), T + ph)
                cr.stroke()
            cr.set_source_rgb(0.75, 0.75, 0.75)
            txt = f"{t:g}s"
            cr.move_to(X(t) - cr.text_extents(txt).x_advance / 2, T + ph + 15)
            cr.show_text(txt)
        if s["plot_border"]:
            cr.set_source_rgb(0.5, 0.5, 0.5)
            cr.rectangle(L + 0.5, T + 0.5, pw, ph)
            cr.stroke()
        cr.save()
        cr.rectangle(L, T, pw, ph)
        cr.clip()
        for k, ps in enumerate(sets):
            if not ps["enabled"]:
                continue
            set_color(cr, ps["color"])
            cr.set_line_width(ps["width"])
            started = False
            for t, row in data:
                if t < t0:
                    continue
                v = row[k] if k < len(row) else None
                if v is None or math.isnan(v):
                    started = False
                    continue
                if started:
                    cr.line_to(X(t), Y(v))
                else:
                    cr.move_to(X(t), Y(v))
                    started = True
            cr.stroke()
        cr.restore()
        # legend
        x = L
        cr.set_font_size(11)
        for ps in sets:
            if not ps["enabled"]:
                continue
            set_color(cr, ps["color"])
            cr.rectangle(x, 8, 12, 4)
            cr.fill()
            cr.set_source_rgb(0.85, 0.85, 0.85)
            cr.move_to(x + 16, 14)
            cr.show_text(ps["source"])
            x += 26 + cr.text_extents(ps["source"]).x_advance
        if eng.logging:
            label(cr, w - 4, 2, f"● LOGGING {eng.log_elapsed_ms / 1000:.0f}s", "#ff5050", size=11, anchor="right")
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.move_to(4, T + 10)
        cr.set_font_size(10)
        cr.show_text(unit)
        # cursor label
        if self.cursor_x is not None and data:
            cx = min(max(self.cursor_x, L), L + pw)
            tt = t0 + (cx - L) / pw * (t1 - t0)
            row = min(data, key=lambda d: abs(d[0] - tt))
            cr.set_source_rgba(1, 1, 1, 0.5)
            cr.move_to(cx, T)
            cr.line_to(cx, T + ph)
            cr.stroke()
            lines = [f"t = {row[0]:.2f} s"]
            for k, ps in enumerate(sets):
                if ps["enabled"] and k < len(row[1]) and row[1][k] is not None:
                    lines.append(f"{ps['source']}: {fmt_t(row[1][k], unit, 2)}")
            yy = T + 4
            for i, ln in enumerate(lines):
                col = "#ffffff" if i == 0 else [p["color"] for p in sets if p["enabled"]][i - 1]
                _, lh = label(cr, cx + 6, yy, ln, col, size=11, bounds=(w, h))
                yy += lh
