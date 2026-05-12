#!/usr/bin/env bash
# coverage.sh — standalone entry point for the coverage / monkey tester.
#
# Wraps `docker compose up` with friendlier flags. All knobs are also
# settable via .env or environment variables; flags override.
#
# Examples:
#   ./coverage.sh                                       # built-in demo site, 15 min
#   ./coverage.sh --url https://example.com --minutes 10
#   ./coverage.sh --url https://example.com --target 80 --output ./out --video
#   ./coverage.sh --recalibrate                         # wipe calibration cache first

set -euo pipefail
cd "$(dirname "$0")"

# ── Load .env if present ──
if [ -f .env ]; then
    set -a; source .env; set +a
fi

usage() {
    cat <<USAGE
Usage: ./coverage.sh [flags]

Coverage run:
  --url URL              Target URL (default: built-in demo site)
  --minutes N            Wall-clock timeout in minutes (default: 15)
  --max-steps N          Cap on agent steps (default: 50)
  --target PCT           Stop early when coverage % is reached
  --snapshot-interval N  Take a coverage snapshot every N steps (default: 1)
  --credentials USER:PASS  Inject credentials into the agent task

LLM (host-side OpenAI-compatible endpoint):
  --api-base URL         e.g. http://localhost:11434/v1
  --api-key KEY
  --model NAME

Output:
  --output DIR           Audit + screenshots land here (default: ./out)
  --video                Render an MP4 timelapse after the run
  --vnc-port PORT        Expose Chromium's VNC on this host port (default: 5900)
  --no-vnc               Don't expose VNC

Lifecycle:
  --recalibrate          Clear cached mouse/screen calibration before run
  --keep                 Don't 'docker compose down' on exit
  -h, --help             This message
USAGE
}

# ── Defaults / parse ──
MINUTES=15
WANT_VIDEO=0
RECALIBRATE=0
KEEP_UP=0
NO_VNC=0
EXTRA_ENV=()

while [ $# -gt 0 ]; do
    case "$1" in
        --url)                 export TARGET_URL="$2"; shift 2 ;;
        --minutes)             MINUTES="$2"; shift 2 ;;
        --max-steps)           export CUA_MAX_STEPS="$2"; shift 2 ;;
        --target)              export COVERAGE_TARGET_PCT="$2"; shift 2 ;;
        --snapshot-interval)   export COVERAGE_SNAPSHOT_INTERVAL="$2"; shift 2 ;;
        --credentials)         export COVERAGE_CREDENTIALS="$2"; shift 2 ;;
        --api-base)            API_BASE="$2"; shift 2 ;;
        --api-key)             export OPENAI_API_KEY="$2"; shift 2 ;;
        --model)               export CUA_MODEL="$2"; shift 2 ;;
        --output)              export OUTPUT_DIR="$2"; shift 2 ;;
        --video)               WANT_VIDEO=1; shift ;;
        --vnc-port)            export VNC_PORT="$2"; shift 2 ;;
        --no-vnc)              NO_VNC=1; shift ;;
        --recalibrate)         RECALIBRATE=1; shift ;;
        --keep)                KEEP_UP=1; shift ;;
        -h|--help)             usage; exit 0 ;;
        *) echo "Unknown flag: $1" >&2; usage; exit 2 ;;
    esac
done

export COVERAGE_TIMEOUT=$(( MINUTES * 60 ))
export OUTPUT_DIR="${OUTPUT_DIR:-./out}"
mkdir -p "$OUTPUT_DIR"

# ── Derive UPSTREAM_HOST/PORT/PATH from --api-base if given ──
# Otherwise rely on values already in .env / env.
if [ -n "${API_BASE:-}" ]; then
    read UPSTREAM_HOST UPSTREAM_PORT UPSTREAM_PATH <<< $(python3 - "$API_BASE" <<'PYEOF'
import sys
from urllib.parse import urlparse
u = urlparse(sys.argv[1])
host = u.hostname or "host.docker.internal"
# Rewrite host-local addresses so they reach the host from inside the gateway.
if host in ("localhost", "127.0.0.1", "::1"):
    host = "host.docker.internal"
port = u.port or (443 if u.scheme == "https" else 80)
path = u.path or "/v1"
print(host, port, path)
PYEOF
)
    export UPSTREAM_HOST UPSTREAM_PORT UPSTREAM_PATH
fi

# ── Optional: wipe calibration volume before run ──
if [ "$RECALIBRATE" = "1" ]; then
    echo "▶ Clearing calibration cache..."
    docker volume rm cua-calibration 2>/dev/null || true
fi

# ── Optional: drop VNC port mapping ──
COMPOSE_OVERRIDE=""
if [ "$NO_VNC" = "1" ]; then
    COMPOSE_OVERRIDE=$(mktemp --suffix=.yml)
    cat > "$COMPOSE_OVERRIDE" <<YAML
services:
  cua-agent:
    ports: !reset []
YAML
    trap 'rm -f "$COMPOSE_OVERRIDE"' EXIT
fi

# ── Run ──
COMPOSE_ARGS=(-f compose.yml)
[ -n "$COMPOSE_OVERRIDE" ] && COMPOSE_ARGS+=(-f "$COMPOSE_OVERRIDE")

cleanup() {
    [ "$KEEP_UP" = "1" ] && return
    echo ""
    echo "▶ Stopping containers..."
    docker compose "${COMPOSE_ARGS[@]}" down --remove-orphans 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "▶ Coverage run → target=${TARGET_URL:-<built-in>}  timeout=${MINUTES}min  output=$OUTPUT_DIR"
docker compose "${COMPOSE_ARGS[@]}" up --build \
    --abort-on-container-exit \
    --exit-code-from cua-agent \
    cua-agent coverage-target coverage-gateway \
    | tee "$OUTPUT_DIR/terminal.log"
RC=${PIPESTATUS[0]}

# ── Optional timelapse ──
if [ "$WANT_VIDEO" = "1" ]; then
    echo ""
    echo "▶ Rendering timelapse..."
    docker compose "${COMPOSE_ARGS[@]}" run --rm --no-deps \
        --entrypoint /app/screenshots_to_video.py \
        cua-agent /app/audit -o /app/audit/steps.mp4 --fps 0.5 \
        || echo "  (timelapse failed — non-fatal)"
fi

echo ""
echo "▶ Done. Exit code: $RC"
echo "  Output: $OUTPUT_DIR"
ls -lh "$OUTPUT_DIR" 2>/dev/null | head -20
exit "$RC"
