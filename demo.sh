#!/bin/bash
# Record demos of the CUA agent.
#
# Same pattern as benchmark.sh — only requires docker (or podman).
# All tools (asciinema, ffmpeg, Playwright) run inside a container.
#
# Usage:
#   ./demo.sh webarena           # Demo 1: WebArena task
#   ./demo.sh coverage           # Demo 2: Coverage benchmark
#   ./demo.sh wikipedia          # Demo 3: Wikipedia via XMPP
#   ./demo.sh all                # Run all demos
#
# Configuration (via environment or .env):
#   CUA_MODEL               LLM model name
#   OPENAI_BASE_URL          LLM API endpoint
#   DEMO_TASK                WebArena task ID (default: 0)
#   DEMO_COVERAGE_MINUTES    Coverage duration (default: 5)
#   DEMO_OUTPUT              Output directory (default: ./demo_out)

set -e
cd "$(dirname "$0")"

# ── Load .env (same file docker compose uses) ──
if [ -f .env ]; then
    set -a
    # shellcheck source=/dev/null
    source .env
    set +a
fi

MODE="${1:-help}"

log() { echo -e "\033[1;36m▶ $*\033[0m"; }
err() { echo -e "\033[1;31m✗ $*\033[0m" >&2; }

if [ "$MODE" = "help" ] || [ "$MODE" = "--help" ] || [ "$MODE" = "-h" ]; then
    echo "Usage: ./demo.sh {webarena|coverage|wikipedia|all}"
    echo ""
    echo "Only requires docker (or podman). Everything else runs inside containers."
    echo ""
    echo "Environment:"
    echo "  CUA_MODEL             ${CUA_MODEL:-(not set)}"
    echo "  OPENAI_BASE_URL       ${OPENAI_BASE_URL:-(not set)}"
    echo "  DEMO_TASK             ${DEMO_TASK:-0}"
    echo "  DEMO_OUTPUT           ${DEMO_OUTPUT:-./demo_out}"
    exit 0
fi

# ── Defaults ──
: "${OPENAI_BASE_URL:=http://localhost:8000/v1}"
export OPENAI_BASE_URL

# Container-friendly URL: localhost/127.0.0.1 → host.docker.internal
# Used by compose-based demos (webarena, wikipedia).
# Coverage runs on the host and uses the original URL.
OPENAI_BASE_URL_CONTAINER="${OPENAI_BASE_URL//localhost/host.docker.internal}"
OPENAI_BASE_URL_CONTAINER="${OPENAI_BASE_URL_CONTAINER//127.0.0.1/host.docker.internal}"
export OPENAI_BASE_URL_CONTAINER

# Clear CUA_AUDIT_DIR to prevent compose volume issues
# (it's set temporarily only during coverage demo)
unset CUA_AUDIT_DIR

# Socket path (compose needs raw path for the volume mount)
if [ -n "${DOCKER_HOST:-}" ]; then
    export DOCKER_SOCKET="${DOCKER_HOST#unix://}"
else
    export DOCKER_SOCKET="/var/run/docker.sock"
fi
# Orchestrator compose uses CUA_RUNTIME_SOCKET
export CUA_RUNTIME_SOCKET="$DOCKER_SOCKET"

# Absolute paths (avoids relative path issues with podman-compose)
export PROJECT_ROOT="$(pwd)"

# Output directory
export DEMO_OUTPUT="${DEMO_OUTPUT:-./demo_out}"
mkdir -p "$DEMO_OUTPUT"
export DEMO_OUTPUT="$(cd "$DEMO_OUTPUT" && pwd)"

echo "▶ Config:"
echo "  Socket:  $DOCKER_SOCKET"
echo "  Output:  $DEMO_OUTPUT"
echo "  Task:    ${DEMO_TASK:-0}"
echo "  Model:   ${CUA_MODEL:-(from .env)}"
echo "  LLM:     $OPENAI_BASE_URL"
if [ "$OPENAI_BASE_URL" != "$OPENAI_BASE_URL_CONTAINER" ]; then
    echo "  LLM (containers): $OPENAI_BASE_URL_CONTAINER"
fi

if [ ! -S "$DOCKER_SOCKET" ]; then
    echo "⚠ Socket not found: $DOCKER_SOCKET"
    echo "  Set DOCKER_HOST or start the Docker/Podman socket service."
    exit 1
fi

COMPOSE_FILE="demo/compose.yml"
WEBARENA_COMPOSE="compose.d/.webarena/compose.yml"
COVERAGE_COMPOSE="compose.d/.coverage/compose.yml"

# ── Build ──
echo ""
echo "▶ Building demo runner..."
docker compose -f "$COMPOSE_FILE" build demo-runner

# ── Start sites as needed ──
SITES_STARTED=false
COVERAGE_STARTED=false

start_webarena() {
    if [ -f "$WEBARENA_COMPOSE" ]; then
        echo "▶ Starting WebArena sites..."
        docker compose -f "$WEBARENA_COMPOSE" up -d
        echo "  Waiting for sites to initialize..."
        sleep 15
        SITES_STARTED=true
    else
        echo "⚠ WebArena compose file not found: $WEBARENA_COMPOSE"
    fi
}

start_coverage() {
    if [ -f "$COVERAGE_COMPOSE" ]; then
        echo "▶ Starting coverage target..."

        # Generate nginx.conf from template (same as benchmark.sh)
        local llm_base="${OPENAI_BASE_URL:-http://localhost:11434}"
        local llm_port
        llm_port=$(python3 -c "
from urllib.parse import urlparse
u = urlparse('$llm_base')
print(u.port or (443 if u.scheme == 'https' else 80))
")
        local nginx_template="compose.d/.coverage/nginx.conf.template"
        local nginx_conf="compose.d/.coverage/nginx.conf"
        if [ -f "$nginx_template" ]; then
            sed "s/LLM_PORT_PLACEHOLDER/${llm_port}/g" "$nginx_template" > "$nginx_conf"
            echo "  LLM gateway: port $llm_port"
        fi

        docker compose -f "$COVERAGE_COMPOSE" up -d
        echo "  Waiting for target to initialize..."
        sleep 5
        COVERAGE_STARTED=true
    else
        echo "⚠ Coverage compose file not found: $COVERAGE_COMPOSE"
    fi
}

stop_sites() {
    if [ "$SITES_STARTED" = true ]; then
        echo "▶ Stopping WebArena sites..."
        docker compose -f "$WEBARENA_COMPOSE" down 2>/dev/null || true
    fi
    if [ "$COVERAGE_STARTED" = true ]; then
        echo "▶ Stopping coverage target..."
        docker compose -f "$COVERAGE_COMPOSE" down 2>/dev/null || true
    fi
}

demo_wikipedia() {
    local out="$DEMO_DIR/wikipedia"
    mkdir -p "$out"
    log "Demo: Wikipedia task via XMPP"

    # Start all services via run.sh (handles compose merging,
    # Prosody, orchestrator, agent, networking)
    log "Starting services via run.sh..."
    OPENAI_BASE_URL="$OPENAI_BASE_URL_CONTAINER" ./run.sh up
    log "Waiting for XMPP to initialize..."
    sleep 12
    log "VNC: localhost:${CUA_VNC_PORT:-5900}"

    # Start screenshot capture in background — discovers the
    # orchestrator's spawned agent container and captures via docker exec
    log "Starting agent screenshot capture..."
    python3 demo/agent_capture.py \
        --output "$out/screenshots" \
        --interval 3 &
    local capture_pid=$!

    # Write client script (single-quoted heredoc — no expansion issues)
    cat > "$out/run_client.sh" << 'CLIENTEOF'
#!/bin/bash
python3 demo/xmpp_client.py \
    --jid "user@cua.local" \
    --password "${XMPP_HUMAN_PASSWORD:-user-secret}" \
    --to "orchestrator@cua.local" \
    --host "127.0.0.1" \
    --port 5222 \
    --message "Go to wikipedia.org, click on the English Wikipedia link, then click on today's featured article and tell me its title" \
    --wait 300
CLIENTEOF

    log "Sending task via XMPP..."
    XMPP_HUMAN_PASSWORD="${XMPP_HUMAN_PASSWORD:-user-secret}" \
        bash "$out/run_client.sh" \
        2>&1 | tee "$out/xmpp_chat.log"

    # Stop screenshot capture
    kill "$capture_pid" 2>/dev/null
    wait "$capture_pid" 2>/dev/null

    # Generate timelapse from captured screenshots
    local num_screenshots
    num_screenshots=$(find "$out/screenshots" -name "*.png" 2>/dev/null | wc -l)
    if [ "$num_screenshots" -gt 0 ]; then
        log "Creating timelapse from $num_screenshots screenshots..."
        if command -v ffmpeg &>/dev/null; then
            ffmpeg -y -framerate 2 \
                -pattern_type glob -i "$out/screenshots/step_*.png" \
                -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" \
                -c:v libx264 -pix_fmt yuv420p \
                "$out/steps.mp4" 2>/dev/null \
                && log "Timelapse: $out/steps.mp4" \
                || err "Timelapse generation failed"
        else
            log "ffmpeg not found — skipping timelapse (screenshots saved)"
        fi
    fi

    # Cleanup
    log "Stopping services..."
    ./run.sh down

    log "Wikipedia demo complete → $out/"
    ls -lh "$out/"
}

# ── Start sites as needed ──
case "$MODE" in
    webarena) start_webarena ;;
esac

# ── Set up output dir ──
DEMO_DIR="$DEMO_OUTPUT/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$DEMO_DIR"

# ── Run ──
echo "▶ Running demo: $MODE"

run_coverage_host() {
    local cov_out="$DEMO_DIR/coverage"
    mkdir -p "$cov_out/audit"
    echo "▶ Recording coverage demo → $cov_out/terminal.log"
    local timeout_secs=$(( ${DEMO_COVERAGE_MINUTES:-5} * 60 ))

    # Mount audit dir so screenshots persist after the agent exits
    export CUA_AUDIT_DIR="$cov_out/audit"

    ./benchmark.sh --coverage --timeout $timeout_secs 2>&1 | tee "$cov_out/terminal.log" \
        || echo "  Coverage exited with $?"

    unset CUA_AUDIT_DIR

    # Generate screenshot timelapse if audit data was captured
    local session_dir
    session_dir=$(find "$cov_out/audit" -maxdepth 1 -name "session_*" -type d | head -1)
    if [ -n "$session_dir" ]; then
        echo "▶ Creating screenshot timelapse..."
        docker run --rm \
            --security-opt label=disable \
            -v "$cov_out:/demo_out:z" \
            -v "$PROJECT_ROOT:/project:ro,z" \
            -w /project \
            demo_demo-runner \
            python3 demo/screenshots_to_video.py /demo_out/audit \
                -o /demo_out/steps.mp4 --fps 0.5 \
            || echo "  Timelapse generation failed"
    fi

    echo "▶ Coverage demo complete → $cov_out/"
    ls -lh "$cov_out/"
}

case "$MODE" in
    webarena)
        docker compose -f "$COMPOSE_FILE" run --rm demo-runner webarena
        ;;
    coverage)
        run_coverage_host
        ;;
    wikipedia)
        demo_wikipedia
        ;;
    all)
        start_webarena
        docker compose -f "$COMPOSE_FILE" run --rm demo-runner webarena
        stop_sites
        SITES_STARTED=false
        run_coverage_host
        demo_wikipedia
        ;;
esac

# ── Cleanup ──
stop_sites

echo ""
echo "▶ Demo complete!"
echo "  Outputs: $DEMO_OUTPUT/"
ls -la "$DEMO_OUTPUT/" 2>/dev/null || true
