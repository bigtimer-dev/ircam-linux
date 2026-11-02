#!/usr/bin/env python3
"""ircam-netd: WiFi manager and setup page for the wireless P2 Pro bridge (Raspberry Pi).

- Joins a saved WiFi network (NetworkManager). When none connects within the grace period,
  it starts the hotspot "P2Pro-xxxx" (10.42.0.1). Streaming works in both modes.
- While on the hotspot and nobody is connected to it, it periodically looks for saved
  networks again (e.g. when you come home).
- Serves a setup page on port 80: add / forget networks, hotspot name and password, device
  name, status. On the hotspot it acts as a captive portal so phones open it automatically.
- Switching to a new network is safe: if it doesn't connect within 30 s, the hotspot comes back
  and the page shows why.

    sudo python3 ircam_netd.py                      # on the Pi (needs root for nmcli)
    python3 ircam_netd.py --fake --port 8080        # simulated WiFi, for trying the page
"""
import argparse
import http.server
import json
import logging
import os
import re
import secrets
import socket
import string
import subprocess
import threading
import time
from pathlib import Path

__version__ = "0.1.0"
log = logging.getLogger("ircam-netd")

AP_IP = "10.42.0.1"
AP_NET = "10.42.0."
HOTSPOT_CON = "p2pro-hotspot"
PROFILE_PREFIX = "wifi-"
CONFIG_PATH = "/etc/ircam-bridge/netd.json"
STATUS_PATH = "/run/ircam-bridge/status.json"
HOLD_S = 300                    # a forced hotspot blocks the return to saved networks this long
HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")


def default_config():
    return {"hotspot_ssid": "P2Pro-" + mac_suffix(), "hotspot_password": gen_password(),
            "pin": "".join(secrets.choice(string.digits) for _ in range(6)),
            "iface": "wlan0", "grace_s": 30, "idle_retry_s": 120, "retry_every_s": 300,
            "connect_timeout_s": 30}


def gen_password():
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"          # no look-alike characters
    return "".join(secrets.choice(alphabet) for _ in range(10))


def mac_suffix(iface="wlan0"):
    try:
        return Path(f"/sys/class/net/{iface}/address").read_text().strip().replace(":", "")[-4:].upper()
    except OSError:
        return secrets.token_hex(2).upper()


def load_config(path):
    cfg = default_config()
    p = Path(path)
    try:
        cfg.update(json.loads(p.read_text()))
    except FileNotFoundError:
        save_config(path, cfg)
    except (OSError, ValueError) as e:
        log.warning("config %s unreadable (%s); using defaults", path, e)
    return cfg


def save_config(path, cfg):
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except OSError as e:
        log.error("cannot save %s: %s", path, e)


def valid_ssid(s):
    return isinstance(s, str) and 1 <= len(s.encode()) <= 32 and "\n" not in s and "\0" not in s


def valid_psk(s):
    return isinstance(s, str) and (s == "" or 8 <= len(s) <= 63) and "\n" not in s and "\0" not in s


# ------------------------------------------------------------------------------ NetworkManager
def _split_terse(line):
    """Split one line of `nmcli -t` output (':' separated, '\\:' and '\\\\' escaped)."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


class NM:
    """Thin nmcli wrapper. Every argument is passed as a list item (no shell)."""

    def __init__(self, iface="wlan0"):
        self.iface = iface

    def _run(self, *args, timeout=40):
        try:
            r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            return 1, "timed out"
        except FileNotFoundError:
            return 1, "nmcli not found"

    def state(self):
        rc, out = self._run("-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "dev", "show", self.iface)
        st = {"connected": False, "connection": None, "ip": None, "ssid": None, "signal": None}
        if rc:
            return st
        for line in out.splitlines():
            key, _, val = line.partition(":")
            if key == "GENERAL.STATE":
                st["connected"] = val.startswith("100")
            elif key == "GENERAL.CONNECTION":
                st["connection"] = val or None
            elif key.startswith("IP4.ADDRESS") and not st["ip"]:
                st["ip"] = val.split("/")[0]
        if st["connected"] and st["connection"] != HOTSPOT_CON:
            rc, out = self._run("-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi", "list", "--rescan", "no")
            for line in out.splitlines():
                f = _split_terse(line)
                if len(f) >= 3 and f[0] == "yes":
                    st["ssid"], st["signal"] = f[1], int(f[2] or 0)
                    break
        return st

    def scan(self, rescan=True):
        rc, out = self._run("-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list",
                            "ifname", self.iface, "--rescan", "yes" if rescan else "no", timeout=30)
        nets = {}
        for line in out.splitlines() if rc == 0 else []:
            f = _split_terse(line)
            if len(f) < 3 or not f[0]:
                continue
            ssid, sig, sec = f[0], int(f[1] or 0), f[2]
            if ssid not in nets or nets[ssid]["signal"] < sig:
                nets[ssid] = {"ssid": ssid, "signal": sig, "secure": bool(sec and sec != "--"), "security": sec}
        return sorted(nets.values(), key=lambda n: -n["signal"])

    def saved(self):
        rc, out = self._run("-t", "-f", "NAME,TYPE", "con", "show")
        res = []
        for line in out.splitlines() if rc == 0 else []:
            f = _split_terse(line)
            if len(f) < 2 or f[1] != "802-11-wireless" or f[0] == HOTSPOT_CON:
                continue
            rc2, o2 = self._run("-g", "802-11-wireless.ssid,connection.autoconnect-priority,802-11-wireless.mode",
                                "con", "show", f[0])
            parts = _split_terse(o2.splitlines()[0]) if rc2 == 0 and o2 else ["", "0", ""]
            if len(parts) >= 3 and parts[2] == "ap":
                continue
            res.append({"name": f[0], "ssid": parts[0] or f[0], "priority": int(parts[1] or 0)})
        return sorted(res, key=lambda n: -n["priority"])

    def make_hotspot(self, ssid, password):
        self._run("con", "delete", HOTSPOT_CON)
        rc, out = self._run("con", "add", "type", "wifi", "ifname", self.iface, "con-name", HOTSPOT_CON,
                            "autoconnect", "no", "ssid", ssid, "mode", "ap",
                            "802-11-wireless.band", "bg", "ipv4.method", "shared", "ipv6.method", "ignore",
                            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password,
                            "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp",
                            # PMF off: with "optional" the Pi's brcmfmac AP advertises PMF/WPA3 in its
                            # beacon but not in the handshake, and clients drop with reason 17
                            # (IE_DIFFERENT). Plain WPA2-PSK works with every phone and laptop.
                            "wifi-sec.pmf", "disable")
        return rc == 0, out

    def up(self, name, timeout=30):
        rc, out = self._run("--wait", str(timeout), "con", "up", name, timeout=timeout + 10)
        return rc == 0, out

    def down(self, name):
        return self._run("con", "down", name)[0] == 0

    def add_network(self, ssid, psk, hidden=False):
        name = PROFILE_PREFIX + ssid
        self._run("con", "delete", name)
        args = ["con", "add", "type", "wifi", "ifname", self.iface, "con-name", name, "ssid", ssid,
                "connection.autoconnect", "yes", "connection.autoconnect-priority", "10"]
        if psk:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
        if hidden:
            args += ["802-11-wireless.hidden", "yes"]
        rc, out = self._run(*args)
        return (name if rc == 0 else None), out

    def delete(self, name):
        return self._run("con", "delete", name)[0] == 0

    def stations(self):
        try:
            r = subprocess.run(["iw", "dev", self.iface, "station", "dump"], capture_output=True, text=True, timeout=5)
            return r.stdout.count("Station ")
        except (OSError, subprocess.TimeoutExpired):
            return 0

    def set_hostname(self, name):
        subprocess.run(["hostnamectl", "set-hostname", name], check=False)
        subprocess.run(["systemctl", "restart", "avahi-daemon"], check=False)


class FakeNM:
    """Simulated WiFi for trying the page on a PC: any network accepts 'password123'."""

    def __init__(self, iface="wlan0"):
        self.iface = iface
        self.air = [{"ssid": "HomeRouter", "signal": 82, "secure": True, "security": "WPA2"},
                    {"ssid": "Workshop 2.4G", "signal": 61, "secure": True, "security": "WPA2 WPA3"},
                    {"ssid": "Neighbor's WiFi", "signal": 35, "secure": True, "security": "WPA2"},
                    {"ssid": "CoffeeShop Free", "signal": 28, "secure": False, "security": ""}]
        self.profiles = {}            # name -> {ssid, psk, priority}
        self.active = None
        self.hotspot = None
        self.lock = threading.Lock()

    def state(self):
        a = self.active
        if a == HOTSPOT_CON:
            return {"connected": True, "connection": a, "ip": AP_IP, "ssid": None, "signal": None}
        if a:
            ssid = self.profiles[a]["ssid"]
            sig = next((n["signal"] for n in self.air if n["ssid"] == ssid), 50)
            return {"connected": True, "connection": a, "ip": "192.168.1.77", "ssid": ssid, "signal": sig}
        return {"connected": False, "connection": None, "ip": None, "ssid": None, "signal": None}

    def scan(self, rescan=True):
        if rescan:
            time.sleep(1.0)
        return list(self.air)

    def saved(self):
        return sorted(({"name": k, "ssid": v["ssid"], "priority": v["priority"]} for k, v in self.profiles.items()),
                      key=lambda n: -n["priority"])

    def make_hotspot(self, ssid, password):
        self.hotspot = (ssid, password)
        return True, ""

    def up(self, name, timeout=30):
        time.sleep(1.5)
        if name == HOTSPOT_CON:
            self.active = name
            return True, ""
        p = self.profiles.get(name)
        net = next((n for n in self.air if p and n["ssid"] == p["ssid"]), None)
        if not net:
            return False, "Error: No network with SSID found."
        if net["secure"] and p["psk"] != "password123":
            return False, "Error: Connection activation failed: Secrets were required, but not provided."
        self.active = name
        return True, ""

    def down(self, name):
        if self.active == name:
            self.active = None
        return True

    def add_network(self, ssid, psk, hidden=False):
        name = PROFILE_PREFIX + ssid
        self.profiles[name] = {"ssid": ssid, "psk": psk, "priority": 10}
        return name, ""

    def delete(self, name):
        if self.active == name:
            self.active = None
        return self.profiles.pop(name, None) is not None

    def stations(self):
        return 1

    def set_hostname(self, name):
        pass


# ------------------------------------------------------------------------------ manager
def classify_error(msg):
    m = (msg or "").lower()
    if "secrets were required" in m or "802-1x" in m or "psk" in m or "authentication" in m:
        return "Wrong password (the network rejected it)."
    if "no network with ssid" in m or "not found" in m:
        return "Network not found (out of range, or the name is wrong)."
    if "timed out" in m or "timeout" in m:
        return "The network did not answer in time (weak signal?)."
    return "Could not connect: " + (msg.strip().splitlines()[-1][:160] if msg.strip() else "unknown error")


class Manager:
    def __init__(self, nm, cfg, cfg_path, status_path=STATUS_PATH):
        self.nm, self.cfg, self.cfg_path, self.status_path = nm, cfg, cfg_path, status_path
        self.lock = threading.RLock()         # serializes all radio changes
        self.mode = "starting"                 # starting | searching | client | hotspot | switching
        self.message = ""
        self.last_error = None
        self.hold_until = 0.0                  # "switch to hotspot now" keeps it until then
        self.scan_cache, self.scan_time = [], 0.0
        self.since_disconnected = None
        self.boot = time.monotonic()
        self.idle_since = None
        self.last_retry = time.monotonic()
        self.st = {}
        self.alive = True

    # radio actions (call with self.lock held) -----------------------------------------
    def _scan(self, rescan=True):
        nets = self.nm.scan(rescan)
        if nets or rescan:
            own = self.cfg["hotspot_ssid"]
            self.scan_cache = [n for n in nets if n["ssid"] != own]
            self.scan_time = time.time()
        return self.scan_cache

    def _start_hotspot(self, reason):
        log.info("starting hotspot %s (%s)", self.cfg["hotspot_ssid"], reason)
        if self.st.get("connection") != HOTSPOT_CON:
            self._scan(True)                  # remember what's around for the setup page
        ok, out = self.nm.make_hotspot(self.cfg["hotspot_ssid"], self.cfg["hotspot_password"])
        if ok:
            ok, out = self.nm.up(HOTSPOT_CON, 20)
        if not ok:
            log.error("hotspot failed: %s", out)
            self.message = "Hotspot failed to start: " + out[-160:]
        self.mode = "hotspot" if ok else "searching"
        self.idle_since = None
        self.last_retry = time.monotonic()
        self.st = self.nm.state()

    def _try_saved(self, visible):
        """In hotspot mode: try saved networks that are in range. True when one connected."""
        saved = [s for s in self.nm.saved() if s["ssid"] in visible]
        for s in saved:
            log.info("trying saved network %s", s["ssid"])
            self.mode, self.message = "switching", f"Trying {s['ssid']}…"
            ok, _ = self.nm.up(s["name"], self.cfg["connect_timeout_s"])
            if ok:
                return True
        return False

    # main loop -------------------------------------------------------------------------
    def run(self):
        while self.alive:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - never die; log and retry
                log.exception("manager tick failed")
            time.sleep(3)

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return                             # a user action is changing the radio
        try:
            st = self.st = self.nm.state()
            now = time.monotonic()
            if st["connection"] == HOTSPOT_CON:
                self.mode = "hotspot"
                self.since_disconnected = None
                self._hotspot_idle_retry(now)
            elif st["connected"]:
                if self.mode != "client":
                    log.info("connected to %s (%s)", st["ssid"], st["ip"])
                self.mode, self.message = "client", ""
                self.since_disconnected = None
            else:
                self.mode = "searching"
                if self.since_disconnected is None:
                    self.since_disconnected = now
                waited = now - max(self.since_disconnected, self.boot)
                if waited >= self.cfg["grace_s"]:
                    self._start_hotspot("no saved network connected")
        finally:
            self.lock.release()

    def _hotspot_idle_retry(self, now):
        if now < self.hold_until or not self.nm.saved():
            self.idle_since = None
            return
        if self.nm.stations():
            self.idle_since = None
            return
        self.idle_since = self.idle_since or now
        if now - self.idle_since < self.cfg["idle_retry_s"] or now - self.last_retry < self.cfg["retry_every_s"]:
            return
        self.last_retry = now
        log.info("hotspot idle; looking for saved networks")
        self.nm.down(HOTSPOT_CON)
        visible = {n["ssid"] for n in self._scan(True)}
        if not self._try_saved(visible):
            self._start_hotspot("no saved network in range")
        else:
            self.mode, self.message = "client", ""
            self.st = self.nm.state()

    # user actions (from the web UI) ----------------------------------------------------
    def connect(self, ssid, psk, hidden=False):
        def work():
            with self.lock:
                self.mode, self.message, self.last_error = "switching", f"Connecting to {ssid}…", None
                time.sleep(2)                  # let the HTTP reply reach the phone first
                name, out = self.nm.add_network(ssid, psk, hidden)
                if not name:
                    self.last_error = "Could not save the network: " + out[-160:]
                    self._start_hotspot("saving failed")
                    return
                if self.st.get("connection") == HOTSPOT_CON:
                    self.nm.down(HOTSPOT_CON)
                ok, out = self.nm.up(name, self.cfg["connect_timeout_s"])
                if ok:
                    log.info("connected to new network %s", ssid)
                    self.hold_until = 0.0
                    self.mode, self.message = "client", ""
                    self.st = self.nm.state()
                else:
                    self.last_error = f"{ssid}: {classify_error(out)}"
                    log.warning("connecting to %s failed: %s", ssid, out)
                    self.nm.delete(name)       # don't keep retrying a bad password
                    self._start_hotspot("new network failed")
        threading.Thread(target=work, daemon=True).start()

    def rescan(self):
        with self.lock:
            if self.st.get("connection") == HOTSPOT_CON:
                self.nm.down(HOTSPOT_CON)        # the radio can't scan while it is an access point
                self._scan(True)
                self._start_hotspot("rescan done")
            else:
                self._scan(True)
        return self.scan_cache

    def force_hotspot(self):
        def work():
            with self.lock:
                self.hold_until = time.monotonic() + HOLD_S   # not forever: if nobody can connect, saved networks win again
                time.sleep(1)
                if self.st.get("connection") and self.st["connection"] != HOTSPOT_CON:
                    self.nm.down(self.st["connection"])
                self._start_hotspot("requested from the setup page")
        threading.Thread(target=work, daemon=True).start()

    def forget(self, name):
        with self.lock:
            if not any(s["name"] == name for s in self.nm.saved()):
                return False
            return self.nm.delete(name)

    def set_hotspot(self, ssid, password):
        with self.lock:
            self.cfg["hotspot_ssid"], self.cfg["hotspot_password"] = ssid, password
            save_config(self.cfg_path, self.cfg)
        if self.st.get("connection") == HOTSPOT_CON:
            def work():
                time.sleep(2)
                with self.lock:
                    self._start_hotspot("hotspot settings changed")
            threading.Thread(target=work, daemon=True).start()

    def set_name(self, name):
        with self.lock:
            self.nm.set_hostname(name)

    def bridge_status(self):
        try:
            st = json.loads(Path(self.status_path).read_text())
            if time.time() - st.get("time", 0) < 10:
                return st
        except (OSError, ValueError):
            pass
        return None

    def status(self, via_hotspot):
        st = self.st
        res = {"mode": self.mode, "message": self.message, "error": self.last_error,
               "ssid": st.get("ssid"), "ip": st.get("ip"), "signal": st.get("signal"),
               "hotspot_ssid": self.cfg["hotspot_ssid"], "hostname": socket.gethostname(),
               "hold_hotspot": time.monotonic() < self.hold_until, "via_hotspot": via_hotspot,
               "netd": __version__, "bridge": self.bridge_status()}
        if via_hotspot:
            res["pin"] = self.cfg["pin"]
            res["hotspot_password"] = self.cfg["hotspot_password"]
        return res


# ------------------------------------------------------------------------------ web
CAPTIVE_OK = {   # what OS connectivity checks expect once the visitor has dismissed the portal
    "/generate_204": (204, "text/plain", b""),
    "/gen_204": (204, "text/plain", b""),
    "/hotspot-detect.html": (200, "text/html", b"<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"),
    "/library/test/success.html": (200, "text/html", b"<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"),
    "/connecttest.txt": (200, "text/plain", b"Microsoft Connect Test"),
    "/ncsi.txt": (200, "text/plain", b"Microsoft NCSI"),
    "/success.txt": (200, "text/plain", b"success\n"),
    "/canonical.html": (200, "text/html", b'<meta http-equiv="refresh" content="0;url=https://support.mozilla.org/kb/captive-portal"/>'),
}


class Web(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, mgr, page, fake=False):
        super().__init__(addr, Handler)
        self.mgr, self.page, self.fake = mgr, page, fake
        self.dismissed = set()                # hotspot clients that chose "just use the hotspot"
        self.pin_fails = {}                   # ip -> (count, first_time)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ircam-netd/" + __version__

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    # helpers ---------------------------------------------------------------------------
    @property
    def ip(self):
        return self.client_address[0].removeprefix("::ffff:")

    @property
    def via_hotspot(self):
        return self.ip.startswith(AP_NET) or (self.server.fake and self.ip == "127.0.0.1")

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _is_own_host(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        me = socket.gethostname().lower()
        return host in ("", AP_IP, "localhost", "127.0.0.1", me, me + ".local") or host == self.server.server_address[0]

    def _captive(self):
        """Requests on the hotspot for other hosts: OS connectivity checks / random sites."""
        path = self.path.split("?")[0]
        if self.ip in self.server.dismissed and path in CAPTIVE_OK:
            code, ctype, body = CAPTIVE_OK[path]
            return self._send(code, ctype, body)
        self._send(302, "text/html", b'<a href="http://%s/">Setup</a>' % AP_IP.encode(),
                   {"Location": f"http://{AP_IP}/"})

    # GET -------------------------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.via_hotspot and not self._is_own_host():
            return self._captive()
        mgr = self.server.mgr
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", self.server.page)
        if path == "/api/status":
            return self._json(mgr.status(self.via_hotspot))
        if path == "/api/scan":
            return self._json({"networks": mgr.scan_cache, "time": mgr.scan_time})
        if path == "/api/saved":
            return self._json({"saved": mgr.nm.saved()})
        if path in CAPTIVE_OK:                 # a check that reached us by IP
            return self._captive()
        self._json({"error": "not found"}, 404)

    # POST ------------------------------------------------------------------------------
    def _authorized(self):
        if self.via_hotspot:
            return True                        # the hotspot itself is WPA2-protected
        fails = self.server.pin_fails
        n, t0 = fails.get(self.ip, (0, 0))
        if n >= 5 and time.monotonic() - t0 < 60:
            return False
        pin = self.headers.get("X-PIN", "")
        if secrets.compare_digest(pin.encode(), self.server.mgr.cfg["pin"].encode()):
            fails.pop(self.ip, None)
            return True
        fails[self.ip] = (n + 1, t0 if n and time.monotonic() - t0 < 60 else time.monotonic())
        return False

    def do_POST(self):
        if self.via_hotspot and not self._is_own_host():
            return self._captive()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 8192)) or b"{}") if n else {}
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        path = self.path.split("?")[0]
        mgr = self.server.mgr
        if path == "/api/dismiss":
            if self.via_hotspot:
                self.server.dismissed.add(self.ip)
            return self._json({"ok": True})
        if not self._authorized():
            return self._json({"error": "PIN required (shown on the setup page when you are on the hotspot)"}, 403)
        if path == "/api/connect":
            ssid, psk = body.get("ssid"), body.get("password", "")
            if not valid_ssid(ssid):
                return self._json({"error": "Network name must be 1–32 bytes."}, 400)
            if not valid_psk(psk):
                return self._json({"error": "Password must be 8–63 characters (or empty for an open network)."}, 400)
            mgr.connect(ssid, psk, bool(body.get("hidden")))
            return self._json({"ok": True, "switching": True})
        if path == "/api/rescan":
            return self._json({"networks": mgr.rescan(), "time": mgr.scan_time})
        if path == "/api/forget":
            ok = mgr.forget(str(body.get("name", "")))
            return self._json({"ok": ok}, 200 if ok else 404)
        if path == "/api/hotspot":
            ssid, pw = body.get("ssid"), body.get("password", "")
            if not valid_ssid(ssid) or not valid_psk(pw) or not pw:
                return self._json({"error": "Hotspot name 1–32 bytes, password 8–63 characters."}, 400)
            mgr.set_hotspot(ssid, pw)
            return self._json({"ok": True})
        if path == "/api/hotspot_now":
            mgr.force_hotspot()
            return self._json({"ok": True})
        if path == "/api/name":
            name = str(body.get("name", "")).strip().lower()
            if not HOSTNAME_RE.match(name):
                return self._json({"error": "Use 1–32 letters, digits or '-' (not at the ends)."}, 400)
            mgr.set_name(name)
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="WiFi manager and setup page for the wireless P2 Pro bridge.")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--status-file", default=STATUS_PATH)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--fake", action="store_true", help="simulated WiFi (try the page on a PC)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(message)s" if os.environ.get("INVOCATION_ID")
                        else "%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    nm = FakeNM(cfg["iface"]) if a.fake else NM(cfg["iface"])
    if a.fake:
        cfg["grace_s"] = min(cfg["grace_s"], 5)
    mgr = Manager(nm, cfg, a.config, a.status_file)
    page = (Path(__file__).resolve().parent / "setup.html").read_bytes()
    web = Web((a.bind, a.port), mgr, page, a.fake)
    threading.Thread(target=mgr.run, daemon=True, name="manager").start()
    log.info("setup page on http://%s:%d/  (hotspot %s)", a.bind, a.port, cfg["hotspot_ssid"])
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
