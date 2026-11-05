#!/bin/sh
# Wait for the host desktop, fix the display mode, register on CEC, start the app.
SOCK="$XDG_RUNTIME_DIR/${WAYLAND_DISPLAY:-wayland-0}"
for i in $(seq 1 120); do [ -S "$SOCK" ] && break; sleep 1; done
[ -S "$SOCK" ] || echo "WARNING: $SOCK not found, mpv will not be able to draw"

wlr-randr --output "${TVREMOTE_OUTPUT:-HDMI-A-1}" --mode 1920x1080@60 || echo "WARNING: wlr-randr failed"
cec-ctl -d /dev/cec0 --playback -o "${TVREMOTE_CEC_NAME:-HunnidPi}" || echo "WARNING: cec-ctl failed"

exec python3 /opt/tvremote/app.py
