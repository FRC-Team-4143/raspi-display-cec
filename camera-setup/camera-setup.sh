#!/usr/bin/env bash
# camera-setup.sh - configure a V4L2 camera with v4l2-ctl.
#
# Invoked by the udev rule 99-camera-setup.rules whenever a matching
# /dev/video* device appears (boot or hotplug). Can also be run by hand:
#
#   sudo ./camera-setup.sh /dev/video0
#
# Edit the v4l2-ctl calls below to match your camera. List what a device
# supports with:  v4l2-ctl -d /dev/video0 --all --list-ctrls-menus
set -euo pipefail

DEV="${1:-/dev/video0}"
V4L2_CTL="$(command -v v4l2-ctl || echo /usr/bin/v4l2-ctl)"

log() { logger -t camera-setup -- "$*"; }

if [[ ! -e "$DEV" ]]; then
  log "device $DEV not present, nothing to do"
  exit 0
fi

# Only act on capture devices (a camera can expose several /dev/video* nodes,
# e.g. metadata nodes, which reject these ioctls).
if ! "$V4L2_CTL" -d "$DEV" --all 2>/dev/null | grep -q 'Video Capture'; then
  log "$DEV is not a video-capture node, skipping"
  exit 0
fi

log "configuring $DEV"

# --- Controls -------------------------------------------------------------
# Group related controls in one call; a bad control name fails the whole call,
# so keep unrelated settings on separate lines.
"$V4L2_CTL" -d "$DEV" --set-ctrl=auto_exposure=1 || \
  log "failed to disable autoexposure on $DEV"

"$V4L2_CTL" -d "$DEV" --set-ctrl=exposure_time_absolute=20 || \
  log "failed to set exposure time on $DEV"


log "done configuring $DEV"
