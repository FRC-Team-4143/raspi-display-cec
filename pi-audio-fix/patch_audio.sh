#!/usr/bin/env bash
#
# patch-audio.sh
#
# Disables PipeWire/WirePlumber's idle-suspend for ALSA audio outputs.
# This fixes the common Raspberry Pi symptom where short/event sounds
# get dropped after a period of silence, but play fine once other
# audio is already active (the sink has to "wake up" from suspend,
# and that wake-up eats the first bit of a new short sound).
#
# Works with both the old Lua-based WirePlumber config (< 0.5.x) and
# the newer .conf-based format (>= 0.5.x) — it detects your version
# and writes the right drop-in automatically.
#
# Usage: sudo ./patch-audio.sh
# (run as the user who normally runs the kiosk, with sudo available;
#  if invoked via sudo directly it will target $SUDO_USER for the
#  service restart step)

set -euo pipefail

echo "== PipeWire/WirePlumber idle-suspend disable script =="
echo

if ! command -v wireplumber >/dev/null 2>&1; then
  echo "ERROR: 'wireplumber' command not found. This script only applies" >&2
  echo "to systems using PipeWire + WirePlumber for audio." >&2
  exit 1
fi

WP_VERSION="$(wireplumber --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"
if [ -z "$WP_VERSION" ]; then
  echo "ERROR: could not determine WirePlumber version from 'wireplumber --version'." >&2
  exit 1
fi
WP_MAJOR_MINOR="$(echo "$WP_VERSION" | cut -d. -f1,2)"

echo "Detected WirePlumber version: $WP_VERSION"

# Decide config format: >= 0.5 uses the new .conf drop-in style,
# older versions use the Lua drop-in style.
if [ "$(printf '%s\n' "0.5" "$WP_MAJOR_MINOR" | sort -V | head -n1)" = "0.5" ]; then
  CONF_DIR="/etc/wireplumber/wireplumber.conf.d"
  CONF_FILE="$CONF_DIR/51-disable-suspend.conf"
  echo "Using new-style .conf drop-in: $CONF_FILE"
  sudo mkdir -p "$CONF_DIR"
  sudo tee "$CONF_FILE" > /dev/null <<'EOF'
monitor.alsa.rules = [
  {
    matches = [
      { node.name = "~alsa_output.*" }
    ]
    actions = {
      update-props = {
        session.suspend-timeout-seconds = 0
      }
    }
  }
]
EOF
else
  CONF_DIR="/etc/wireplumber/main.lua.d"
  CONF_FILE="$CONF_DIR/51-disable-suspend.lua"
  echo "Using old-style Lua drop-in: $CONF_FILE"
  sudo mkdir -p "$CONF_DIR"
  sudo tee "$CONF_FILE" > /dev/null <<'EOF'
alsa_monitor.rules = {
  {
    matches = {
      {
        { "node.name", "matches", "alsa_output.*" },
      },
    },
    apply_properties = {
      ["session.suspend-timeout-seconds"] = 0,
    },
  },
}
EOF
fi

echo "Config written to $CONF_FILE"
echo

# Figure out which user's session to restart services for.
TARGET_USER="${SUDO_USER:-$USER}"
TARGET_UID="$(id -u "$TARGET_USER" 2>/dev/null || true)"

echo "Attempting to restart pipewire/wireplumber for user: $TARGET_USER"

if [ -n "$TARGET_UID" ] && sudo -u "$TARGET_USER" \
     XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
     systemctl --user restart wireplumber pipewire pipewire-pulse 2>/dev/null; then
  echo "Services restarted successfully."
else
  echo "Could not restart the user services automatically."
  echo "This is common on kiosk/autologin setups without a full login session bus."
  echo "Reboot the Pi instead to apply the change:  sudo reboot"
fi

echo
echo "Done. To verify: leave the Pi completely silent for 20-30 seconds,"
echo "then trigger an event sound in your app. It should play immediately"
echo "and in full, without needing other audio playing first."