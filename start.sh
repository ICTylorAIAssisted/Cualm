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

# In agent mode, always start with the calibration page — agent.py
# navigates to CUA_START_URL after calibration completes.
# In non-agent mode (e.g. human testing), open CUA_START_URL directly.
if [ "$1" = "agent" ]; then
    START_URL="file:///app/calibration/index.html"
else
    START_URL="${CUA_START_URL:-file:///app/calibration/index.html}"
fi

# Set up Chrome profile with password manager disabled
CHROME_PROFILE="/tmp/chrome-profile"
mkdir -p "$CHROME_PROFILE/Default"
cat > "$CHROME_PROFILE/Default/Preferences" << 'PREFS'
{
  "credentials_enable_service": false,
  "profile": {
    "password_manager_enabled": false,
    "default_content_setting_values": {
      "notifications": 2
    }
  },
  "autofill": {
    "profile_enabled": false,
    "credit_card_enabled": false
  },
  "translate": {
    "enabled": false
  }
}
PREFS

# Chromium — note: just "chromium" on Debian, not "chromium-browser"
# --test-type suppresses the "developer mode extensions" warning banner
chromium \
    --start-fullscreen \
    --no-first-run \
    --no-default-browser-check \
    --disable-gpu \
    --disable-software-rasterizer \
    --disable-dev-shm-usage \
    --no-sandbox \
    --test-type \
    --remote-debugging-port=9222 \
    --remote-allow-origins=* \
    --load-extension=/app/extensions/cua-hud \
    --window-size="${SCREEN_WIDTH},${SCREEN_HEIGHT}" \
    --start-maximized \
    --user-data-dir="$CHROME_PROFILE" \
    --password-store=basic \
    --disable-features=PasswordManager,TranslateUI,AutofillServerCommunication \
    "$START_URL" 2>/dev/null &
sleep 2

if [ "$1" = "agent" ]; then
    exec /app/agent.py "${@:2}"
else
    echo "Ready. Run: /app/agent.py 'your task'"
    tail -f /dev/null
fi
