# Wireless P2 Pro bridge — Raspberry Pi Zero 2 W

The P2 Pro plugs into a Raspberry Pi Zero 2 W. The Pi streams the camera's raw temperature data
over WiFi to ircam-linux ("InfiRay P2 Pro (Wireless)"). USB on the PC keeps working as before.
Design and measurements: [../docs/WIRELESS_PLAN.md](../docs/WIRELESS_PLAN.md).

| File | What it does |
|---|---|
| `ircam_bridge.py` | Reads the camera (V4L2 + vendor channel) and sends IRW1 frames. `--demo` runs without a camera |
| `ircam_netd.py` + `setup.html` | WiFi manager (home network ↔ hotspot fallback), captive portal, setup page on port 80. `--fake --port 8080` runs a simulated version on a PC |
| `ircam_web.py` + `web/` | Optional browser viewer (iPhone, iPad, any browser) on port 8080. Off until switched on from the setup page |
| `install-pi.sh` | Installs everything on the Pi (packages, services, USB host mode, WiFi tuning) |
| `deploy.sh` | Copies the bridge from the PC to the Pi over SSH and runs the installer |
| `system/` | systemd units and NetworkManager config used by the installer |

## What you need

- Raspberry Pi Zero 2 W, a microSD card (16–32 GB), a 5 V supply (power bank, or PiSugar 3)
- OTG adapter **micro-USB male → USB-C female**. It must carry data, and it goes in the Pi's
  **USB** port (the inner one), not PWR.
- On the PC: Raspberry Pi Imager (`sudo pacman -S rpi-imager`) and an SSH key
  (`ssh-keygen -t ed25519` if `~/.ssh/id_ed25519.pub` doesn't exist)

## 1. Flash the SD card

1. Open Raspberry Pi Imager.
   - **Device:** Raspberry Pi Zero 2 W
   - **OS:** Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**
   - **Storage:** your SD card
2. When it asks about customisation, choose **Edit settings**:
   - **Hostname:** `p2pro`
   - **Username / password:** your choice (examples below use `pi`)
   - **WiFi:** your home network name and password. It must be **2.4 GHz**, because the Zero 2 W
     has no 5 GHz.
   - **WiFi country:** your country. Without it, the hotspot can't start.
   - **Services → Enable SSH → Allow public-key authentication only:** paste your
     `~/.ssh/id_ed25519.pub`
3. Write the card, put it in the Pi, and power the Pi from the **PWR** port. You don't need
   the camera yet. The first boot takes about 2 minutes.

## 2. Install the bridge (one command from the PC)

```bash
ping p2pro.local                 # wait until it answers
ssh pi@p2pro.local true          # accept the host key once
cd ~/Work/ircam-linux
bridge/deploy.sh pi@p2pro.local  # installs, prints the hotspot password + PIN, offers a reboot
```

The installer prints the **hotspot name, password and PIN**. It also saves them to
`p2pro-bridge.txt` on the SD card's boot partition, so you can read them on any PC with a card
reader.

## 3. Test

1. Plug the P2 Pro into the Pi's **USB** port with the OTG adapter.
2. On the PC, run the app from this branch: `python3 -m ircam --wireless` (or `./install.sh`
   to update the installed app, then `ircam-linux --wireless`). Or pick
   **InfiRay P2 Pro (Wireless)** in the camera list and click Connect.
3. The status bar shows fps, packet loss, latency, WiFi signal and battery.
4. Setup page: open `http://p2pro.local`.

**Testing the hotspot:**
- On the setup page, open "Hotspot & device settings" → **Switch to hotspot now**. Or switch
  your router off.
- Join `P2Pro-xxxx` with your phone or laptop. The setup page should pop up by itself.
- Stream from the app using the address `10.42.0.1`.
- Add your home network on the page. The Pi switches back to it (if the password is wrong, the
  hotspot returns in about 15 s and the page says so).
- "Switch to hotspot now" holds the hotspot for 5 minutes, then the Pi goes back to known networks.

## Browser viewer (iPhone / iPad)

iOS doesn't allow apps to talk to the P2 Pro over USB, so iPhones watch through the bridge in Safari.

1. Open the setup page (`http://p2pro.local`, or `http://10.42.0.1` on the hotspot).
2. **Browser viewer → Turn on** (no PIN needed).
3. Open the link it shows (`http://p2pro.local:8080`) in Safari. Share → **Add to Home Screen**
   gives it an app icon and full screen.

It has live view, max/min/centre readouts, 52 palettes, Low/High range, snapshot (Share → Save
Image), video recording, emissivity and °C/°F. Touch: tap to measure (drag the spot to move it),
pinch or double-tap to zoom, swipe left/right for the next palette. Full screen (controls hide
until you touch the screen), a landscape layout with the controls at the side, a colour-range
lock, freeze, rotate, mirror and keep-screen-on. Needs iOS 16.4 or newer.

It's an add-on: it reads frames from a local socket on the Pi (`--tap`), separate from the IRW1
viewer slot, so IRCAM on the PC or Android keeps streaming while a browser watches. With no
browser connected the bridge does no extra work. **Turn off** stops and disables the service
(`ircam-web`); the choice survives reboots and updates.

## Troubleshooting

| Symptom | Check |
|---|---|
| `p2pro.local` not found | Use the Pi's IP from your router. The app also accepts an IP address in Settings → Camera |
| Setup page says "Camera: Not connected" | Is the adapter in the USB port (not PWR)? Does it carry data? Run `ssh pi@p2pro.local lsusb` and look for `0bda:5830` |
| Camera never detected, `dmesg` shows nothing at all when plugging | The adapter/cable has no data wires, or it's in the PWR port. Many micro-USB→USB-C adapters are charge-only. A phone saying **"Analog audio accessory detected"** through the adapter means its USB-C CC pins are miswired: use another adapter. Check the port directly: `sudo grep HPRT0 /sys/kernel/debug/usb/3f980000.usb/regdump` (bit 0 = device connected) |
| Hotspot doesn't start | WiFi country not set: `sudo raspi-config nonint do_wifi_country US` (your code), then reboot |
| Stutter / loss | Run `journalctl -u ircam-bridge -f`. Move closer, check that 2.4 GHz isn't crowded, check the power supply (≥ 2 A) |
| Browser viewer shows "Can't reach the bridge" | Is it switched on (setup page)? Same WiFi? Check `journalctl -u ircam-web -f` |
| Logs | `ssh pi@p2pro.local journalctl -u ircam-bridge -u ircam-netd -u ircam-web -f` |

Update after code changes: run `bridge/deploy.sh pi@p2pro.local` again.
Remove: `bridge/deploy.sh pi@p2pro.local --uninstall`.

Optional battery readout: install the PiSugar power manager on the Pi (see PiSugar's docs). The
bridge reads it automatically.
