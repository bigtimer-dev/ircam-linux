"""Settings dialog: the Linux counterpart of the Windows app's settings tabs
(camera configuration, calibration, snapshot/CSV, recording, 2D plot data sets,
data logging, temperature alarms and periodic triggers)."""
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import cameras as C
from .. import engine as E
from .. import v4l2

DELIMS = [(",", "Comma ( , )"), (";", "Semicolon ( ; )"), (":", "Colon ( : )"), (" ", "Space"), ("\t", "Tab")]


def _combo(title, items, current, on_change, subtitle=None):
    row = Adw.ComboRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_model(Gtk.StringList.new([str(i) for i in items]))
    if current in items:
        row.set_selected(items.index(current))
    row.connect("notify::selected", lambda r, p: on_change(items[r.get_selected()]))
    return row


def _spin(title, lo, hi, step, value, on_change, digits=1, subtitle=None):
    row = Adw.SpinRow.new_with_range(lo, hi, step)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_digits(digits)
    row.set_value(value)
    row.connect("notify::value", lambda r, p: on_change(r.get_value()))
    return row


def _switch(title, value, on_change, subtitle=None):
    row = Adw.SwitchRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_active(value)
    row.connect("notify::active", lambda r, p: on_change(r.get_active()))
    return row


def _color_button(hexcol, on_change):
    rgba = Gdk.RGBA()
    rgba.parse(hexcol)
    b = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), rgba=rgba, valign=Gtk.Align.CENTER)

    def changed(btn, p):
        c = btn.get_rgba()
        on_change("#%02x%02x%02x" % (int(c.red * 255), int(c.green * 255), int(c.blue * 255)))
    b.connect("notify::rgba", changed)
    return b


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, app):
        super().__init__(title="Settings")
        self.app = app
        self.s = app.settings
        self.set_search_enabled(True)
        self.add(self._camera_page())
        self.add(self._calibration_page())
        self.add(self._files_page())
        self.add(self._plot_page())
        self.add(self._alarms_page())
        self.add(self._triggers_page())
        self.connect("closed", lambda d: app.settings.save())

    def _set(self, key, value):
        self.s[key] = value

    def _folder_row(self, title, key):
        row = Adw.ActionRow(title=title, subtitle=self.s[key])
        b = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Choose folder")

        def pick(btn):
            dlg = Gtk.FileDialog(title=title, initial_folder=Gio.File.new_for_path(self.s[key]))

            def done(d, res):
                try:
                    f = d.select_folder_finish(res)
                except GLib.Error:
                    return
                self.s[key] = f.get_path()
                row.set_subtitle(f.get_path())
            dlg.select_folder(self.app.win, None, done)
        b.connect("clicked", pick)
        row.add_suffix(b)
        return row

    # --------------------------------------------------------------- camera
    def _camera_page(self):
        s = self.s
        page = Adw.PreferencesPage(title="Camera", icon_name="camera-video-symbolic")
        g = Adw.PreferencesGroup(title="Video Device",
                                 description="With “Automatic” the camera is found by its V4L2 name or USB id.")
        devs = v4l2.list_devices()
        items = ["auto"] + [d.path for d in devs]
        labels = ["Automatic"] + [d.label() for d in devs]
        row = Adw.ComboRow(title="Device", model=Gtk.StringList.new(labels))
        if s["v4l2_device"] in items:
            row.set_selected(items.index(s["v4l2_device"]))
        row.connect("notify::selected", lambda r, p: self._set("v4l2_device", items[r.get_selected()]))
        g.add(row)
        page.add(g)

        g = Adw.PreferencesGroup(title="Temperature Units")
        units = ["C", "F", "K"]
        g.add(_combo("Display unit", ["Celsius (°C)", "Fahrenheit (°F)", "Kelvin (K)"],
                     ["Celsius (°C)", "Fahrenheit (°F)", "Kelvin (K)"][units.index(s["temp_unit"])],
                     lambda v: self.app.set_unit(units[["Celsius (°C)", "Fahrenheit (°F)", "Kelvin (K)"].index(v)])))
        page.add(g)

        g = Adw.PreferencesGroup(title="Temperature Configuration",
                                 description="Object parameters used for the temperature calculation "
                                             "(written to the camera for InfiRay T2/T3 models).")
        cam = self.app.engine.cam
        cur = {
            "temp_correction": cam.temp_correction if cam else s["temp_correction"],
            "ambient": cam.ambient if cam else C.DEFAULT_AMBIENT,
            "reflected": cam.reflected if cam else C.DEFAULT_REFLECTED,
            "humidity": cam.humidity if cam else C.DEFAULT_HUMIDITY,
            "emissivity": cam.emissivity if cam else C.DEFAULT_EMISSIVITY,
            "distance": cam.distance if cam else C.DEFAULT_DISTANCE,
        }
        self.cfg = dict(cur)
        setc = lambda k: (lambda v: self.cfg.__setitem__(k, v))
        rows = [
            _spin("Temperature correction (°C)", -50, 50, 0.1, cur["temp_correction"], setc("temp_correction"), 2),
            _spin("Ambient temperature (°C)", -40, 200, 0.1, cur["ambient"], setc("ambient"), 1),
            _spin("Reflected temperature (°C)", -40, 200, 0.1, cur["reflected"], setc("reflected"), 1),
            _spin("Relative humidity (0–1)", 0, 1, 0.01, cur["humidity"], setc("humidity"), 2),
            _spin("Emissivity", 0.01, 1, 0.01, cur["emissivity"], setc("emissivity"), 2),
            _spin("Distance (m)", 0, 1000, 1, cur["distance"], setc("distance"), 0),
        ]
        self.cfg_rows = dict(zip(cur.keys(), rows))
        for r in rows:
            g.add(r)
        bx = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8)
        dflt = Gtk.Button(label="Defaults")
        dflt.connect("clicked", self._cfg_defaults)
        apply = Gtk.Button(label="Set Configuration")
        apply.add_css_class("suggested-action")
        apply.connect("clicked", self._cfg_apply)
        bx.append(dflt)
        bx.append(apply)
        g.add(bx)
        page.add(g)
        return page

    def _cfg_defaults(self, b):
        for k, v in (("temp_correction", 0.0), ("ambient", C.DEFAULT_AMBIENT), ("reflected", C.DEFAULT_REFLECTED),
                     ("humidity", C.DEFAULT_HUMIDITY), ("emissivity", C.DEFAULT_EMISSIVITY), ("distance", C.DEFAULT_DISTANCE)):
            self.cfg_rows[k].set_value(v)

    def _cfg_apply(self, b):
        self.s["temp_correction"] = self.cfg["temp_correction"]
        if not self.app.engine.source:
            self.app.log("Connect a camera to set its configuration (temperature correction saved).", "warning")
            return
        vals = dict(self.cfg)
        vals["distance"] = float(int(vals["distance"]))
        self.app.engine.apply_config(vals)

    # --------------------------------------------------------------- calibration
    def _calibration_page(self):
        s = self.s
        page = Adw.PreferencesPage(title="Calibration", icon_name="view-refresh-symbolic")
        g = Adw.PreferencesGroup(title="Automatic Calibration",
                                 description="Shutter / non-uniformity calibration on a timer or when the camera "
                                             "temperature drifts. Supported by InfiRay T2/T3 (V1 and V2) and Thermal Master P3.")
        g.add(_switch("Periodic calibration", s["auto_cal"], self._auto_cal))
        g.add(_spin("Period (minutes)", 0.1, 600, 0.5, s["auto_cal_period"], lambda v: self._set("auto_cal_period", v)))
        g.add(_switch("Calibrate on temperature drift", s["drift_cal"], lambda v: self._set("drift_cal", v)))
        g.add(_spin("Drift threshold (degrees)", 0.1, 20, 0.1, s["drift_cal_value"], lambda v: self._set("drift_cal_value", v)))
        page.add(g)
        g = Adw.PreferencesGroup()
        b = Gtk.Button(label="Calibrate Now", halign=Gtk.Align.START)
        b.connect("clicked", lambda x: self.app.engine.calibrate())
        g.add(b)
        page.add(g)
        return page

    def _auto_cal(self, v):
        import time
        self.s["auto_cal"] = v
        self.app.engine.auto_cal_next = time.monotonic() + self.s["auto_cal_period"] * 60

    # --------------------------------------------------------------- files
    def _files_page(self):
        s = self.s
        page = Adw.PreferencesPage(title="Snapshots & Video", icon_name="camera-photo-symbolic")
        g = Adw.PreferencesGroup(title="Snapshots")
        g.add(self._folder_row("Snapshot folder", "snapshot_dir"))
        g.add(_switch("Save the live view image", s["snapshot_include_view"], lambda v: self._set("snapshot_include_view", v),
                      "Snapshot_[time].png, the window content including labels"))
        g.add(_switch("Save RAW snapshot data", s["snapshot_raw"], lambda v: self._set("snapshot_raw", v),
                      "SnapShotRAW_[time].png, can be reopened in Snapshot Analysis Mode"))
        g.add(_combo("Full frame data delimiter", [d[1] for d in DELIMS],
                     dict(DELIMS)[s["frame_csv_delimiter"]],
                     lambda v: self._set("frame_csv_delimiter", [d[0] for d in DELIMS if d[1] == v][0])))
        page.add(g)
        g = Adw.PreferencesGroup(title="Video Recording")
        g.add(self._folder_row("Video folder", "video_dir"))
        g.add(_spin("Recording frame rate", 1, 30, 1, s["record_fps"], lambda v: self._set("record_fps", v), 0))
        g.add(_switch("Record RAW data", s["record_raw"], lambda v: self._set("record_raw", v),
                      "RAWRecording_[time].avi, can be reopened in Recording Analysis Mode"))
        row = Adw.EntryRow(title="Screen capture command (optional, e.g. wf-recorder -f %f)")
        row.set_text(s["screen_capture_cmd"])
        row.connect("changed", lambda r: self._set("screen_capture_cmd", r.get_text()))
        g.add(row)
        page.add(g)
        return page

    # --------------------------------------------------------------- plot / logging
    def _plot_page(self):
        s = self.s
        page = Adw.PreferencesPage(title="Plot & Logging", icon_name="utilities-system-monitor-symbolic")
        g = Adw.PreferencesGroup(title="2D Plot Data Sets", description="The enabled data sets are also the data logging columns.")
        names = E.data_source_names(include_drift=True)
        for i, ps in enumerate(s["plot_sets"]):
            exp = Adw.ExpanderRow(title=f"Data set {i + 1}", subtitle=ps["source"])
            exp.set_show_enable_switch(True)
            exp.set_enable_expansion(ps["enabled"])
            exp.connect("notify::enable-expansion", lambda r, p, d=ps: d.__setitem__("enabled", r.get_enable_expansion()))

            def src_changed(v, d=ps, e=exp):
                d["source"] = v
                e.set_subtitle(v)
            exp.add_row(_combo("Data source", names, ps["source"], src_changed))
            exp.add_row(_spin("Line width", 0.5, 8, 0.5, ps["width"], lambda v, d=ps: d.__setitem__("width", v)))
            cr = Adw.ActionRow(title="Line colour")
            cr.add_suffix(_color_button(ps["color"], lambda v, d=ps: d.__setitem__("color", v)))
            exp.add_row(cr)
            g.add(exp)
        page.add(g)
        g = Adw.PreferencesGroup(title="Data Logging", description="DataLogSession_[time].txt with Sample, Time(ms) and one column per enabled data set.")
        g.add(self._folder_row("Log folder", "log_dir"))
        g.add(_spin("Sample interval (s)", 0.05, 3600, 0.05, s["log_interval"], lambda v: self._set("log_interval", v), 2))
        g.add(_spin("Duration hours", 0, 999, 1, s["log_hours"], lambda v: self._set("log_hours", int(v)), 0))
        g.add(_spin("Duration minutes", 0, 59, 1, s["log_minutes"], lambda v: self._set("log_minutes", int(v)), 0))
        g.add(_spin("Duration seconds", 0, 59, 1, s["log_seconds"], lambda v: self._set("log_seconds", int(v)), 0,
                    "A total of 0 logs until stopped"))
        g.add(_combo("Delimiter", [d[1] for d in DELIMS], dict(DELIMS)[s["log_delimiter"]],
                     lambda v: self._set("log_delimiter", [d[0] for d in DELIMS if d[1] == v][0])))
        page.add(g)
        return page

    # --------------------------------------------------------------- alarms
    def _alarms_page(self):
        s = self.s
        page = Adw.PreferencesPage(title="Alarms", icon_name="dialog-warning-symbolic")
        g = Adw.PreferencesGroup(title="General")
        g.add(_switch("Alarm sound", s["alarm_sound"], lambda v: self._set("alarm_sound", v)))
        g.add(_switch("Alarm trigger actions", s["alarm_events"], self._alarm_events,
                      "Run the alarm's action when it triggers (once, until re-armed)"))
        g.add(_spin("Minimum time between actions (s)", 0.1, 3600, 1, s["alarm_event_interval"],
                    lambda v: self._set("alarm_event_interval", v)))
        page.add(g)
        g = Adw.PreferencesGroup(title="Temperature Alarms")
        names = E.data_source_names()
        for i, a in enumerate(s["alarms"]):
            exp = Adw.ExpanderRow(title=f"Alarm {i + 1}", subtitle=f"{a['source']} · {a['type']}")
            exp.set_show_enable_switch(True)
            exp.set_enable_expansion(a["enabled"])
            exp.connect("notify::enable-expansion", lambda r, p, d=a: d.__setitem__("enabled", r.get_enable_expansion()))
            upd = lambda e=exp, d=a: e.set_subtitle(f"{d['source']} · {d['type']}")
            exp.add_row(_combo("Source", names, a["source"], lambda v, d=a, u=upd: (d.__setitem__("source", v), u())))
            exp.add_row(_combo("Type", E.ALARM_TYPES, a["type"], lambda v, d=a, u=upd: (d.__setitem__("type", v), u()),
                               "Above: ≥ high · Below: ≤ low · Window: outside low…high"))
            exp.add_row(_spin("Low threshold", -273, 3000, 0.5, a["low"], lambda v, d=a: d.__setitem__("low", v)))
            exp.add_row(_spin("High threshold", -273, 3000, 0.5, a["high"], lambda v, d=a: d.__setitem__("high", v)))
            exp.add_row(_combo("Trigger action", E.EVENTS, a["action"], lambda v, d=a, k=i: (d.__setitem__("action", v), self.app.engine.reset_alarm_event(k))))
            g.add(exp)
        page.add(g)
        return page

    def _alarm_events(self, v):
        self.s["alarm_events"] = v
        for i in range(5):
            self.app.engine.reset_alarm_event(i)

    # --------------------------------------------------------------- triggers
    def _triggers_page(self):
        s = self.s
        page = Adw.PreferencesPage(title="Triggers", icon_name="alarm-symbolic")
        g = Adw.PreferencesGroup(title="Periodic Triggers",
                                 description="Run events on a timer while the triggers are started (hotkey P).")
        g.add(_switch("Triggers running", s["triggers_running"], lambda v: self.app.set_triggers_running(v)))
        page.add(g)
        g = Adw.PreferencesGroup()
        for i, t in enumerate(s["triggers"]):
            exp = Adw.ExpanderRow(title=f"Trigger {i + 1}", subtitle=t["event"])
            exp.set_show_enable_switch(True)
            exp.set_enable_expansion(t["enabled"])
            exp.connect("notify::enable-expansion", lambda r, p, d=t: d.__setitem__("enabled", r.get_enable_expansion()))
            exp.add_row(_combo("Event", E.EVENTS, t["event"], lambda v, d=t, e=exp: (d.__setitem__("event", v), e.set_subtitle(v))))
            exp.add_row(_spin("Period (s)", 0.1, 86400, 1, t["period"], lambda v, d=t: d.__setitem__("period", v)))
            exp.add_row(_switch("Run once", t["once"], lambda v, d=t: d.__setitem__("once", v)))
            g.add(exp)
        page.add(g)
        return page
