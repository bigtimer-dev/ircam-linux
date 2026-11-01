#!/usr/bin/env bash
# Copy the bridge to a Raspberry Pi over SSH and install it there.
#
#   bridge/deploy.sh pi@p2pro.local            # install / update
#   bridge/deploy.sh pi@p2pro.local --uninstall
#
# Needs SSH access to the Pi (set up in Raspberry Pi Imager) and sudo rights for that user.
set -euo pipefail
TARGET=${1:?usage: bridge/deploy.sh user@host [install-pi.sh options]}
shift
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Copying the bridge to $TARGET:ircam-bridge-src"
tar -C "$ROOT" -cz --exclude=__pycache__ --exclude=ircam/ui --exclude=ircam/data \
    bridge ircam data/99-ircam-linux.rules \
  | ssh "$TARGET" 'rm -rf ircam-bridge-src && mkdir -p ircam-bridge-src && tar -xz -C ircam-bridge-src'

echo "==> Installing (sudo on the Pi)"
ssh -t "$TARGET" "sudo bash ircam-bridge-src/bridge/install-pi.sh $*"
