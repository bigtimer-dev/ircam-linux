"""Live view stream widget: image + movable measurement objects.

Counterpart of RMH_OpenGL_Winforms.h / LiveViewStream.h / RMH_LiveView_ZoomWindow.h.
"""
import math

import cairo
from gi.repository import Gdk, Gio, GLib, Gtk

from .. import engine as E
from .common import add_action, fmt_t, label, popover_menu, rgb_surface, set_color

HIT = 9  # px


def _dist_seg(px, py, x0, y0, x1, y1):
    dx, dy = x1 - x0, y1 - y0
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - x0) * dx + (py - y0) * dy) / L))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


class LiveView(Gtk.DrawingArea):
    def __init__(self, app, zoom=False):
        super().__init__()
        self.app = app
        self.zoom = zoom   # True: the split-view zoom window showing the zoom ROI
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_focusable(True)
        self.set_draw_func(self._draw)
        self.drag = None
        self.selected = None
        self.mouse = None
        if zoom:
            return
        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._menu)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", self._leave)
        self.add_controller(motion)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)
        self._actions()

    # ----------------------------------------------------------------- geometry
    def image_rect(self, w, h, r):
        """Where the thermal image is drawn inside a (w, h) area."""
        if r is None:
            return 0, 0, w, h
        iw, ih = r.width, r.height
        if self.zoom and self.app.engine.zoom_rect:
            _, _, iw, ih = self.app.engine.zoom_rect
        if self.app.settings["fixed_aspect"] or self.zoom:
            s = min(w / iw, h / ih)
            dw, dh = iw * s, ih * s
            return (w - dw) / 2, (h - dh) / 2, dw, dh
        return 0, 0, w, h

    def to_image(self, x, y):
        r = self.app.engine.latest
        if r is None:
            return None
        ox, oy, dw, dh = self.image_rect(self.get_width(), self.get_height(), r)
        return (x - ox) / dw * r.width, (y - oy) / dh * r.height

    def to_widget(self, ix, iy, rect, r):
        ox, oy, dw, dh = rect
        return ox + ix / r.width * dw, oy + iy / r.height * dh

    # ----------------------------------------------------------------- drawing
    def _draw(self, area, cr, w, h):
        self.render(cr, w, h, self.app.engine.latest, interactive=True)

    def render(self, cr, w, h, r, interactive=False):
        cr.set_source_rgb(0.05, 0.05, 0.06)
        cr.paint()
        if r is None:
            cr.set_source_rgb(0.5, 0.5, 0.5)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(15)
            txt = "Zoom window — add the zoom ROI on the live view" if self.zoom else \
                "No camera connected — pick a camera and press Connect"
            e = cr.text_extents(txt)
            cr.move_to((w - e.x_advance) / 2, h / 2)
            cr.show_text(txt)
            return
        s = self.app.settings
        eng = self.app.engine
        rect = self.image_rect(w, h, r)
        ox, oy, dw, dh = rect
        surf = rgb_surface(r.rgb)
        cr.save()
        cr.rectangle(ox, oy, dw, dh)
        cr.clip()
        if self.zoom and eng.zoom_rect:
            zx, zy, zw, zh = eng.zoom_rect
            sx, sy = dw / (zw * r.scale), dh / (zh * r.scale)
            cr.translate(ox, oy)
            cr.scale(sx, sy)
            cr.set_source_surface(surf, -zx * r.scale, -zy * r.scale)
        else:
            cr.translate(ox, oy)
            cr.scale(dw / r.rgb.shape[1], dh / r.rgb.shape[0])
            cr.set_source_surface(surf, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_BILINEAR if s["enhanced_resolution"] else cairo.FILTER_NEAREST)
        cr.paint()
        cr.restore()
        if self.zoom:
            if eng.zoom_rect and r.zoom:
                z = r.zoom
                label(cr, 6, 6, f"ZOOM  max {fmt_t(z.max, r.unit)}  min {fmt_t(z.min, r.unit)}  avg {fmt_t(z.avg, r.unit)}",
                      s["colors"]["zoom"], bg=True, size=12)
            return
        self.draw_overlays(cr, w, h, r, rect)
        st = []
        if not eng.running and eng.source and eng.source.live:
            st.append("PAUSED")
        if eng.recording:
            st.append("● REC")
        if eng.logging:
            st.append("● LOG")
        if st:
            label(cr, w - 6, 6, "   ".join(st), "#ff5050", size=13, anchor="right")

    def draw_overlays(self, cr, w, h, r, rect):
        s = self.app.settings
        eng = self.app.engine
        col = s["colors"]
        bg = s["label_background"]
        unit = r.unit
        P = lambda ix, iy: self.to_widget(ix + 0.5, iy + 0.5, rect, r)
        bounds = (w, h)
        cr.set_line_width(1.5)

        def cross(x, y, color, size=9, sel=False):
            set_color(cr, "#000000", 0.6)
            cr.set_line_width(3.5)
            for a, b, c, d in ((x - size, y, x + size, y), (x, y - size, x, y + size)):
                cr.move_to(a, b)
                cr.line_to(c, d)
            cr.stroke()
            set_color(cr, "#ffffff" if sel else color)
            cr.set_line_width(1.5)
            for a, b, c, d in ((x - size, y, x + size, y), (x, y - size, x, y + size)):
                cr.move_to(a, b)
                cr.line_to(c, d)
            cr.stroke()
            cr.arc(x, y, size * 0.45, 0, 2 * math.pi)
            cr.stroke()

        def tri(x, y, color, up=True):
            d = -1 if up else 1
            cr.move_to(x, y)
            cr.line_to(x - 7, y - 12 * d)
            cr.line_to(x + 7, y - 12 * d)
            cr.close_path()
            set_color(cr, color)
            cr.fill_preserve()
            cr.set_source_rgb(0, 0, 0)
            cr.set_line_width(1)
            cr.stroke()

        def zoom_roi():
            if eng.zoom_rect and s["split_view"]:
                x, y, zw, zh = eng.zoom_rect
                x0, y0 = self.to_widget(x, y, rect, r)
                x1, y1 = self.to_widget(x + zw, y + zh, rect, r)
                set_color(cr, col["zoom"])
                cr.set_dash([6, 4])
                cr.rectangle(x0, y0, x1 - x0, y1 - y0)
                cr.stroke()
                cr.set_dash([])
                label(cr, x0, y0 - 18, "ZOOM", col["zoom"], bg, 11, bounds=bounds)

        def dual():
            if s["dual_palette_box"] and eng.dual_rect:
                x, y, bw, bh = eng.dual_rect
                x0, y0 = self.to_widget(x, y, rect, r)
                x1, y1 = self.to_widget(x + bw, y + bh, rect, r)
                set_color(cr, "#ffffff" if self.selected == ("dual", 0) else col["dual"])
                cr.set_line_width(1.5)
                cr.rectangle(x0, y0, x1 - x0, y1 - y0)
                cr.stroke()

        def rois():
            for i, (rc, m) in enumerate(zip(list(eng.rois), r.rois)):
                x, y, bw, bh = rc
                x0, y0 = self.to_widget(x, y, rect, r)
                x1, y1 = self.to_widget(x + bw, y + bh, rect, r)
                sel = self.selected == ("roi", i)
                set_color(cr, "#000000", 0.5)
                cr.set_line_width(3.5)
                cr.rectangle(x0, y0, x1 - x0, y1 - y0)
                cr.stroke()
                set_color(cr, "#ffffff" if sel else col["rois"])
                cr.set_line_width(1.5)
                cr.rectangle(x0, y0, x1 - x0, y1 - y0)
                cr.stroke()
                label(cr, x0, y0 - 18, f"ROI{i + 1}: {fmt_t(m.max, unit)} / {fmt_t(m.min, unit)} / {fmt_t(m.avg, unit)}",
                      col["rois"], bg, 11, bounds=bounds)
                for (px, py), c, up in ((m.max_pos, col["max"], True), (m.min_pos, col["min"], False)):
                    wx, wy = P(px, py)
                    set_color(cr, c)
                    cr.arc(wx, wy, 2.5, 0, 2 * math.pi)
                    cr.fill()

        def lines():
            for i, (ln, m) in enumerate(zip(list(eng.lines), r.lines)):
                x0, y0 = self.to_widget(ln[0] + 0.5, ln[1] + 0.5, rect, r)
                x1, y1 = self.to_widget(ln[2] + 0.5, ln[3] + 0.5, rect, r)
                sel = self.selected == ("line", i)
                set_color(cr, "#000000", 0.5)
                cr.set_line_width(4)
                cr.move_to(x0, y0)
                cr.line_to(x1, y1)
                cr.stroke()
                set_color(cr, "#ffffff" if sel else col["lines"])
                cr.set_line_width(2)
                cr.move_to(x0, y0)
                cr.line_to(x1, y1)
                cr.stroke()
                for ex, ey in ((x0, y0), (x1, y1)):
                    cr.arc(ex, ey, 4, 0, 2 * math.pi)
                    cr.fill()
                wx, wy = P(*m.max_pos)
                tri(wx, wy, col["max"], True)
                wx, wy = P(*m.min_pos)
                tri(wx, wy, col["min"], False)
                lx, ly = (x0, y0) if y0 <= y1 else (x1, y1)
                label(cr, lx, ly - 20, f"L{i + 1}: max {fmt_t(m.max, unit)}  min {fmt_t(m.min, unit)}",
                      col["lines"], bg, 11, bounds=bounds)

        def points():
            for i, (x, y, t) in enumerate(r.points):
                wx, wy = P(x, y)
                cross(wx, wy, col["points"], sel=self.selected == ("point", i))
                label(cr, wx + 10, wy - 22, f"TM{i + 1}: {fmt_t(t, unit)}", col["points"], bg, 12, bounds=bounds)

        order = {"lines": lines, "rois": rois, "points": points}
        zoom_roi()
        dual()
        for name in s["render_order"]:
            order[name]()
        if s["track_center"]:
            wx, wy = P(*r.center_pos)
            cross(wx, wy, col["center"], 7)
            label(cr, wx + 9, wy + 4, fmt_t(r.tcenter, unit), col["center"], bg, 12, bounds=bounds)
        if s["track_max"]:
            wx, wy = P(*r.max_pos)
            tri(wx, wy, col["max"], True)
            label(cr, wx + 9, wy - 6, f"MAX {fmt_t(r.tmax, unit)}", col["max"], bg, 12, bounds=bounds)
        if s["track_min"]:
            wx, wy = P(*r.min_pos)
            tri(wx, wy, col["min"], False)
            label(cr, wx + 9, wy - 6, f"MIN {fmt_t(r.tmin, unit)}", col["min"], bg, 12, bounds=bounds)
        if s["track_cursor"] and r.cursor and self.mouse:
            x, y, t = r.cursor
            mx, my = self.mouse
            label(cr, mx + 14, my + 10, f"{fmt_t(t, unit, 2)}  [{x}, {y}]", col["cursor"], bg, 12, bounds=bounds)

    # ----------------------------------------------------------------- interaction
    def _hit(self, x, y):
        eng = self.app.engine
        r = eng.latest
        if r is None:
            return None
        rect = self.image_rect(self.get_width(), self.get_height(), r)
        W = lambda ix, iy: self.to_widget(ix, iy, rect, r)
        layers = {"points": [], "lines": [], "rois": []}
        for i, (px, py) in enumerate(eng.points):
            wx, wy = W(px + 0.5, py + 0.5)
            if math.hypot(x - wx, y - wy) < HIT + 3:
                layers["points"].append(("point", i, "move"))
        for i, ln in enumerate(eng.lines):
            a = W(ln[0] + 0.5, ln[1] + 0.5)
            b = W(ln[2] + 0.5, ln[3] + 0.5)
            if math.hypot(x - a[0], y - a[1]) < HIT:
                layers["lines"].append(("line", i, "p0"))
            elif math.hypot(x - b[0], y - b[1]) < HIT:
                layers["lines"].append(("line", i, "p1"))
            elif _dist_seg(x, y, *a, *b) < HIT - 2:
                layers["lines"].append(("line", i, "move"))

        def box_hit(kind, i, bx):
            x0, y0 = W(bx[0], bx[1])
            x1, y1 = W(bx[0] + bx[2], bx[1] + bx[3])
            if not (x0 - HIT <= x <= x1 + HIT and y0 - HIT <= y <= y1 + HIT):
                return None
            e = ""
            if abs(y - y0) < HIT:
                e += "n"
            elif abs(y - y1) < HIT:
                e += "s"
            if abs(x - x0) < HIT:
                e += "w"
            elif abs(x - x1) < HIT:
                e += "e"
            if e:
                return (kind, i, e)
            if x0 < x < x1 and y0 < y < y1:
                return (kind, i, "move")
            return None
        for i, bx in enumerate(eng.rois):
            hit = box_hit("roi", i, bx)
            if hit:
                layers["rois"].append(hit)
        for name in reversed(self.app.settings["render_order"]):
            if layers[name]:
                return layers[name][-1]
        if self.app.settings["dual_palette_box"] and eng.dual_rect:
            hit = box_hit("dual", 0, eng.dual_rect)
            if hit:
                return hit
        if self.app.settings["split_view"] and eng.zoom_rect:
            hit = box_hit("zoom", 0, eng.zoom_rect)
            if hit:
                return hit
        return None

    def _get(self, kind, i):
        eng = self.app.engine
        return {"point": lambda: eng.points[i], "line": lambda: eng.lines[i], "roi": lambda: eng.rois[i],
                "dual": lambda: eng.dual_rect, "zoom": lambda: eng.zoom_rect}[kind]()

    def _put(self, kind, i, v):
        eng = self.app.engine
        with eng.lock:
            if kind == "point":
                eng.points[i] = v
            elif kind == "line":
                eng.lines[i] = v
            elif kind == "roi":
                eng.rois[i] = v
            elif kind == "dual":
                eng.dual_rect = v
            else:
                eng.zoom_rect = v

    def _drag_begin(self, g, x, y):
        self.grab_focus()
        hit = self._hit(x, y)
        self.drag = None
        if hit:
            kind, i, part = hit
            self.selected = (kind, i)
            self.drag = (kind, i, part, self._get(kind, i), self.to_image(x, y))
        else:
            self.selected = None
        self.queue_draw()

    def _drag_update(self, g, dx, dy):
        if not self.drag:
            return
        r = self.app.engine.latest
        if r is None:
            return
        kind, i, part, orig, start = self.drag
        ok, sx, sy = g.get_start_point()
        cur = self.to_image(sx + dx, sy + dy)
        ddx, ddy = cur[0] - start[0], cur[1] - start[1]
        W, H = r.width, r.height
        clampx = lambda v: max(0.0, min(W - 1.0, v))
        clampy = lambda v: max(0.0, min(H - 1.0, v))
        if kind == "point":
            v = (clampx(orig[0] + ddx), clampy(orig[1] + ddy))
        elif kind == "line":
            x0, y0, x1, y1 = orig
            if part == "p0":
                v = (clampx(x0 + ddx), clampy(y0 + ddy), x1, y1)
            elif part == "p1":
                v = (x0, y0, clampx(x1 + ddx), clampy(y1 + ddy))
            else:
                ddx = max(-min(x0, x1), min(W - 1 - max(x0, x1), ddx))
                ddy = max(-min(y0, y1), min(H - 1 - max(y0, y1), ddy))
                v = (x0 + ddx, y0 + ddy, x1 + ddx, y1 + ddy)
        else:
            x, y, bw, bh = orig
            if part == "move":
                v = (max(0, min(W - bw, x + ddx)), max(0, min(H - bh, y + ddy)), bw, bh)
            else:
                x0, y0, x1, y1 = x, y, x + bw, y + bh
                if "w" in part:
                    x0 = min(max(0, x0 + ddx), x1 - 2)
                if "e" in part:
                    x1 = max(min(W, x1 + ddx), x0 + 2)
                if "n" in part:
                    y0 = min(max(0, y0 + ddy), y1 - 2)
                if "s" in part:
                    y1 = max(min(H, y1 + ddy), y0 + 2)
                v = (x0, y0, x1 - x0, y1 - y0)
        self._put(kind, i, v)
        self.queue_draw()

    def _drag_end(self, g, dx, dy):
        self.drag = None

    def _motion(self, c, x, y):
        self.mouse = (x, y)
        p = self.to_image(x, y)
        r = self.app.engine.latest
        eng = self.app.engine
        if p and r and 0 <= p[0] < r.width and 0 <= p[1] < r.height:
            eng.cursor = p
        else:
            eng.cursor = None
        hit = self._hit(x, y)
        cursor = None
        if hit:
            part = hit[2]
            cursor = {"move": "move", "p0": "crosshair", "p1": "crosshair", "n": "n-resize", "s": "s-resize",
                      "e": "e-resize", "w": "w-resize", "ne": "ne-resize", "nw": "nw-resize",
                      "se": "se-resize", "sw": "sw-resize"}.get(part, "default")
        self.set_cursor(Gdk.Cursor.new_from_name(cursor, None) if cursor else None)
        if self.app.settings["track_cursor"]:
            self.queue_draw()

    def _leave(self, c):
        self.mouse = None
        self.app.engine.cursor = None

    def _scroll(self, c, dx, dy):
        if self.app.settings["wheel_rotates"]:
            self.app.rotate(90 if dy > 0 else -90)
            return True
        return False

    # ----------------------------------------------------------------- objects
    def add_point(self):
        eng = self.app.engine
        r = eng.latest
        if r is None or len(eng.points) >= E.MAX_POINTS:
            self.app.log(f"Maximum {E.MAX_POINTS} temperature points.", "warning")
            return
        n = len(eng.points)
        with eng.lock:
            eng.points.append((r.width / 2 + 12 * (n % 5) - 24, r.height / 2 + 12 * (n // 5) - 6))

    def add_line(self):
        eng = self.app.engine
        r = eng.latest
        if r is None or len(eng.lines) >= E.MAX_LINES:
            self.app.log(f"Maximum {E.MAX_LINES} temperature lines.", "warning")
            return
        n = len(eng.lines)
        y = r.height * (0.3 + 0.1 * n)
        with eng.lock:
            eng.lines.append((r.width * 0.2, y, r.width * 0.8, y))

    def add_roi(self):
        eng = self.app.engine
        r = eng.latest
        if r is None or len(eng.rois) >= E.MAX_ROIS:
            self.app.log(f"Maximum {E.MAX_ROIS} regions of interest.", "warning")
            return
        n = len(eng.rois)
        bw, bh = r.width / 4, r.height / 4
        with eng.lock:
            eng.rois.append((min(r.width - bw, r.width / 2 - bw / 2 + 8 * n), min(r.height - bh, r.height / 2 - bh / 2 + 8 * n), bw, bh))

    def ensure_dual_rect(self):
        eng = self.app.engine
        r = eng.latest
        if r and not eng.dual_rect:
            eng.dual_rect = (r.width / 4, r.height / 4, r.width / 2, r.height / 2)

    def ensure_zoom_rect(self):
        eng = self.app.engine
        r = eng.latest
        if r and not eng.zoom_rect:
            eng.zoom_rect = (r.width / 3, r.height / 3, r.width / 3, r.height / 3)

    # ----------------------------------------------------------------- context menu
    def _actions(self):
        app = self.app
        s = app.settings
        g = Gio.SimpleActionGroup()
        eng = app.engine

        def delete(v):
            kind, _, idx = v.get_string().partition(":")
            with eng.lock:
                if kind == "point":
                    del eng.points[int(idx)]
                elif kind == "line":
                    del eng.lines[int(idx)]
                elif kind == "roi":
                    del eng.rois[int(idx)]
                elif kind == "points":
                    eng.points.clear()
                elif kind == "lines":
                    eng.lines.clear()
                elif kind == "rois":
                    eng.rois.clear()
                elif kind == "all":
                    eng.points.clear()
                    eng.lines.clear()
                    eng.rois.clear()
            self.selected = None
            self.queue_draw()
        add_action(g, "delete", delete, param="s")
        for key in ("invert_palette", "invert_dual", "invert_back", "label_background", "wheel_rotates",
                    "focus_assist", "split_view", "colorbar_visible", "tools_visible"):
            add_action(g, key, lambda v, k=key: app.set_setting(k, v.get_boolean()), GLib.Variant.new_boolean(s[key]))
        add_action(g, "rotcw", lambda: app.rotate(90))
        add_action(g, "rotccw", lambda: app.rotate(-90))
        add_action(g, "strength", lambda v: app.set_setting("sharpen_strength", float(v.get_string())),
                   GLib.Variant.new_string(str(float(s["sharpen_strength"]))), "s")
        add_action(g, "sigma", lambda v: app.set_setting("sharpen_sigma", float(v.get_string())),
                   GLib.Variant.new_string(str(float(s["sharpen_sigma"]))), "s")
        add_action(g, "order", lambda v: app.set_setting("render_order", v.get_string().split(",")),
                   GLib.Variant.new_string(",".join(s["render_order"])), "s")
        add_action(g, "color", lambda v: app.choose_color(v.get_string()), param="s")
        self.actions = g
        self.insert_action_group("lv", g)

    def sync_action(self, key):
        a = self.actions.lookup_action(key)
        if a:
            a.set_state(GLib.Variant.new_boolean(bool(self.app.settings[key])))

    def _menu(self, g, n, x, y):
        eng = self.app.engine
        m = Gio.Menu()
        dele = Gio.Menu()
        for i in range(len(eng.points)):
            dele.append(f"Temp Point TM{i + 1}", f"lv.delete::point:{i}")
        for i in range(len(eng.lines)):
            dele.append(f"Temp Line L{i + 1}", f"lv.delete::line:{i}")
        for i in range(len(eng.rois)):
            dele.append(f"ROI {i + 1}", f"lv.delete::roi:{i}")
        sec = Gio.Menu()
        sec.append("All Temp Points", "lv.delete::points:")
        sec.append("All Temp Lines", "lv.delete::lines:")
        sec.append("All ROIs", "lv.delete::rois:")
        sec.append("Delete All", "lv.delete::all:")
        dele.append_section(None, sec)
        m.append_submenu("Delete", dele)
        view = Gio.Menu()
        view.append("Split View (Zoom Window)", "lv.split_view")
        view.append("Show Color Bar And Histogram", "lv.colorbar_visible")
        view.append("Show Tools Panel", "lv.tools_visible")
        view.append("Label Backgrounds", "lv.label_background")
        m.append_section(None, view)
        pal = Gio.Menu()
        pal.append("Invert Live View Palette", "lv.invert_palette")
        pal.append("Invert Dual Palette", "lv.invert_dual")
        pal.append("Invert Color Bar Background Palette", "lv.invert_back")
        m.append_submenu("Palettes", pal)
        rot = Gio.Menu()
        rot.append("Rotate Clockwise", "lv.rotcw")
        rot.append("Rotate Counter-Clockwise", "lv.rotccw")
        rot.append("Mouse Wheel Rotates View", "lv.wheel_rotates")
        m.append_submenu("Rotation", rot)
        sh = Gio.Menu()
        st = Gio.Menu()
        for v in (0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0):
            st.append(f"{v:g}", f"lv.strength::{v}")
        sg = Gio.Menu()
        for v in (0.2, 0.5, 1.0, 1.5, 2.0):
            sg.append(f"{v:g}", f"lv.sigma::{v}")
        sh.append_submenu("Sharpening Strength", st)
        sh.append_submenu("Sharpening Deviation (Sigma)", sg)
        sh.append("Focus Assist Mode", "lv.focus_assist")
        m.append_submenu("Image Sharpening", sh)
        cols = Gio.Menu()
        for key, name in (("max", "Maximum Label"), ("min", "Minimum Label"), ("center", "Center Label"),
                          ("cursor", "Mouse Cursor Label"), ("points", "Temp Points"), ("lines", "Temp Lines"),
                          ("rois", "ROIs"), ("dual", "Dual Palette Box"), ("zoom", "Zoom ROI")):
            cols.append(name + "…", f"lv.color::{key}")
        m.append_submenu("Element Colors", cols)
        order = Gio.Menu()
        order.append("Points In Front", "lv.order::lines,rois,points")
        order.append("ROIs In Front", "lv.order::lines,points,rois")
        order.append("Lines In Front", "lv.order::rois,points,lines")
        m.append_submenu("Rendering Order", order)
        popover_menu(self, m, x, y)
