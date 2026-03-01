#!/bin/bash
set -e

# Start virtual framebuffer (suppress xkb warnings)
Xvfb :99 -screen 0 ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH} \
    -ac +extension GLX +render -noreset 2>/dev/null &
sleep 1

# Fluxbox (suppress config warnings)
fluxbox -display :99 2>/dev/null &
sleep 1

# Start system dbus daemon
mkdir -p /run/dbus
dbus-daemon --system --fork 2>/dev/null || true

# Start session bus
eval $(dbus-launch --sh-syntax)

# VNC for debugging
x11vnc -display :99 -forever -nopw -rfbport 5900 -bg -q
echo "VNC available on port 5900"

# Chromium — note: just "chromium" on Debian, not "chromium-browser"
chromium \
    --no-first-run \
    --no-default-browser-check \
    --disable-gpu \
    --disable-software-rasterizer \
    --disable-dev-shm-usage \
    --no-sandbox \
    --window-size=${SCREEN_WIDTH},${SCREEN_HEIGHT} \
    --start-maximized \
    "about:blank" 2>/dev/null &
sleep 2

if [ "$1" = "agent" ]; then
    exec /app/venv/bin/python3 /app/agent.py "${@:2}"
else
    echo "Ready. Run: python3 /app/agent.py 'your task'"
    tail -f /dev/null
fi
