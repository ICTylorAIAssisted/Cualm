#!/bin/bash
set -e

# ── Sync config.ini screen values from ENV so there is one source of truth ──
# If SCREEN_WIDTH/SCREEN_HEIGHT/DISPLAY are overridden at runtime, the Python
# config must reflect the same values.
if [ -f /app/config.ini ]; then
    sed -i "s/^width *= *.*/width = ${SCREEN_WIDTH}/" /app/config.ini
    sed -i "s/^height *= *.*/height = ${SCREEN_HEIGHT}/" /app/config.ini
    sed -i "s/^display *= *.*/display = ${DISPLAY}/" /app/config.ini
fi

# Start virtual framebuffer (suppress xkb warnings)
Xvfb "${DISPLAY}" -screen 0 "${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH}" \
    -ac +extension GLX +render -noreset 2>/dev/null &
sleep 1

# Fluxbox (suppress config warnings)
fluxbox -display "${DISPLAY}" 2>/dev/null &
sleep 1

# Start system dbus daemon
mkdir -p /run/dbus
dbus-daemon --system --fork 2>/dev/null || true

# Start session bus
eval $(dbus-launch --sh-syntax)

# VNC for debugging
x11vnc -display "${DISPLAY}" -forever -nopw -rfbport 5900 -bg -q
echo "VNC available on port 5900"

# Chromium — note: just "chromium" on Debian, not "chromium-browser"
chromium \
    --no-first-run \
    --no-default-browser-check \
    --disable-gpu \
    --disable-software-rasterizer \
    --disable-dev-shm-usage \
    --no-sandbox \
    --window-size="${SCREEN_WIDTH},${SCREEN_HEIGHT}" \
    --start-maximized \
    "about:blank" 2>/dev/null &
sleep 2

if [ "$1" = "agent" ]; then
    exec /app/agent.py "${@:2}"
else
    echo "Ready. Run: /app/agent.py 'your task'"
    tail -f /dev/null
fi
