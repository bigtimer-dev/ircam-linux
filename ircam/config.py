"""Session settings, saved between runs (the Linux counterpart of
RMH_Application_SaveSession.cpp)."""
import copy
import json
import os
from pathlib import Path


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    d = Path(base) / "ircam-linux"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pictures():
    for env in ("XDG_PICTURES_DIR",):
        if os.environ.get(env):
            return os.environ[env]
    p = Path.home() / "Pictures"
    return str(p if p.is_dir() else Path.home())


def _videos():
    p = Path.home() / "Videos"
    return str(p if p.is_dir() else Path.home())


PLOT_COLORS = ["#ff4040", "#40a0ff", "#40ff70", "#ffd040", "#ff60ff",
               "#40ffff", "#ff9a40", "#b080ff", "#ffffff", "#a0a0a0"]

DEFAULTS = {
    "camera_source": "InfiRay Or Thermal Master P2Pro",
    "v4l2_device": "auto",
    "wireless_host": "p2pro.local",   # wireless P2 Pro bridge: host, host:port or IP
    "temp_unit": "C",
    "temp_correction": 0.0,
    # palettes
    "palette": "Iron",
    "dual_palette": "Gray",
    "back_palette": "Gray",
    "invert_palette": False,
    "invert_dual": False,
    "invert_back": False,
    "adapt_full_palette": True,
    "palette_range_live": True,     # live view palette follows the colour bar arrows
    "palette_range_dual": False,    # dual palette follows the colour bar arrows
    # colour bar
    "colorbar_manual": False,
    "colorbar_manual_max": 40.0,
    "colorbar_manual_min": 20.0,
    "colorbar_ticks": 6,
    "colorbar_wheel_step": 1.0,
    "colorbar_center_arrow": True,
    "colorbar_visible": True,
    "plot_visible": False,       # 2D temperature plot under the live view
    # histogram
    "histogram": False,
    "histogram_bins": 64,
    "histogram_source": "frame",
    "histogram_dual_palette": False,
    "histogram_ranged_palette": True,
    # live view tools
    "track_max": True,
    "track_min": True,
    "track_center": True,
    "track_cursor": False,
    "dual_palette_box": False,
    "ultra_resolution": False,
    "enhanced_resolution": True,
    "sharpening": False,
    "sharpen_strength": 1.5,
    "sharpen_sigma": 2.0,
    "focus_assist": False,
    "fixed_aspect": True,
    "rotation": 0,
    "wheel_rotates": False,
    "label_background": True,
    "split_view": False,
    "tools_visible": True,
    "render_order": ["lines", "rois", "points"],
    "colors": {
        "max": "#ff3030", "min": "#30a0ff", "center": "#ffffff", "cursor": "#ffff60",
        "points": "#ffffff", "lines": "#40ff40", "rois": "#ffd040", "dual": "#ff60ff",
        "zoom": "#00e0ff",
    },
    # snapshot / CSV
    "snapshot_dir": _pictures(),
    "snapshot_include_view": True,
    "snapshot_raw": True,
    "frame_csv_delimiter": ",",
    # recording
    "video_dir": _videos(),
    "record_raw": True,
    "record_fps": 25.0,
    "screen_capture_cmd": "",
    # 2D plot data sets: 10
    "plot_sets": [
        {"source": s, "enabled": i < 3, "width": 2.0, "color": PLOT_COLORS[i]}
        for i, s in enumerate(["Maximum Temp", "Minimum Temp", "Average Temp", "Center Temp",
                               "Temp Point 1", "Temp Point 2", "Temp Point 3", "Temp Point 4",
                               "Temp Point 5", "Temp Point 6"])
    ],
    "plot_ticks_x": 10,
    "plot_ticks_y": 8,
    "plot_border": True,
    "plot_grid": True,
    "plot_seconds": 60,
    # data logging
    "log_dir": str(Path.home()),
    "log_interval": 1.0,
    "log_hours": 0,
    "log_minutes": 10,
    "log_seconds": 0,
    "log_delimiter": ",",
    # alarms: 5
    "alarms": [
        {"enabled": False, "source": "Maximum Temp", "type": "Above", "low": 10.0, "high": 60.0,
         "action": "None"} for _ in range(5)
    ],
    "alarm_sound": False,
    "alarm_events": False,
    "alarm_event_interval": 10.0,
    # periodic triggers: 5
    "triggers": [
        {"enabled": False, "event": "None", "period": 60.0, "once": False} for _ in range(5)
    ],
    "triggers_running": False,
    # calibration
    "auto_cal_period": 5.0,   # minutes
    "auto_cal": False,
    "drift_cal_value": 1.0,   # degrees
    "drift_cal": False,
    # 3D surface plot
    "surface_mode": "fill",
    "surface_point_size": 2.0,
    "surface_z_scale": 1.0,
    # misc
    "statistics_on_top": False,
    "window_size": [1400, 900],
}


class Settings(dict):
    path = None

    @classmethod
    def load(cls):
        s = cls(copy.deepcopy(DEFAULTS))
        s.path = config_dir() / "session.json"
        try:
            data = json.loads(s.path.read_text())
            for k, v in data.items():
                if k in DEFAULTS and type(v) is type(DEFAULTS[k]) or (
                        k in DEFAULTS and isinstance(DEFAULTS[k], float) and isinstance(v, int)):
                    if isinstance(v, dict):
                        merged = copy.deepcopy(DEFAULTS[k])
                        merged.update(v)
                        v = merged
                    if isinstance(v, list) and k in ("plot_sets", "alarms", "triggers"):
                        base = copy.deepcopy(DEFAULTS[k])
                        for i, item in enumerate(v[: len(base)]):
                            if isinstance(item, dict):
                                base[i].update(item)
                        v = base
                    s[k] = v
        except (OSError, ValueError):
            pass
        return s

    def save(self):
        try:
            self.path.write_text(json.dumps(self, indent=1))
        except OSError:
            pass
