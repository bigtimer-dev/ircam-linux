#!/usr/bin/env bash
# IRCAM Thermal Viewer for Linux — per-user installer.
#
#   From a clone:     ./install.sh
#   One-liner:        curl -fsSL https://raw.githubusercontent.com/bigtimer-dev/ircam-linux/main/install.sh | bash
#   Uninstall:        ./install.sh --uninstall      (or: ... | bash -s -- --uninstall)
#   Options:          --yes  install missing system packages without asking
#                     --no-deps  skip the dependency check
#
# Installs to ~/.local/share/ircam-linux, ~/.local/bin/ircam-linux and an app-launcher entry. No root needed,
# except (optionally) for system packages and the USB udev rule (P2 Pro range switching, Thermal Master P3).
set -euo pipefail

APP_ID=io.github.bigtimer_dev.IrcamLinux
REPO=bigtimer-dev/ircam-linux
BRANCH=main
TARBALL_URL="${IRCAM_SOURCE:-https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz}"

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
LIBDIR="$DATA/ircam-linux"
BINDIR="$HOME/.local/bin"
BIN="$BINDIR/ircam-linux"
UDEV_RULE=/etc/udev/rules.d/99-ircam-linux.rules
DESKTOP="$DATA/applications/$APP_ID.desktop"
ICON="$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"

ASSUME_YES=0
CHECK_DEPS=1
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --no-deps) CHECK_DEPS=0 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,12p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

# Ask a yes/no question; works when the script itself is piped into bash.
ask() {
  [[ $ASSUME_YES == 1 ]] && return 0
  local reply=""
  if { : > /dev/tty; } 2>/dev/null; then
    printf '%s [y/N] ' "$1" > /dev/tty
    read -r reply < /dev/tty || true
  else
    info "(no terminal to ask; re-run with --yes to accept)"
  fi
  [[ $reply =~ ^[Yy] ]]
}

# ---------------------------------------------------------------- uninstall
if [[ $UNINSTALL == 1 ]]; then
  rm -rf "$LIBDIR" "$BIN" "$DESKTOP" "$ICON"
  command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
  bold "IRCAM Thermal Viewer for Linux removed."
  info "Your settings and custom palettes are kept in ~/.config/ircam-linux."
  [[ -f $UDEV_RULE ]] && info "The udev rule stays installed; remove it with: sudo rm $UDEV_RULE"
  exit 0
fi

bold "Installing IRCAM Thermal Viewer for Linux"

# ---------------------------------------------------------------- dependencies
python_ok() {
  python3 - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
import cairo, numpy
assert (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 12)
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 6)
PY
}

install_packages() {
  local pm="" pkgs="" cmd=""
  if command -v pacman >/dev/null; then
    pm=pacman; pkgs="python python-gobject python-cairo python-numpy gtk4 libadwaita ffmpeg libusb"
    cmd="sudo pacman -S --needed $pkgs"
  elif command -v apt-get >/dev/null; then
    pm=apt; pkgs="python3 python3-gi python3-gi-cairo python3-numpy gir1.2-gtk-4.0 gir1.2-adw-1 ffmpeg libusb-1.0-0"
    cmd="sudo apt-get install -y $pkgs"
  elif command -v dnf >/dev/null; then
    pm=dnf; pkgs="python3 python3-gobject python3-cairo python3-numpy gtk4 libadwaita ffmpeg-free libusb1"
    cmd="sudo dnf install -y $pkgs"
  elif command -v zypper >/dev/null; then
    pm=zypper; pkgs="python3 python3-gobject python3-gobject-cairo python3-numpy typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 ffmpeg libusb-1_0-0"
    cmd="sudo zypper install -y $pkgs"
  else
    die "Unknown package manager. Install Python 3, PyGObject, pycairo, numpy, GTK >= 4.12, libadwaita >= 1.6 (optional: ffmpeg, libusb-1.0), then re-run."
  fi
  info "Missing packages can be installed with:"
  info "  $cmd"
  if ask "Run it now?"; then
    [[ $pm == apt ]] && sudo apt-get update
    $cmd
  else
    die "Dependencies missing. Install them and re-run this script."
  fi
}

if [[ $CHECK_DEPS == 1 ]]; then
  if ! command -v python3 >/dev/null || ! python_ok; then
    warn "Python GTK4 / libadwaita bindings or numpy not found (or too old)."
    install_packages
    python_ok || die "GTK >= 4.12 and libadwaita >= 1.6 are required. Your distribution may be too old (Ubuntu needs 25.04+, Debian 13+)."
  fi
  info "✓ Python, GTK $(python3 -c 'import gi;gi.require_version("Gtk","4.0");from gi.repository import Gtk;print(f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}")'), libadwaita"

  command -v ffmpeg >/dev/null && info "✓ ffmpeg (MP4 recording)" \
    || warn "ffmpeg not found: processed video is recorded as uncompressed AVI instead of MP4."
  python3 -c "import ctypes.util,sys;sys.exit(0 if ctypes.util.find_library('usb-1.0') else 1)" && info "✓ libusb-1.0" \
    || warn "libusb-1.0 not found: P2 Pro range switching and the Thermal Master P3 will be unavailable."
  id -nG | grep -qw video || warn "Your user is not in the 'video' group; cameras may not open (sudo usermod -aG video \$USER, then log in again)."
fi

# ---------------------------------------------------------------- source
SRC=""
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
TMP=""
cleanup() { if [[ -n $TMP ]]; then rm -rf "$TMP"; fi; }
trap cleanup EXIT

if [[ -n $SCRIPT_DIR && -f "$SCRIPT_DIR/ircam/__main__.py" ]]; then
  SRC="$SCRIPT_DIR"
  info "✓ Using local checkout: $SRC"
else
  command -v curl >/dev/null || command -v wget >/dev/null || die "curl or wget is needed to download the app."
  TMP="$(mktemp -d)"
  info "Downloading $TARBALL_URL"
  if command -v curl >/dev/null; then
    curl -fsSL "$TARBALL_URL" -o "$TMP/src.tar.gz" || die "Download failed."
  else
    wget -qO "$TMP/src.tar.gz" "$TARBALL_URL" || die "Download failed."
  fi
  mkdir "$TMP/src"
  tar -xzf "$TMP/src.tar.gz" -C "$TMP/src" --strip-components=1 || die "Could not unpack the download."
  [[ -f "$TMP/src/ircam/__main__.py" ]] || die "Downloaded archive does not contain the app."
  SRC="$TMP/src"
fi

# ---------------------------------------------------------------- install
mkdir -p "$LIBDIR" "$BINDIR" "$DATA/applications" "$(dirname "$ICON")"
rm -rf "$LIBDIR/ircam"
cp -r "$SRC/ircam" "$LIBDIR/"
find "$LIBDIR" -name __pycache__ -prune -exec rm -rf {} +
[[ -f "$SRC/LICENSE" ]] && cp "$SRC/LICENSE" "$LIBDIR/"
python3 -m compileall -q "$LIBDIR/ircam" >/dev/null 2>&1 || true

cat > "$BIN" <<SH
#!/bin/sh
exec env PYTHONPATH="$LIBDIR" python3 -m ircam "\$@"
SH
chmod +x "$BIN"

sed "s|@BIN@|$BIN|g" "$SRC/data/$APP_ID.desktop" > "$DESKTOP"
cp "$SRC/data/$APP_ID.svg" "$ICON"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true

# quick self-test: the package imports with the installed layout
PYTHONPATH="$LIBDIR" python3 -c "import ircam.engine, ircam.sources" 2>/dev/null \
  || warn "Installed, but the app failed to import — run '$BIN' in a terminal to see why."

# ---------------------------------------------------------------- USB permissions (optional)
if [[ ! -f $UDEV_RULE ]] && [[ -d /etc/udev/rules.d ]]; then
  info "A udev rule gives the 'video' group raw USB access to the P2 Pro (range switching) and Thermal Master P3."
  if ask "Install $UDEV_RULE with sudo?"; then
    sudo install -m 644 "$SRC/data/99-ircam-linux.rules" "$UDEV_RULE"
    sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb || true
    info "✓ udev rule installed (re-plug the camera)"
  else
    info "Skipped. Later: sudo install -m 644 data/99-ircam-linux.rules $UDEV_RULE"
  fi
fi

bold "✓ Installed IRCAM Thermal Viewer for Linux $(PYTHONPATH="$LIBDIR" python3 -c 'import ircam;print(ircam.__version__)' 2>/dev/null)"
info "Open \"IRCAM Thermal Viewer (Native)\" from your app launcher, or run: ircam-linux   (ircam-linux --demo without a camera)"
case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *) warn "$BINDIR is not in your PATH — the launcher entry works, but to use 'ircam-linux' in a terminal add it to PATH." ;;
esac
info "Pick your camera model in the drop-down and press Connect."
info "Update: run the same command again.  Uninstall: add --uninstall."
