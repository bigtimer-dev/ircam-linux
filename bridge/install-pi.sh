#!/usr/bin/env bash
# Install the wireless P2 Pro bridge on a Raspberry Pi (Zero 2 W) running Raspberry Pi OS
# Lite (Bookworm or newer, NetworkManager). Normally run through bridge/deploy.sh from the PC:
#
#   bridge/deploy.sh pi@p2pro.local
#
# or on the Pi itself, from a copy of the repo:   sudo bash bridge/install-pi.sh
#   --uninstall   remove services and files (keeps saved WiFi networks)
#   --no-reboot   don't offer to reboot at the end
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/opt/ircam-bridge
CONF_DIR=/etc/ircam-bridge
BOOT_CONFIG=/boot/firmware/config.txt
[ -f "$BOOT_CONFIG" ] || BOOT_CONFIG=/boot/config.txt
MARK="# --- ircam-bridge ---"
REBOOT=1
UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --uninstall) UNINSTALL=1 ;;
    --no-reboot) REBOOT=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option $a" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (sudo bash $0)"

if [ "$UNINSTALL" = 1 ]; then
  say "Removing the bridge"
  systemctl disable --now ircam-web.service ircam-bridge.service ircam-netd.service 2>/dev/null || true
  nmcli con delete p2pro-hotspot 2>/dev/null || true
  rm -f /etc/systemd/system/ircam-bridge.service /etc/systemd/system/ircam-netd.service \
        /etc/systemd/system/ircam-web.service \
        /etc/NetworkManager/dnsmasq-shared.d/captive.conf \
        /etc/NetworkManager/conf.d/90-ircam-wifi-powersave.conf \
        /etc/udev/rules.d/99-ircam-linux.rules
  rm -rf "$DEST"
  sed -i "/^$MARK/,/^# --- end ircam-bridge ---/d" "$BOOT_CONFIG"
  systemctl daemon-reload
  say "Done. $CONF_DIR (hotspot password, PIN) was kept; delete it by hand if you want."
  exit 0
fi

command -v nmcli >/dev/null || die "NetworkManager (nmcli) not found. Use Raspberry Pi OS Bookworm or newer."
for f in bridge/ircam_bridge.py bridge/ircam_netd.py bridge/ircam_web.py bridge/setup.html bridge/web/viewer.html ircam/sources.py; do
  [ -f "$SRC/$f" ] || die "missing $SRC/$f (run from a copy of the ircam-linux repo)"
done

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-numpy libusb-1.0-0 avahi-daemon iw dnsmasq-base >/dev/null

say "Creating the service user 'ircam'"
id ircam >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin ircam
usermod -a -G video,plugdev ircam

say "Copying the bridge to $DEST"
rm -rf "$DEST.new"
mkdir -p "$DEST.new"
cp "$SRC/bridge/ircam_bridge.py" "$SRC/bridge/ircam_netd.py" "$SRC/bridge/ircam_web.py" "$SRC/bridge/setup.html" "$DEST.new/"
cp -r "$SRC/bridge/web" "$DEST.new/web"
cp -r "$SRC/ircam" "$DEST.new/ircam"
rm -rf "$DEST.new/ircam/ui" "$DEST.new/ircam/data"
find "$DEST.new" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$DEST"
mv "$DEST.new" "$DEST"
chown -R root:root "$DEST"

say "USB access rule for the P2 Pro vendor channel"
install -m 644 "$SRC/data/99-ircam-linux.rules" /etc/udev/rules.d/99-ircam-linux.rules
udevadm control --reload-rules && udevadm trigger --subsystem-match=usb || true

say "NetworkManager: WiFi power save off, captive portal for the hotspot"
install -D -m 644 "$SRC/bridge/system/90-ircam-wifi-powersave.conf" /etc/NetworkManager/conf.d/90-ircam-wifi-powersave.conf
install -D -m 644 "$SRC/bridge/system/captive.conf" /etc/NetworkManager/dnsmasq-shared.d/captive.conf

say "Boot config ($BOOT_CONFIG): USB host mode, minimal GPU memory, Bluetooth off"
sed -i "/^$MARK/,/^# --- end ircam-bridge ---/d" "$BOOT_CONFIG"
cat >> "$BOOT_CONFIG" <<EOF
$MARK
[all]
# the single micro-USB OTG port drives the P2 Pro as a USB host
dtoverlay=dwc2,dr_mode=host
gpu_mem=16
dtoverlay=disable-bt
# --- end ircam-bridge ---
EOF
systemctl disable --now hciuart.service bluetooth.service 2>/dev/null || true

say "Services"
install -m 644 "$SRC/bridge/system/ircam-bridge.service" /etc/systemd/system/ircam-bridge.service
install -m 644 "$SRC/bridge/system/ircam-netd.service" /etc/systemd/system/ircam-netd.service
# the browser viewer is optional: installed, but only switched on from the setup page (an
# earlier on/off choice is kept across updates)
install -m 644 "$SRC/bridge/system/ircam-web.service" /etc/systemd/system/ircam-web.service
systemctl daemon-reload
systemctl enable ircam-bridge.service ircam-netd.service >/dev/null
# first start of netd writes $CONF_DIR/netd.json with a random hotspot password and PIN
systemctl restart ircam-netd.service
systemctl reload NetworkManager 2>/dev/null || true
for _ in $(seq 20); do [ -f "$CONF_DIR/netd.json" ] && break; sleep 0.5; done
systemctl restart ircam-bridge.service
if systemctl is-enabled --quiet ircam-web.service; then systemctl restart ircam-web.service; fi

COUNTRY=$(iw reg get 2>/dev/null | awk '/^country/ {sub(":","",$2); print $2; exit}')
if [ -z "$COUNTRY" ] || [ "$COUNTRY" = "00" ]; then
  warn "WiFi country is not set; the hotspot may not start. Set it with: sudo raspi-config nonint do_wifi_country US  (your country code)"
fi

HS_SSID=$(python3 -c "import json;print(json.load(open('$CONF_DIR/netd.json'))['hotspot_ssid'])" 2>/dev/null || echo "?")
HS_PW=$(python3 -c "import json;print(json.load(open('$CONF_DIR/netd.json'))['hotspot_password'])" 2>/dev/null || echo "?")
PIN=$(python3 -c "import json;print(json.load(open('$CONF_DIR/netd.json'))['pin'])" 2>/dev/null || echo "?")
# a copy on the boot partition, readable from any PC by putting the SD card in a reader
BOOT_DIR=$(dirname "$BOOT_CONFIG")
printf 'P2 Pro bridge\nHotspot: %s\nPassword: %s\nPIN: %s\nSetup page: http://10.42.0.1 (hotspot) or http://%s.local\n' \
  "$HS_SSID" "$HS_PW" "$PIN" "$(hostname)" > "$BOOT_DIR/p2pro-bridge.txt" || true

echo
say "Installed."
cat <<EOF

  Address in IRCAM:   $(hostname).local   (on the hotspot: 10.42.0.1)
  Setup page:         http://$(hostname).local   or   http://10.42.0.1 on the hotspot
  Browser viewer:     turn it on from the setup page, then open http://$(hostname).local:8080
  Hotspot:            $HS_SSID   password: $HS_PW
  PIN (changes from the home network): $PIN
  (also saved to $BOOT_DIR/p2pro-bridge.txt)

  Logs:   journalctl -u ircam-bridge -u ircam-netd -f
  The USB host mode setting needs a reboot to take effect.
EOF
if [ "$REBOOT" = 1 ]; then
  read -r -p "Reboot now? [Y/n] " ans </dev/tty || ans=n
  case "${ans:-y}" in [Yy]*) say "Rebooting"; systemctl reboot ;; esac
fi
