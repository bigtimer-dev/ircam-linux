"""Main window (MainGUI.h / LiveViewTools.h counterpart)."""
import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

import cairo
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import cameras as C
from .. import engine as E
from .. import palettes, sources
from ..config import Settings
from .common import add_action
from .liveview import LiveView
from .widgets import ColorBar, Histogram, Plot2D

APP_ID = "io.github.bigtimer_dev.IrcamLinux"
MANUAL_URL = "https://github.com/Cyclotronic/OpenRMH-IRCAM/blob/main/docs/manual/manual.md"

HOTKEYS = [
    ("1", "Toggle the maximum temperature tracking label"),
    ("2", "Toggle the minimum temperature tracking label"),
    ("3", "Toggle the center temperature tracking label"),
    ("4", "Add a movable temperature point"),
    ("5", "Add a movable temperature line"),
    ("6", "Toggle the histogram"),
    ("7", "Add a movable ROI box"),
    ("8", "Toggle mouse cursor temperature tracking"),
    ("9", "Toggle the dual palette box"),
    ("A", "Toggle fixed aspect ratio"),
    ("C / F / K", "Celsius / Fahrenheit / Kelvin"),
    ("H", "Live view Run/Stop"),
    ("O", "Single frame trigger (while stopped)"),
    ("M / N", "Start / stop CSV data logging"),
    ("P", "Start / stop the periodic triggers"),
    ("Q", "Calibrate the camera (NUC / shutter)"),
    ("R", "Rotate the live view 90° clockwise"),
    ("S", "Take a snapshot (and RAW snapshot)"),
    ("T", "Toggle the high / low temperature range"),
    ("U", "Toggle ULTRA resolution"),
    ("V", "Start / stop video recording"),
    ("X", "Toggle enhanced resolution (smooth scaling)"),
    ("Y", "Toggle image sharpening"),
    ("G", "Show / hide the 2D temperature plot (Linux addition)"),
    ("Ctrl+,", "Settings"),
]

CSS = b"""
.tools-panel { background: alpha(@window_fg_color, 0.03); }
.status-bar { padding: 3px 10px; border-top: 1px solid alpha(@window_fg_color, 0.12); font-size: 0.9em; }
.rec-on { color: #ff5050; }
.tool-heading { font-weight: bold; font-size: 0.85em; opacity: 0.7; margin-top: 8px; }
"""


class IrcamApp(Adw.Application):
    def __init__(self, demo=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.demo = demo
        self.settings = Settings.load()
        self.engine = E.Engine(self.settings, self.log, self._notify)
        self.win = None
        self.stats_win = self.surface_win = self.emis_win = self.palette_win = None
        self._frame_pending = False
        self._last_plot = 0.0
        self._screen_proc = None
        self.tool_widgets = {}
        self.connect("activate", self._activate)

    # ================================================================== logging / notifications
    def log(self, msg, kind="info"):
        print(f"[{kind}] {msg}", flush=True)
        GLib.idle_add(self._show_log, msg, kind)

    def _show_log(self, msg, kind):
        if not self.win:
            return False
        self.status_msg.set_label(msg.splitlines()[0])
        self.status_msg.set_tooltip_text(msg)
        if kind in ("warning", "error", "success"):
            t = Adw.Toast(title=GLib.markup_escape_text(msg.splitlines()[0]), timeout=3 if kind == "success" else 5)
            self.toasts.add_toast(t)
        return False

    def _notify(self, kind, payload):
        if kind == "frame":
            if not self._frame_pending:
                self._frame_pending = True
                GLib.idle_add(self._on_frame)
        else:
            GLib.idle_add(self._on_event, kind, payload)

    def _on_frame(self):
        self._frame_pending = False
        if not self.win:
            return False
        self.live.queue_draw()
        if self.settings["split_view"]:
            self.zoomview.queue_draw()
        if self.settings["colorbar_visible"]:
            self.colorbar.queue_draw()
            if self.settings["histogram"]:
                self.histogram.queue_draw()
        now = time.monotonic()
        if self.settings["plot_visible"] and now - self._last_plot > 0.1:
            self._last_plot = now
            self.plot.queue_draw()
        return False

    def _on_event(self, kind, payload):
        if kind == "event":
            self.run_event(payload)
        elif kind == "beep":
            self.beep()
        elif kind == "busy":
            self.busy_label.set_label(payload or "")
            self.busy_spinner.set_visible(bool(payload))
        elif kind == "disconnected":
            self.disconnect()
        elif kind == "logging":
            self._sync_toggle(self.btn_log, payload)
        elif kind == "recording":
            self._sync_toggle(self.btn_rec, payload)
            if not payload:
                self._stop_screen_capture()
        elif kind == "range":
            self.btn_range.set_tooltip_text(f"Temperature range: {'HIGH' if payload else 'LOW'} (T)")
            self._sync_range_buttons()
        return False

    def run_event(self, ev):
        eng = self.engine
        if ev == "Start Data Logging":
            eng.start_logging()
        elif ev == "Stop Data Logging":
            eng.stop_logging()
        elif ev == "Start Video Recording":
            self.start_recording()
        elif ev == "Stop Video Recording":
            eng.stop_recording()
        elif ev == "Save Snapshot":
            self.snapshot()
        elif ev == "Save Frame Temp Data":
            eng.save_frame_csv()

    def beep(self):
        for f in ("/usr/share/sounds/freedesktop/stereo/bell.oga",
                  "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga"):
            if os.path.exists(f):
                for player in ("pw-play", "paplay"):
                    try:
                        subprocess.Popen([player, f], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return
                    except OSError:
                        continue
        if self.win:
            self.win.get_display().beep()

    # ================================================================== UI construction
    def _activate(self, app):
        if self.win:
            self.win.present()
            return
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        self.win = self.build_window()
        self._app_actions()
        self.win.present()
        GLib.timeout_add(250, self._status_tick)
        path = getattr(self, "open_path", None)
        if path:
            mode = C.RECORDING_ANALYSIS if path.lower().endswith(".avi") else C.SNAPSHOT_ANALYSIS
            self.camera_dd.set_selected(C.source_names().index(mode))
            GLib.idle_add(lambda: self.connect_camera(path) and False)
        elif self.demo:
            self.camera_dd.set_selected(C.source_names().index(C.DEMO_CAMERA))
            GLib.idle_add(self.connect_camera)

    def build_window(self):
        s = self.settings
        win = Adw.ApplicationWindow(application=self, title="IRCAM Thermal Viewer")
        win.set_default_size(*s["window_size"])
        win.connect("close-request", self._on_close)
        self.toasts = Adw.ToastOverlay()
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        names = C.source_names()
        self.camera_dd = Gtk.DropDown.new_from_strings(names)
        self.camera_dd.set_enable_search(True)
        self.camera_dd.set_tooltip_text("Camera / source")
        if s["camera_source"] in names:
            self.camera_dd.set_selected(names.index(s["camera_source"]))
        hb.pack_start(self.camera_dd)
        self.btn_connect = Gtk.Button(label="Connect")
        self.btn_connect.add_css_class("suggested-action")
        self.btn_connect.connect("clicked", lambda b: self.disconnect() if self.engine.source else self.connect_camera())
        hb.pack_start(self.btn_connect)
        self.busy_spinner = Adw.Spinner(visible=False)
        self.busy_label = Gtk.Label()
        hb.pack_start(self.busy_spinner)
        hb.pack_start(self.busy_label)
        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append("Settings", "app.settings")
        sec.append("Statistics", "app.statistics")
        sec.append("Emissivity Table", "app.emissivity")
        sec.append("3D Surface Plot", "app.surface")
        sec.append("Palette Creator", "app.palette-creator")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Open RAW Snapshot…", "app.open-snapshot")
        sec.append("Open RAW Recording…", "app.open-recording")
        sec.append("Save Full Frame Temperature Data", "app.frame-csv")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Keyboard Shortcuts", "app.shortcuts")
        sec.append("User Guide (Online)", "app.guide")
        sec.append("About", "app.about")
        menu.append_section(None, sec)
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True, tooltip_text="Main menu"))
        tv.add_top_bar(hb)
        tv.add_top_bar(self._action_bar())

        # main area
        self.live = LiveView(self)
        self.zoomview = LiveView(self, zoom=True)
        self.split = Gtk.Box(homogeneous=True, spacing=2, hexpand=True)
        self.split.append(self.live)
        self.split.append(self.zoomview)
        self.live.set_size_request(240, 180)
        self.zoomview.set_visible(s["split_view"])
        self.colorbar = ColorBar(self)
        self.histogram = Histogram(self)
        self.bar_box = Gtk.Box()
        self.bar_box.append(self.colorbar)
        self.bar_box.append(self.histogram)
        self.histogram.set_visible(s["histogram"])
        self.bar_box.set_visible(s["colorbar_visible"])
        top = Gtk.Box(vexpand=True)
        top.append(self.split)
        top.append(self.bar_box)
        self.plot = Plot2D(self)
        self.plot.set_size_request(-1, 100)
        vpan = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, start_child=top, end_child=self.plot,
                         resize_start_child=True, resize_end_child=False, shrink_start_child=False,
                         shrink_end_child=False, vexpand=True, hexpand=True)
        self._vpan_set = False

        def place(p, _):
            if not self._vpan_set and p.get_property("max-position") > 300:
                self._vpan_set = True
                p.set_position(int(p.get_property("max-position") * 0.7))
        vpan.connect("notify::max-position", place)
        self.vpan = vpan
        self.plot.set_visible(s["plot_visible"])
        self.tools = self._tools_panel()
        self.tools.set_visible(s["tools_visible"])
        body = Gtk.Box()
        body.append(self.tools)
        body.append(vpan)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(body)
        self.playback = self._playback_bar()
        outer.append(self.playback)
        outer.append(self._status_bar())
        self.toasts.set_child(outer)
        tv.set_content(self.toasts)
        win.set_content(tv)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        win.add_controller(keys)
        return win

    def _sync_range_buttons(self):
        if hasattr(self, "range_low"):
            b = self.range_high if self.engine.high_range else self.range_low
            self._sync_toggle(b, True)

    def _tb(self, icon, tip, cb, toggle=False):
        b = Gtk.ToggleButton(icon_name=icon) if toggle else Gtk.Button(icon_name=icon)
        b.set_tooltip_text(tip)
        b.add_css_class("flat")
        if toggle:
            b._cb = b.connect("toggled", lambda x: cb(x.get_active()))
        else:
            b.connect("clicked", lambda x: cb())
        return b

    def _sync_toggle(self, b, active):
        b.handler_block(b._cb)
        b.set_active(active)
        b.handler_unblock(b._cb)

    def _action_bar(self):
        bar = Gtk.Box(spacing=2, margin_start=6, margin_end=6, margin_top=2, margin_bottom=2)
        self.btn_run = self._tb("media-playback-pause-symbolic", "Run / Stop live view (H)", self.toggle_run)
        self.btn_single = self._tb("media-skip-forward-symbolic", "Single frame trigger (O)", self.single_frame)
        self.btn_cal = self._tb("view-refresh-symbolic", "Calibrate camera (Q)", lambda: self.engine.calibrate())
        self.btn_range = self._tb("weather-clear-symbolic", "Toggle high / low temperature range (T)", lambda: self.engine.toggle_range())
        self.btn_snap = self._tb("camera-photo-symbolic", "Snapshot (S)", self.snapshot)
        self.btn_csv = self._tb("x-office-spreadsheet-symbolic", "Save full frame temperature data", lambda: self.engine.save_frame_csv())
        self.btn_rec = self._tb("media-record-symbolic", "Record video (V)", self._rec_toggled, toggle=True)
        self.btn_log = self._tb("document-save-symbolic", "CSV data logging (M / N)", self._log_toggled, toggle=True)
        self.btn_trig = self._tb("alarm-symbolic", "Periodic triggers (P)", self.set_triggers_running, toggle=True)
        self._sync_toggle(self.btn_trig, self.settings["triggers_running"])
        for b in (self.btn_run, self.btn_single, self.btn_cal, self.btn_range):
            bar.append(b)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        for b in (self.btn_snap, self.btn_csv, self.btn_rec, self.btn_log, self.btn_trig):
            bar.append(b)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.unit_btns = {}
        group = None
        for u, txt in (("C", "°C"), ("F", "°F"), ("K", "K")):
            b = Gtk.ToggleButton(label=txt, group=group, tooltip_text=f"Units ({u})")
            b.add_css_class("flat")
            group = group or b
            b.set_active(self.settings["temp_unit"] == u)
            b.connect("toggled", lambda x, uu=u: x.get_active() and self.set_unit(uu))
            self.unit_btns[u] = b
            bar.append(b)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        bar.append(self._tb("object-rotate-left-symbolic", "Rotate counter-clockwise", lambda: self.rotate(-90)))
        bar.append(self._tb("object-rotate-right-symbolic", "Rotate clockwise (R)", lambda: self.rotate(90)))
        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)
        tools = self._tb("sidebar-show-symbolic", "Show tools panel", lambda a: self.set_setting("tools_visible", a), toggle=True)
        self._sync_toggle(tools, self.settings["tools_visible"])
        self.tool_widgets["tools_visible"] = tools
        bar.append(tools)
        self._update_buttons()
        return bar

    def _tools_panel(self):
        s = self.settings
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=10, margin_end=10, margin_top=6, margin_bottom=10)

        def heading(t):
            l = Gtk.Label(label=t, xalign=0)
            l.add_css_class("tool-heading")
            box.append(l)

        def check(key, text, hk=None):
            c = Gtk.CheckButton(label=text + (f"  ({hk})" if hk else ""))
            c.set_active(bool(s[key]))
            c._cb = c.connect("toggled", lambda x: self.set_setting(key, x.get_active()))
            self.tool_widgets[key] = c
            box.append(c)

        def button(text, cb):
            b = Gtk.Button(label=text)
            b.connect("clicked", lambda x: cb())
            box.append(b)

        heading("PALETTES")
        self.pal_dd = {}
        for key, title in (("palette", "Live view"), ("dual_palette", "Dual palette"), ("back_palette", "Background")):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row.append(Gtk.Label(label=title, xalign=0))
            dd = Gtk.DropDown.new_from_strings(palettes.names())
            dd.set_enable_search(True)
            self._dd_select(dd, s[key])
            dd.connect("notify::selected", lambda d, p, k=key: self.set_setting(k, d.get_selected_item().get_string()))
            self.pal_dd[key] = dd
            row.append(dd)
            box.append(row)
        check("invert_palette", "Invert live view palette")
        heading("TRACKING")
        check("track_max", "Maximum temp label", "1")
        check("track_min", "Minimum temp label", "2")
        check("track_center", "Center temp label", "3")
        check("track_cursor", "Mouse cursor temp", "8")
        heading("MEASUREMENTS")
        button("Add Temp Point  (4)", self.live.add_point)
        button("Add Temp Line  (5)", self.live.add_line)
        button("Add ROI Box  (7)", self.live.add_roi)
        check("dual_palette_box", "Dual palette box", "9")
        check("split_view", "Split zoom view")
        heading("DISPLAY")
        check("histogram", "Histogram", "6")
        check("colorbar_visible", "Color bar")
        check("plot_visible", "2D temperature plot", "G")
        check("fixed_aspect", "Fixed aspect ratio", "A")
        check("ultra_resolution", "ULTRA resolution", "U")
        check("enhanced_resolution", "Enhanced resolution", "X")
        check("sharpening", "Image sharpening", "Y")
        check("focus_assist", "Focus assist")
        check("label_background", "Label backgrounds")
        heading("TEMPERATURE RANGE  (T)")
        row = Gtk.Box(css_classes=["linked"])
        self.range_low = Gtk.ToggleButton(label="Low", hexpand=True, active=True,
                                          tooltip_text="About −20…150 °C, most precise")
        self.range_high = Gtk.ToggleButton(label="High", group=self.range_low, hexpand=True,
                                           tooltip_text="Up to about 550–600 °C, for hot objects")
        for b, hi in ((self.range_low, False), (self.range_high, True)):
            b._cb = b.connect("toggled", lambda x, h=hi: x.get_active() and self.engine.set_range(h))
            row.append(b)
        self.range_box = row
        box.append(row)
        heading("COLOR BAR")
        row = Gtk.Box(spacing=0, css_classes=["linked"])
        self.bar_auto = Gtk.ToggleButton(label="Auto", hexpand=True)
        self.bar_manual = Gtk.ToggleButton(label="Manual", group=self.bar_auto, hexpand=True)
        (self.bar_manual if s["colorbar_manual"] else self.bar_auto).set_active(True)
        self.bar_auto.connect("toggled", lambda b: b.get_active() and self.colorbar.actions.activate_action("mode", GLib.Variant.new_string("auto")))
        self.bar_manual.connect("toggled", lambda b: b.get_active() and self.colorbar.actions.activate_action("mode", GLib.Variant.new_string("manual")))
        row.append(self.bar_auto)
        row.append(self.bar_manual)
        box.append(row)
        button("Set Range…", self.dialog_colorbar_range)
        sw = Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_size_request(236, -1)
        sw.set_hexpand(False)
        sw.add_css_class("tools-panel")
        return sw

    def _dd_select(self, dd, name):
        model = dd.get_model()
        for i in range(model.get_n_items()):
            if model.get_string(i) == name:
                dd.set_selected(i)
                return

    def _playback_bar(self):
        rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        bar = Gtk.Box(spacing=6, margin_start=8, margin_end=8, margin_top=4, margin_bottom=4)
        self.pb_play = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play / pause")
        self.pb_play.connect("clicked", lambda b: self._pb_toggle())
        prev = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Previous frame")
        prev.connect("clicked", lambda b: self._pb_step(-1))
        nxt = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Next frame")
        nxt.connect("clicked", lambda b: self._pb_step(1))
        self.pb_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, draw_value=False)
        self.pb_scale.set_range(0, 1)
        self.pb_scale.connect("change-value", lambda sc, t, v: self._pb_seek(v))
        self.pb_label = Gtk.Label(label="0 / 0")
        self.pb_label.add_css_class("monospace")
        self.pb_speed = Gtk.DropDown.new_from_strings(["0.25×", "0.5×", "1×", "2×", "4×"])
        self.pb_speed.set_selected(2)
        self.pb_speed.connect("notify::selected", lambda d, p: setattr(self.engine.source, "speed", [0.25, 0.5, 1, 2, 4][d.get_selected()]) if isinstance(self.engine.source, sources.RecordingSource) else None)
        self.pb_loop = Gtk.CheckButton(label="Loop", active=True)
        self.pb_loop.connect("toggled", lambda c: setattr(self.engine.source, "loop", c.get_active()) if isinstance(self.engine.source, sources.RecordingSource) else None)
        for w in (prev, self.pb_play, nxt, self.pb_scale, self.pb_label, self.pb_speed, self.pb_loop):
            bar.append(w)
        rev.set_child(bar)
        return rev

    def _pb_toggle(self):
        src = self.engine.source
        if isinstance(src, sources.RecordingSource):
            src.playing = not src.playing

    def _pb_step(self, d):
        src = self.engine.source
        if isinstance(src, sources.RecordingSource):
            src.playing = False
            src.seek(src.position + d)

    def _pb_seek(self, v):
        src = self.engine.source
        if isinstance(src, sources.RecordingSource):
            src.seek(v)
        return False

    def _status_bar(self):
        bar = Gtk.Box(spacing=16)
        bar.add_css_class("status-bar")
        self.status_msg = Gtk.Label(label="Ready", xalign=0, hexpand=True, ellipsize=3)
        self.status_info = Gtk.Label(xalign=1)
        self.status_info.add_css_class("monospace")
        bar.append(self.status_msg)
        bar.append(self.status_info)
        return bar

    def _status_tick(self):
        eng = self.engine
        src = eng.source
        if src is None:
            self.status_info.set_label("not connected")
        else:
            parts = [src.name, f"{src.fps():.1f} fps", f"frame {eng.frames}"]
            if src.live:
                us = eng.unit()
                parts.append(f"FPA {src.cam.temp_fpa * us[0] + us[1]:.1f}{us[2]}")
                parts.append(f"drift {eng.drift() * us[0]:+.2f}")
                parts.append("HIGH range" if eng.high_range else "LOW range")
            if eng.recording:
                parts.append(f"● REC {eng.rec_frames}")
            if eng.logging:
                parts.append(f"● LOG {eng.log_sample}")
            self.status_info.set_label("   ".join(parts))
        if isinstance(src, sources.RecordingSource):
            self.pb_scale.set_range(0, max(1, src.frames - 1))
            self.pb_scale.set_value(src.position)
            self.pb_label.set_label(f"{src.position + 1} / {src.frames}")
            self.pb_play.set_icon_name("media-playback-pause-symbolic" if src.playing else "media-playback-start-symbolic")
        return True

    def _update_buttons(self):
        src = self.engine.source
        live = bool(src and src.live)
        connected = src is not None
        self.btn_connect.set_label("Disconnect" if connected else "Connect")
        if connected:
            self.btn_connect.remove_css_class("suggested-action")
            self.btn_connect.add_css_class("destructive-action")
        else:
            self.btn_connect.add_css_class("suggested-action")
            self.btn_connect.remove_css_class("destructive-action")
        self.camera_dd.set_sensitive(not connected)
        for b in (self.btn_run, self.btn_single, self.btn_snap, self.btn_csv, self.btn_rec, self.btn_log):
            b.set_sensitive(connected)
        self.btn_cal.set_sensitive(bool(live and src.can_calibrate))
        self.btn_range.set_sensitive(bool(live and src.can_switch_range))
        if hasattr(self, "range_box"):
            self.range_box.set_sensitive(bool(live and src.can_switch_range))
            self._sync_range_buttons()
        self.btn_run.set_icon_name("media-playback-pause-symbolic" if self.engine.running else "media-playback-start-symbolic")
        self.btn_single.set_sensitive(connected and not self.engine.running)
        if hasattr(self, "playback"):
            self.playback.set_reveal_child(isinstance(src, sources.RecordingSource))

    # ================================================================== app actions & keys
    def _app_actions(self):
        add_action(self, "settings", self.open_settings)
        add_action(self, "statistics", self.open_statistics)
        add_action(self, "emissivity", self.open_emissivity)
        add_action(self, "surface", self.open_surface)
        add_action(self, "palette-creator", self.open_palette_creator)
        add_action(self, "open-snapshot", lambda: self._open_file(C.SNAPSHOT_ANALYSIS))
        add_action(self, "open-recording", lambda: self._open_file(C.RECORDING_ANALYSIS))
        add_action(self, "frame-csv", lambda: self.engine.save_frame_csv())
        add_action(self, "shortcuts", self.show_shortcuts)
        add_action(self, "guide", lambda: Gtk.UriLauncher(uri=MANUAL_URL).launch(self.win, None, None))
        add_action(self, "about", self.show_about)
        self.set_accels_for_action("app.settings", ["<Control>comma"])
        self.set_accels_for_action("app.open-snapshot", ["<Control>o"])

    def _key(self, ctrl, keyval, keycode, state):
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        focus = self.win.get_focus()
        if isinstance(focus, (Gtk.Text, Gtk.Entry, Gtk.SpinButton)) or (focus and focus.get_ancestor(Gtk.Popover)):
            return False
        k = Gdk.keyval_name(Gdk.keyval_to_upper(keyval)) or ""
        eng = self.engine
        toggles = {"1": "track_max", "2": "track_min", "3": "track_center", "6": "histogram", "8": "track_cursor",
                   "9": "dual_palette_box", "A": "fixed_aspect", "U": "ultra_resolution", "X": "enhanced_resolution",
                   "Y": "sharpening", "G": "plot_visible"}
        if k in toggles:
            self.set_setting(toggles[k], not self.settings[toggles[k]])
        elif k == "4":
            self.live.add_point()
        elif k == "5":
            self.live.add_line()
        elif k == "7":
            self.live.add_roi()
        elif k in ("C", "F", "K"):
            self.unit_btns[k].set_active(True)
        elif k == "H":
            self.toggle_run()
        elif k == "O":
            self.single_frame()
        elif k == "M":
            eng.start_logging()
        elif k == "N":
            eng.stop_logging()
        elif k == "P":
            self._sync_toggle(self.btn_trig, not self.settings["triggers_running"])
            self.set_triggers_running(not self.settings["triggers_running"])
        elif k == "Q":
            eng.calibrate()
        elif k == "R":
            self.rotate(90)
        elif k == "S":
            self.snapshot()
        elif k == "T":
            eng.toggle_range()
        elif k == "V":
            if eng.recording:
                eng.stop_recording()
            else:
                self.start_recording()
        else:
            return False
        return True

    # ================================================================== settings helpers
    def set_setting(self, key, value):
        s = self.settings
        s[key] = value
        w = self.tool_widgets.get(key)
        if w is not None and hasattr(w, "_cb"):
            self._sync_toggle(w, bool(value))
        if hasattr(self, "live"):
            self.live.sync_action(key) if key in ("invert_palette", "invert_dual", "invert_back", "label_background",
                                                  "wheel_rotates", "focus_assist", "split_view", "colorbar_visible",
                                                  "tools_visible", "plot_visible") else None
        if key == "dual_palette_box" and value:
            self.live.ensure_dual_rect()
        elif key == "split_view":
            if value:
                self.live.ensure_zoom_rect()
            self.zoomview.set_visible(value)
        elif key == "histogram":
            self.histogram.set_visible(value)

        elif key == "colorbar_visible":
            self.bar_box.set_visible(value)
        elif key == "plot_visible":
            self.plot.set_visible(value)
            if value:
                # give the plot about 30 % of the height when it appears
                GLib.idle_add(lambda: self.vpan.set_position(int(self.vpan.get_height() * 0.7)) and False)
        elif key == "tools_visible":
            self.tools.set_visible(value)
        elif key in ("palette", "dual_palette", "back_palette") and key in getattr(self, "pal_dd", {}):
            self._dd_select(self.pal_dd[key], value)
        self.live.queue_draw()

    def set_unit(self, u):
        if u == self.settings["temp_unit"]:
            return
        old = E.UNITS[self.settings["temp_unit"]]
        new = E.UNITS[u]
        s = self.settings
        for key in ("colorbar_manual_max", "colorbar_manual_min"):
            c = (s[key] - old[1]) / old[0]
            s[key] = round(c * new[0] + new[1], 1)
        s["temp_unit"] = u
        self.engine.plot = []
        if self.unit_btns[u].get_active() is False:
            self.unit_btns[u].set_active(True)
        self.log(f"Temperature unit set to {new[2]}.")

    def set_triggers_running(self, v):
        self.settings["triggers_running"] = v
        self.engine.reset_triggers()
        self.log("Periodic triggers started." if v else "Periodic triggers stopped.", "success" if v else "warning")

    def rotate(self, deg):
        eng = self.engine
        r = eng.latest
        self.settings["rotation"] = (self.settings["rotation"] + deg) % 360
        if r is None:
            return
        w, h = r.width, r.height
        cw = deg > 0

        def pt(x, y):
            return (h - 1 - y, x) if cw else (y, w - 1 - x)

        def rect(b):
            x, y, bw, bh = b
            return (h - y - bh, x, bh, bw) if cw else (y, w - x - bw, bh, bw)
        with eng.lock:
            eng.points = [pt(*p) for p in eng.points]
            eng.lines = [pt(l[0], l[1]) + pt(l[2], l[3]) for l in eng.lines]
            eng.rois = [rect(b) for b in eng.rois]
            if eng.dual_rect:
                eng.dual_rect = rect(eng.dual_rect)
            if eng.zoom_rect:
                eng.zoom_rect = rect(eng.zoom_rect)

    def toggle_run(self):
        eng = self.engine
        if not eng.source:
            return
        eng.running = not eng.running
        self.log("Live view running." if eng.running else "Live view stopped (press O for a single frame).")
        self._update_buttons()

    def single_frame(self):
        if self.engine.source and not self.engine.running:
            self.engine.single = True

    def choose_color(self, key):
        dlg = Gtk.ColorDialog(with_alpha=False)
        rgba = Gdk.RGBA()
        rgba.parse(self.settings["colors"][key])

        def done(d, res):
            try:
                c = d.choose_rgba_finish(res)
            except GLib.Error:
                return
            self.settings["colors"][key] = "#%02x%02x%02x" % (int(c.red * 255), int(c.green * 255), int(c.blue * 255))
        dlg.choose_rgba(self.win, rgba, None, done)

    def reload_palettes(self, select=None):
        names = palettes.names()
        for key, dd in self.pal_dd.items():
            cur = select if (select and key == "palette") else self.settings[key]
            if cur not in names:
                cur = "Iron"
            dd.set_model(Gtk.StringList.new(names))
            self._dd_select(dd, cur)
            self.settings[key] = cur

    def apply_emissivity(self, value, name):
        eng = self.engine
        if not eng.source:
            self.log("Connect a camera first to apply an emissivity value.", "warning")
            return
        eng.apply_config({"emissivity": value})
        self.log(f"Emissivity {value:.2f} ({name}) applied.", "success")

    def dialog_colorbar_range(self):
        s = self.settings
        unit = E.UNITS[s["temp_unit"]][2]
        dlg = Adw.AlertDialog(heading="Color Bar Temperature Range", body=f"Manual range in {unit}")
        grid = Gtk.Grid(column_spacing=12, row_spacing=8, halign=Gtk.Align.CENTER)
        hi = Gtk.SpinButton.new_with_range(-300, 3000, 0.1)
        hi.set_digits(1)
        hi.set_value(s["colorbar_manual_max"])
        lo = Gtk.SpinButton.new_with_range(-300, 3000, 0.1)
        lo.set_digits(1)
        lo.set_value(s["colorbar_manual_min"])
        grid.attach(Gtk.Label(label="Maximum", xalign=0), 0, 0, 1, 1)
        grid.attach(hi, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Minimum", xalign=0), 0, 1, 1, 1)
        grid.attach(lo, 1, 1, 1, 1)
        dlg.set_extra_child(grid)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("set", "Set Range")
        dlg.set_response_appearance("set", Adw.ResponseAppearance.SUGGESTED)

        def resp(d, r):
            if r != "set":
                return
            a, b = hi.get_value(), lo.get_value()
            if a <= b:
                self.log("The maximum must be higher than the minimum.", "warning")
                return
            s["colorbar_manual_max"], s["colorbar_manual_min"] = a, b
            s["colorbar_manual"] = True
            self.engine.bar_mode = "manual"
            self.bar_manual.set_active(True)
            self.colorbar.sync_mode()
        dlg.connect("response", resp)
        dlg.present(self.win)

    # ================================================================== connect / files
    def connect_camera(self, path=None):
        name = self.camera_dd.get_selected_item().get_string()
        if name in (C.SNAPSHOT_ANALYSIS, C.RECORDING_ANALYSIS) and path is None:
            self._open_file(name)
            return
        self.settings["camera_source"] = name
        try:
            src = sources.make_source(name, self.settings, path)
        except Exception as e:  # noqa: BLE001
            self.log(f"Cannot use {name}: {e}", "error")
            return
        self.btn_connect.set_sensitive(False)
        self._on_event("busy", "Connecting…")

        def work():
            try:
                self.engine.connect(src)
                err = None
            except sources.SourceError as e:
                err = str(e)
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
            GLib.idle_add(self._connected, err, src)
        threading.Thread(target=work, daemon=True).start()

    def _connected(self, err, src):
        self.btn_connect.set_sensitive(True)
        self._on_event("busy", None)
        if err:
            self.engine.source = None
            try:
                src.close()
            except Exception:  # noqa: BLE001
                pass
            self.log(err, "error")
        else:
            self.log(f"Connected: {src.name}", "success")
            self.engine.bar_mode = "manual" if self.settings["colorbar_manual"] else "auto"
            if self.settings["dual_palette_box"]:
                GLib.timeout_add(300, lambda: self.live.ensure_dual_rect() and False)
            if self.settings["split_view"]:
                GLib.timeout_add(300, lambda: self.live.ensure_zoom_rect() and False)
        self._update_buttons()
        return False

    def disconnect(self):
        if self.engine.recording:
            self.engine.stop_recording()
        self.engine.disconnect()
        with self.engine.lock:
            self.engine.dual_rect = self.engine.zoom_rect = None
        self._update_buttons()
        self.live.queue_draw()
        self.colorbar.queue_draw()
        self.log("Disconnected.")

    def _open_file(self, mode):
        snap = mode == C.SNAPSHOT_ANALYSIS
        dlg = Gtk.FileDialog(title="Open RAW Snapshot" if snap else "Open RAW Recording")
        f = Gtk.FileFilter()
        f.set_name("IRCAM RAW snapshot (PNG)" if snap else "IRCAM RAW recording (AVI)")
        f.add_pattern("*.png" if snap else "*.avi")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dlg.set_filters(filters)
        folder = self.settings["snapshot_dir"] if snap else self.settings["video_dir"]
        if os.path.isdir(folder):
            dlg.set_initial_folder(Gio.File.new_for_path(folder))

        def done(d, res):
            try:
                gf = d.open_finish(res)
            except GLib.Error:
                return
            if self.engine.source:
                self.disconnect()
            self.camera_dd.set_selected(C.source_names().index(mode))
            self.connect_camera(gf.get_path())
        dlg.open(self.win, None, done)

    # ================================================================== snapshot / recording
    def render_view(self):
        """Offscreen render of the live view, labels and colour bar (the processed snapshot)."""
        r = self.engine.latest
        if r is None:
            return None
        scale = max(1, round(720 / r.width))
        w, h = r.width * scale, r.height * scale
        bar_w = 118 if self.settings["colorbar_visible"] else 0
        surf = cairo.ImageSurface(cairo.FORMAT_RGB24, w + bar_w, h)
        cr = cairo.Context(surf)
        saved_mouse = self.live.mouse
        self.live.mouse = None
        self.live.render(cr, w, h, r)
        self.live.mouse = saved_mouse
        if bar_w:
            cr.translate(w, 0)
            cr.rectangle(0, 0, bar_w, h)
            cr.clip()
            self.colorbar.render(cr, bar_w, h, r)
        return surf

    def snapshot(self):
        eng = self.engine
        if eng.latest is None:
            return
        s = self.settings
        stamp = E.timestamp()
        out = Path(s["snapshot_dir"])
        saved = []
        try:
            out.mkdir(parents=True, exist_ok=True)
            if s["snapshot_include_view"]:
                surf = self.render_view()
                p = out / f"Snapshot_{stamp}.png"
                surf.write_to_png(str(p))
                saved.append(p.name)
            if s["snapshot_raw"]:
                p = eng.save_raw_snapshot()
                if p:
                    saved.append(p.name)
        except OSError as e:
            self.log(f"Snapshot failed: {e}", "error")
            return
        self.log(f"Snapshot saved in {out}: {', '.join(saved)}", "success")

    def _rec_toggled(self, active):
        if active:
            self.start_recording()
        else:
            self.engine.stop_recording()

    def _log_toggled(self, active):
        if active:
            self.engine.start_logging()
        else:
            self.engine.stop_logging()
        self._sync_toggle(self.btn_log, self.engine.logging)

    def start_recording(self):
        eng = self.engine
        if eng.recording:
            return
        eng.start_recording()
        self._sync_toggle(self.btn_rec, eng.recording)
        cmd = self.settings["screen_capture_cmd"].strip()
        if eng.recording and cmd:
            path = str(Path(self.settings["video_dir"]) / f"ScreenCapture_{E.timestamp()}.mp4")
            args = [a.replace("%f", path) for a in shlex.split(cmd)]
            try:
                self._screen_proc = subprocess.Popen(args)
                self.log(f"Screen capture started: {' '.join(args)}")
            except OSError as e:
                self.log(f"Screen capture command failed: {e}", "error")

    def _stop_screen_capture(self):
        p = self._screen_proc
        self._screen_proc = None
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)

    # ================================================================== windows
    def open_settings(self):
        from .settings import SettingsDialog
        SettingsDialog(self).present(self.win)

    def _window(self, attr, cls):
        w = getattr(self, attr)
        if w is None:
            from . import windows
            w = getattr(windows, cls)(self)
            w.set_transient_for(self.win) if attr == "emis_win" else None
            setattr(self, attr, w)
            if attr in ("emis_win", "palette_win"):
                w.connect("close-request", lambda *a: setattr(self, attr, None) or False)
        w.present()
        return w

    def open_statistics(self):
        w = self._window("stats_win", "StatisticsWindow")
        if self.settings["statistics_on_top"]:
            w.set_transient_for(self.win)

    def open_emissivity(self):
        self._window("emis_win", "EmissivityWindow")

    def open_surface(self):
        self._window("surface_win", "SurfaceWindow")

    def open_palette_creator(self):
        self._window("palette_win", "PaletteCreator")

    def show_shortcuts(self):
        dlg = Adw.Dialog(title="Keyboard Shortcuts", content_width=520, content_height=640)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        grp = Adw.PreferencesGroup(margin_start=12, margin_end=12, margin_top=6, margin_bottom=12)
        for k, d in HOTKEYS:
            row = Adw.ActionRow(title=d)
            lbl = Gtk.Label(label=k)
            lbl.add_css_class("monospace")
            row.add_prefix(lbl)
            grp.add(row)
        tv.set_content(Gtk.ScrolledWindow(child=grp))
        dlg.set_child(tv)
        dlg.present(self.win)

    def show_about(self):
        a = Adw.AboutDialog(application_name="IRCAM Thermal Viewer for Linux", application_icon="camera-video",
                            developer_name="Linux port of OpenRMH-IRCAM", version=__import__("ircam").__version__,
                            website="https://github.com/Cyclotronic/OpenRMH-IRCAM",
                            comments="Native GTK4 port of the IRCAM Thermal Viewer for InfiRay, Thermal Master, "
                                     "TOPDON, HTI, UNI-T and other USB thermal cameras.",
                            license_type=Gtk.License.MIT_X11,
                            copyright="© 2022–2026 Rune Mark Hansen, © 2026 Cyclotronic, © 2026 Linux port")
        a.present(self.win)

    def _on_close(self, *a):
        self.settings["window_size"] = [self.win.get_width(), self.win.get_height()]
        self._stop_screen_capture()
        self.engine.disconnect()
        self.settings.save()
        for w in (self.stats_win, self.surface_win, self.emis_win, self.palette_win):
            if w:
                w.close()
        return False


def main(argv):
    if "-h" in argv or "--help" in argv:
        print("usage: ircam-linux [--demo] [SnapShotRAW_*.png | RAWRecording_*.avi]")
        return 0
    demo = "--demo" in argv
    files = [a for a in argv[1:] if not a.startswith("-") and os.path.isfile(a)]
    app = IrcamApp(demo=demo)
    if files:
        app.open_path = os.path.abspath(files[0])
    return app.run(argv[:1])
