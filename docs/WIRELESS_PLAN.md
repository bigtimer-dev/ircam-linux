# Wireless P2 Pro — Execution Plan (Raspberry Pi Zero 2 W bridge)

Goal: plug the InfiRay P2 Pro into a battery-powered Raspberry Pi Zero 2 W. The Pi sends the
camera's raw temperature data over WiFi, with low latency, to ircam-linux (later also
ircam-android). The app works exactly as it does with the wired camera.

**Wireless is purely an add-on.** The wired USB path (`V4L2Source`, the P2 Pro vendor
channel, every other camera model) stays as it is. Wireless shows up as one more entry in the
camera list: "InfiRay P2 Pro" for USB, "InfiRay P2 Pro (Wireless)" for the bridge. New code
goes in new files. Existing files only get the new camera entry, the settings keys, the UI
hook, and one line in `make_source`.

**Same data, different transport.** The bridge reads the identical 256×384 frame over USB
and sends the temperature half (256×192 u16, unchanged) inside UDP packets. The PC rebuilds
the same 256×384 buffer, so decoding, palettes, measurements, recordings and snapshots all run
the existing code. Range switching becomes a request over the network, and the Pi sends the
same USB vendor command to the camera.

```
P2 Pro ──USB-C──[OTG adapter]──> Pi Zero 2 W ──── WiFi 2.4 GHz ────> PC: ircam-linux
                                  ircam-bridge (systemd)               NetworkSource
                                  V4L2 capture → crop → zlib → UDP ──> → existing engine/UI
                                  TCP control  <── range/status/ping ── (client-initiated)
```

---

## Key numbers

| Item | Value |
|---|---|
| Camera frame (UVC YUYV) | 256×384 × 2 B = 196,608 B |
| Temperature half (sent) | rows 192–383 = 98,304 B per frame, values in 1/64 K |
| Raw rate @ 25 fps | 2.46 MB/s ≈ 19.7 Mbit/s |
| Pi Zero 2 W real UDP throughput (2.4 GHz, 1×1 802.11n) | ~25–35 Mbit/s, so raw *fits but is tight* |
| With codec 1 (row-delta + byte planes + zlib-1, measured) | **~5.2 Mbit/s** on a busy scene, which leaves plenty of headroom |
| Latency target (camera → screen) | < 80 ms (one sensor frame alone is 40 ms) |
| Power estimate | Pi ~0.8–1.2 W while streaming + P2 Pro ~0.35 W ≈ 1.5 W |

---

## What's needed

### Hardware (shopping list)

| Item | Notes |
|---|---|
| **Raspberry Pi Zero 2 W** | The version with soldered headers isn't needed. The onboard antenna is 2.4 GHz only (no 5 GHz) |
| **microSD 16–32 GB, A1/A2** | Any brand-name card |
| **OTG adapter: micro-USB male → USB-C female** | The P2 Pro has a *male* USB-C plug. Use the Pi's port marked **USB**, not PWR. Get a short rigid adapter or a 10 cm cable; must carry data, not charge only |
| **Power: pick one** | (a) any USB power bank → PWR port with a micro-USB cable (fine for testing); (b) **PiSugar 3** (1200 mAh, bolts under the Zero, reports battery level over I²C, ~2.5 h); (c) PiSugar 3 Plus 5000 mAh (~8–10 h) |
| **Mount / case** | 3D-printed case or bracket so the Pi + battery clip onto the P2 Pro or a phone mount (do this last) |
| For setup only | A PC with an SD card reader. A micro-HDMI adapter and keyboard are *not* required (headless setup) |

### Software on the Pi

| Item | Why |
|---|---|
| Raspberry Pi OS **Lite 64-bit** (current release, NetworkManager-based) | Headless, small. Flashed with Raspberry Pi Imager: hostname `p2pro`, user, SSH key, home WiFi preset |
| `python3-numpy`, `libusb-1.0-0`, `avahi-daemon` (apt) | numpy: row-delta. Compression is zlib from the standard library (nothing to install). libusb: the P2 Pro vendor channel (`ircam/usb.py` loads it via ctypes). avahi: so the Pi is reachable as `p2pro.local` |
| Reused ircam-linux modules: `ircam/v4l2.py` (stdlib only), `ircam/usb.py`, `ircam/p2pro.py` | Same capture and range code as the desktop app. No GTK on the Pi |
| udev rule `data/99-ircam-linux.rules` | Lets the service user reach the vendor channel. User added to the `video` group |
| `/boot/firmware/config.txt`: `dtoverlay=dwc2,dr_mode=host` | Forces the single OTG port into host mode regardless of the adapter's ID pin |

### On the PC (this machine)

- **Firewall:** ufw is active here and denies incoming connections. So the protocol is
  **client-initiated**: the PC sends to the Pi first (UDP subscribe + TCP connect), and the
  frames come back as replies on the same flow, which ufw allows as ESTABLISHED. No firewall
  rules needed. Only the optional LAN discovery would need one rule, so the default is to find
  the Pi as `p2pro.local` or by a manually entered IP.
- No new dependencies: zlib is in Python's standard library (and `java.util.zip` on Android).

---

## Phase 0 — Check assumptions ✅ done 2026-10-31

**USB descriptors** (`docs/p2pro-descriptors.txt`):
- USB 2.0 **High-Speed (480 Mbps)**, declares 500 mA.
- The video stream is **isochronous**, with alt settings 1–7 (128 B … 3×1024 B per
  microframe).
- It needs 196,608 B × 25 fps ≈ 614 B per 125 µs microframe, so **alt 3 (1×1024 B)** is
  enough. No high-bandwidth (multi-transaction) isochronous mode is needed, and that is the
  case the Pi's dwc2 controller handles worst. **Risk lowered.**
- Formats: 256×192 and 256×384 YUYV, 25 fps only.

**Compression** (`tools/capture_frames.py` + `tools/compress_bench.py`; each band compressed
on its own; ms measured on the PC):

| Scene | Codec | Rows/band | B/frame | Ratio | Mbit/s @25 | Bands > 1376 B | ms/frame |
|---|---|---|---|---|---|---|---|
| raw | — | — | 98,304 | 1.00 | 19.7 | — | — |
| flat (31–33 °C) | hdelta+planes+zlib-1 | 8 | 21,482 | 4.58 | 4.30 | 0 % | 1.5 |
| **busy, moving (30–174 °C)** | **hdelta+planes+zlib-1** | **8** | **26,057** | **3.77** | **5.21** | **0 %** | **1.6** |
| busy | hdelta+planes+zstd-1 | 8 | 24,052 | 4.09 | 4.81 | 0 % | 1.0 |
| busy | med+planes+zlib-1 | 8 | 24,825 | 3.96 | 4.97 | 0 % | 2.3 |
| busy | hdelta+planes+LZ4 | 8 | 45,677 | 2.15 | 9.14 | 100 % | 0.7 |
| busy | plain zlib-1 (no predictor) | 8 | 39,876 | 2.47 | 7.98 | 92 % | 1.5 |

**Decision: codec 1 = horizontal delta + zigzag + hi/lo byte planes + zlib level 1, 8-row
bands.**
- LZ4 does badly because sensor noise needs entropy coding.
- zstd is only about 8 % smaller, but it isn't in the standard library on the Pi's Python
  (3.13) or on Android.
- zlib is everywhere, with no dependencies.
- Expected cost on the Pi Zero 2 W (Cortex-A53): roughly 10–20 ms per frame on one of its 4
  cores, to be verified in Phase 2.

## Phase 1 — Protocol + PC side (2–3 days, all testable without the Pi)

> **Status: ✅ done 2026-10-31.** Real P2 Pro → bridge on the PC → localhost → app: 25.0 fps,
> 0 % loss, ~6 ms added by the bridge pipeline (encode + send + decode), range switching works
> both ways. Unit, assembly and loopback tests in `tests/test_net.py`. The app can also be
> started with `--wireless[=HOST]`. Differences from the text below: the bridge already reads
> RSSI from `/proc/net/wireless` (no `iw` needed), and close/takeover use `shutdown()` so the
> other side notices at once.

### 1.1 Protocol `IRW1` — `ircam/net/protocol.py` (stdlib + numpy only)

**Control: TCP, PC → Pi port 47801, newline-delimited JSON**

| Message | Reply / effect |
|---|---|
| `hello` | `{model, serial, fw, width:256, height:384, temp_rows:[192,384], codecs, high_range}` |
| `start {codec, fps_limit, full_frame}` | Begin streaming to the UDP address that subscribed (below). `full_frame` also sends the image half, for debugging |
| `stop` | Stop streaming |
| `set_range {high}` / `get_range` | Runs `P2ProControl.set_high_gain(not high)` / `high_gain()` |
| `ping {t}` | `pong {t, bridge_us}`: round-trip time and clock offset, used for the latency readout |
| `status` | `{rssi_dbm, tx_fps, sent, dropped_capture, cpu_temp, battery_pct, battery_mv}` |

If the TCP session closes, or no ping arrives for 3 s, the Pi stops streaming. One client at a
time; a new client takes over.

**Frames: UDP, Pi port 47800 → back to the PC's subscribing socket**
- The PC opens one UDP socket and sends `SUB <session-token>` to Pi:47800, repeated every
  second as a keepalive. The Pi sends frames to the source address and port of that datagram.
  This gets through ufw and NAT on the PC.
- One datagram = one **independently decodable row band**, at most 1400 B, so it never
  fragments.
- Header, 24 B little-endian: `magic "IRW1"` · `ver u8` · `codec u8` (0 raw, 1 hdelta+planes+zlib)
  · `flags u16` (bit0 high range, bit1 full frame) · `frame_id u32` · `ts_us u64` (Pi
  monotonic clock at V4L2 dequeue) · `row_start u16` · `row_count u16`.
- Codec 1 = per band: horizontal delta → zigzag → split into hi/lo byte planes → `zlib`
  level 1. Bands are **8 rows**. If a band ever compresses to more than 1376 B, it is re-sent
  as two 4-row bands, or as raw 2-row bands. In raw mode a band is 2 rows (1024 B).
- **No retransmission (latency first).** The receiver collects bands by `frame_id`. A frame
  is published when all its rows have arrived, or when a newer `frame_id` starts. Any missing
  bands are copied from the previous frame and counted as loss. A late packet from an older
  frame is discarded.
- Set socket `IP_TOS = 0xA0` (CS5) so the WiFi chip puts the packets in the video WMM
  queue.

### 1.2 Bridge program — `bridge/ircam_bridge.py`
- **Capture thread:** `v4l2.V4L2Camera` → `set_format(256, 384, YUYV)`, `set_fps(25)`,
  4 mmap buffers. `read()` already returns the newest frame. It slices bytes
  `[98304:196608]` (the temperature half) and drops them into a 1-slot "latest" holder,
  overwriting the previous one so stale frames never queue up.
- **Send thread:** waits for a new frame, then codec 1 per 8-row band, header,
  `sendto`. It counts sent and skipped frames.
- **Control thread:** TCP server as above. Range calls go through `P2ProControl` under a
  lock. RSSI comes from `iw dev wlan0 link`, battery from the PiSugar server (TCP 8423) if
  present.
- **Robustness:** camera unplugged → close and wait for `/dev/video*` to come back (udev also
  restarts the service). Logs go to journald.
- **CLI:** `--device auto|/dev/videoN`, `--demo` (the simulated P2 Pro from `DemoSource`, for
  PC testing), `--codec`, `--port`.

### 1.3 Receiver — ircam-linux
- **`ircam/sources.py`: `NetworkSource(Source)`**
  - `open()`: TCP connect (default host `p2pro.local`), `hello`, `_geometry(256, 384,
    POOL_2)`, `read_camera_config` + `generate_lut` (same as other pool-2 sources), UDP
    subscribe, `start`, then a receive thread.
  - `read()`: wait up to 1 s for a newly assembled frame. Build the 384×256 buffer: the
    temperature half as received, the image half synthesized from it (same trick as
    `DemoSource`) unless `full_frame` was requested. Return its bytes.
  - `can_switch_range` comes from `hello`. `set_range()` / `current_high_range()` go over
    TCP. `fps()` is measured on the receiver. `link_stats()` returns fps, loss %, latency ms,
    RSSI and battery.
  - Losing the connection raises `SourceError`, so the existing UI shows the error and the
    camera can be reopened.
- **`ircam/cameras.py`:** new constant `WIRELESS_P2PRO = "InfiRay P2 Pro (Wireless)"`, added
  to the camera list.
  **`make_source()`:** route that name to `NetworkSource(settings["wireless_host"])`.
- **`ircam/config.py`:** `wireless_host: "p2pro.local"`, `wireless_codec: "auto"`.
- **UI:** a host entry in Settings. While a wireless source is active, a small link label in
  the status area (`25 fps · 0.3 % loss · 45 ms · −55 dBm · 82 %`).

### 1.4 Tests — `tests/test_net.py`
- Header and band pack/unpack round trip for each codec. Codec 1 is lossless (bit
  exact against the input).
- Assembler: reordered, duplicated and missing bands, stale frame discarded, missing band
  filled from the previous frame.
- Loopback: `ircam_bridge.py --demo` on 127.0.0.1 ↔ `NetworkSource`, check the frames are
  byte-identical to the demo frames and range switching works.

**Exit:** with `--demo`, and with the real P2 Pro on this PC running the bridge locally, the
app shows the "Wireless" camera at 25 fps with working range switching. The wired entry still
works unchanged.

---

## Phase 2 — Pi Zero 2 W bridge (2–3 days)

> **Status: software ready 2026-10-31, waiting for the hardware.**
> - Written and tested on the PC: `bridge/install-pi.sh`, `bridge/deploy.sh`,
>   `bridge/ircam_netd.py` + `bridge/setup.html` (WiFi manager, hotspot fallback, captive
>   portal, setup page with PIN), and the `system/` units. The netd tests use a simulated WiFi
>   backend (`tests/test_netd.py`).
> - Not testable without the Pi: real `nmcli` / AP mode, the captive pop-up on phones, the
>   dwc2 USB host, and the measurements below.
> - Step-by-step guide: `bridge/README.md`.
> - Differences from the text below:
>   - The bridge service runs all the time and retries the camera every 2 s, instead of being
>     started by udev.
>   - The WiFi watcher and the setup page are one service, `ircam-netd`.
>   - The GPIO hotspot button is not done yet.
>   - Read-only rootfs is left as a manual step once everything works.

### 2.1 Pi setup (`bridge/install-pi.sh`, run over SSH)
1. Flash Pi OS Lite 64-bit with Imager (hostname `p2pro`, SSH key, home WiFi).
2. `apt install python3-numpy libusb-1.0-0 avahi-daemon`.
3. Copy `bridge/` and the needed `ircam/*.py` to `/opt/ircam-bridge`. Create the system user
   `ircam` in group `video`.
4. Install `99-ircam-linux.rules` plus a rule that starts the service when the camera is
   plugged in: `ENV{SYSTEMD_WANTS}="ircam-bridge.service"` on the P2 Pro's video4linux
   device.
5. `ircam-bridge.service`: `Restart=always`, `Nice=-5`, `CPUSchedulingPolicy=fifo` for the
   capture thread (optional).
6. Latency tuning:
   - NetworkManager `wifi.powersave = 2` (disabled). This is the biggest single latency
     win.
   - CPU governor `performance`.
   - Turn off unused services (bluetooth, triggerhappy, etc.) for a faster boot.
7. `config.txt`: `dtoverlay=dwc2,dr_mode=host`, `gpu_mem=16`, `dtoverlay=disable-bt`
   (frees power and the UART).
8. Optional: read-only root filesystem (`raspi-config` → Overlay FS), so pulling the battery
   can't corrupt the SD card. Enabled only after everything works.

### 2.2 Network modes + setup page
- **Home:** joins a saved WiFi. The PC uses `p2pro.local`.
- **Hotspot (fallback):** `bridge/netwatch.py`. If no saved network connects within 30 s of
  boot, the Pi brings up its own WPA2 network `P2Pro-xxxx` (NetworkManager shared mode,
  channel picked from the least crowded scan). The Pi is then always `10.42.0.1`.
  **Streaming works normally in this mode.** You don't have to use the setup page; you can
  just connect the app and use the camera.
- **Phone hotspot:** just another saved network (it can be added from the setup page).

**Setup page** — `bridge/webui.py`: stdlib `http.server`, port 80, a single page with no
external assets. It's served in both modes: `http://10.42.0.1` on the hotspot and
`http://p2pro.local` at home.
- **Captive portal on the hotspot:** NetworkManager's dnsmasq answers every DNS name with
  10.42.0.1 (`/etc/NetworkManager/dnsmasq-shared.d/captive.conf`), and the page also answers
  the phone/OS connectivity-check URLs. So joining `P2Pro-xxxx` pops the page up
  automatically. Dismiss it and the hotspot keeps working.
- **Add a network:** a list of nearby networks with signal strength, from a scan made just
  before the hotspot started, plus a "rescan" button. You pick one and type the password, or
  enter a hidden SSID by hand.
- **Switching:** the Pi has a single radio, so it can't run the hotspot and join a router at
  the same time. After "Save & connect", the page shows *"Switching… reconnect your device
  to <network> and open http://p2pro.local"*. If the new network fails to connect within
  30 s, the Pi brings the hotspot back, and the page shows the error ("wrong password" /
  "network not found") on the next visit. A wrong password can never strand the Pi.
- **Manage:**
  - list saved networks (priority order, delete)
  - "force hotspot now" button
  - hotspot name and password
  - device name (`p2pro` → `p2pro-shop` etc., which also changes the `.local` name)
- **Status panel:** camera connected, current viewer, fps, WiFi signal, battery, CPU
  temperature, bridge version.
- **Physical fallback (optional):** holding a button on a GPIO pin for 5 s forces hotspot
  mode, for when the Pi is stuck on a network you can't reach.
- **Security:**
  - The hotspot is WPA2. Its default password is printed on a label / in the README, and
    can be changed on the page.
  - On the home network, changing settings requires a PIN shown on the hotspot page (stored
    on the Pi).
  - Viewing the status needs no PIN.

**Phone note:** a hotspot with no internet makes Android prefer mobile data. A browser still
reaches 10.42.0.1 fine. In Phase 3, the Android app must bind its sockets to the WiFi network
(`ConnectivityManager.bindProcessToNetwork`) or the stream would try to go over mobile
data.

### 2.3 Measurements (results recorded in this file)

| Test | How |
|---|---|
| fps, loss % | `link_stats` logged for 10 min at 1 m, 5 m, 10 m, and through one wall |
| Latency (estimate) | ping/pong clock offset + `ts_us` → time from capture to display |
| Latency (glass-to-glass) | Film an LED / stopwatch and the PC screen together with a phone at 240 fps; compare against the wired camera |
| CPU / temperature | `top`, `vcgencmd measure_temp` while streaming. A small heatsink if it throttles |
| Power / runtime | USB power meter on the 5 V input, then a full battery run-down |
| Boot to streaming | Stopwatch from power-on to the first frame in the app |

**Exit criteria:** 25 fps · < 80 ms glass-to-glass · < 1 % loss at 5 m · runtime ≥ 2 h on
PiSugar 3 · auto-start with no SSH needed.

---

## Phase 3 — Android receiver (optional, ~2 days)

- In `ircam-android`: Kotlin `NetworkSource` speaking the same IRW1 (`DatagramChannel` + TCP
  control, `java.util.zip.Inflater`), added as a second frame source next to the libusb UVC path. USB stays
  the default.

---

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Pi Zero USB controller (`dwc2`) and the P2 Pro's isochronous stream | Dropped or corrupted frames | Phase 0 descriptor check. Webcams at this rate are normal on the Zero. If needed: `dwc_otg.fiq_fsm_enable`, a powered OTG hub, or check the cable |
| 2.4 GHz throughput / congestion | Packet loss and jitter | Codec 1 compression (~3.8×, measured), loss hidden by row bands, AP channel picked by scan, optional `fps_limit` of 12.5 |
| WiFi power save | +100 ms or more of latency | Disabled in NetworkManager. Verified with ping jitter |
| Camera draw plus Pi startup current on a small battery | Brown-out / reboot | 2 A capable supply. PiSugar 3 is rated for the Zero + USB devices |
| SD card corruption from power pulls | Doesn't boot | Overlay FS (read-only root) once stable |
| mDNS `.local` not resolving on some networks | Can't connect | Manual IP field. In AP mode the IP is always 10.42.0.1 |
| Wrong WiFi password entered on the setup page | Pi unreachable | Hotspot comes back after 30 s; optional GPIO button forces hotspot |

---

## Deliverables (in order)

1. `docs/p2pro-descriptors.txt`, `tools/compress_bench.py` + results — Phase 0
2. `ircam/net/protocol.py`, `bridge/ircam_bridge.py`, `NetworkSource`, camera entry,
   settings, UI link label, `tests/test_net.py` — Phase 1
3. `bridge/install-pi.sh`, `bridge/netwatch.py`, `bridge/webui.py` (setup page / captive
   portal), systemd + udev units, measurement table —
   Phase 2
4. Android `NetworkSource` — Phase 3

**Estimate:** ~1 week to a working wireless P2 Pro. Phase 1 can start right away with
`--demo`. Phase 0 needs the camera plugged in, and Phase 2 needs the Pi hardware.
