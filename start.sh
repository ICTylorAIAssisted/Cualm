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

# ── Port forwarding (benchmark mode) ─────────────────────────────
# CUA_PORT_FORWARDS maps localhost ports to WebArena site containers
# so that sites' hardcoded base URLs (e.g. http://localhost:7770)
# work from inside the agent container.
# Format: "7770:shopping:80,7780:shopping_admin:80,..."
if [ -n "${CUA_PORT_FORWARDS:-}" ]; then
    IFS=',' read -ra FORWARDS <<< "$CUA_PORT_FORWARDS"
    for fwd in "${FORWARDS[@]}"; do
        IFS=':' read -r LOCAL_PORT REMOTE_HOST REMOTE_PORT <<< "$fwd"
        socat "TCP-LISTEN:${LOCAL_PORT},fork,reuseaddr" \
              "TCP:${REMOTE_HOST}:${REMOTE_PORT}" &
        echo "Forward: localhost:${LOCAL_PORT} → ${REMOTE_HOST}:${REMOTE_PORT}"
    done
    sleep 1
fi

# In agent mode, always start with the calibration page — agent.py
# navigates to CUA_START_URL after calibration completes.
# In non-agent mode (e.g. human testing), open CUA_START_URL directly.
if [ "$1" = "agent" ]; then
    START_URL="file:///app/calibration/index.html"
else
    START_URL="${CUA_START_URL:-file:///app/calibration/index.html}"
fi

# Optional HTTP proxy (mitmproxy for HAR capture)
# --proxy-bypass-list=<-loopback> forces localhost traffic through
# the proxy too — needed because WebArena sites redirect to localhost
# URLs and we need those requests in the HAR trace.
PROXY_ARGS=""
if [ -n "${CUA_HTTP_PROXY:-}" ]; then
    PROXY_ARGS="--proxy-server=$CUA_HTTP_PROXY --proxy-bypass-list=<-loopback>"
fi

# Chromium — note: just "chromium" on Debian, not "chromium-browser"
chromium \
    --start-fullscreen \
    --no-first-run \
    --no-default-browser-check \
    --disable-gpu \
    --disable-software-rasterizer \
    --disable-dev-shm-usage \
    --no-sandbox \
    --test-type \
    --window-size="${SCREEN_WIDTH},${SCREEN_HEIGHT}" \
    --start-maximized \
    $PROXY_ARGS \
    "$START_URL" 2>/dev/null &
sleep 2

if [ "$1" = "agent" ]; then
    exec /app/agent.py "${@:2}"
else
    echo "Ready. Run: /app/agent.py 'your task'"
    tail -f /dev/null
fi
