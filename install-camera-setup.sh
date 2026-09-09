#!/usr/bin/env bash
set -euo pipefail

# install-camera-setup.sh - installs camera-setup.sh and its udev rule so the
# camera is configured via v4l2-ctl whenever it appears (boot or hotplug).
#
# Usage:
#   sudo bash install-camera-setup.sh [--script-dir /usr/local/bin] [--no-trigger] [--no-deps]

SCRIPT_DIR=/usr/local/bin
RULES_DIR=/etc/udev/rules.d
SCRIPT_FILE=camera-setup.sh
RULES_FILE=99-camera-setup.rules
DO_TRIGGER=1
INSTALL_DEPS=1

print_usage(){
  cat <<EOF
Usage: sudo bash install-camera-setup.sh [--script-dir DIR] [--no-trigger] [--no-deps]

Defaults:
  --script-dir: $SCRIPT_DIR   (where camera-setup.sh is installed)
  --no-trigger: skip 'udevadm trigger' after install (rule still applies on next replug/boot)
  --no-deps:    do not apt-get install v4l-utils

Examples:
  sudo bash install-camera-setup.sh
  sudo bash install-camera-setup.sh --script-dir /opt/cec-scheduler/bin --no-trigger
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --script-dir) SCRIPT_DIR="$2"; shift 2;;
    --no-trigger) DO_TRIGGER=0; shift 1;;
    --no-deps)    INSTALL_DEPS=0; shift 1;;
    -h|--help)    print_usage; exit 0;;
    *) echo "Unknown arg: $1"; print_usage; exit 2;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "This installer must be run as root. Use sudo."
  exit 1
fi

if ! command -v udevadm >/dev/null 2>&1; then
  echo "udevadm not found. This installer requires a system with udev."
  exit 1
fi

for f in "$SCRIPT_FILE" "$RULES_FILE"; do
  if [[ ! -f "$f" ]]; then
    echo "Required file '$f' not found. Run this from the repository directory."
    exit 1
  fi
done

# Install v4l-utils (provides v4l2-ctl) unless disabled
if [[ $INSTALL_DEPS -eq 1 ]]; then
  if command -v v4l2-ctl >/dev/null 2>&1; then
    echo "v4l2-ctl already present"
  elif command -v apt-get >/dev/null 2>&1; then
    echo "Installing OS package dependency: v4l-utils"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y v4l-utils
  else
    echo "apt-get not found and v4l2-ctl missing; install v4l-utils manually."
  fi
else
  echo "Skipping v4l-utils installation (--no-deps)"
fi

# Install the configuration script
install -d "$SCRIPT_DIR"
install -m 0755 "$SCRIPT_FILE" "$SCRIPT_DIR/$SCRIPT_FILE"
echo "Installed $SCRIPT_DIR/$SCRIPT_FILE"

# Install the udev rule, rewriting the RUN+= path to the chosen script dir
RULES_DEST="$RULES_DIR/$RULES_FILE"
if [[ -f "$RULES_DEST" ]]; then
  echo "Backing up existing rule to ${RULES_DEST}.bak"
  cp -v "$RULES_DEST" "${RULES_DEST}.bak"
fi
sed "s|/usr/local/bin/camera-setup.sh|$SCRIPT_DIR/$SCRIPT_FILE|g" "$RULES_FILE" > "$RULES_DEST"
chmod 644 "$RULES_DEST"
echo "Installed $RULES_DEST"

# Reload udev
udevadm control --reload
echo "Reloaded udev rules"

if [[ $DO_TRIGGER -eq 1 ]]; then
  udevadm trigger --subsystem-match=video4linux --action=add
  echo "Triggered video4linux add events"
fi

cat <<EOF

Installation complete.
- Script: $SCRIPT_DIR/$SCRIPT_FILE
- Rule:   $RULES_DEST

Next steps:
1. Confirm the rule matches your camera. Inspect attributes with:
     udevadm info -a -n /dev/video0
   and edit the ATTRS{...} clauses in $RULES_DEST if needed, then:
     sudo udevadm control --reload
2. Edit the v4l2-ctl calls in $SCRIPT_DIR/$SCRIPT_FILE for your camera.
3. Watch it run:
     journalctl -t camera-setup -f
   then replug the camera (or reboot).
EOF
