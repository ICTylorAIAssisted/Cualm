#!/bin/bash
# Start WebArena sites, run benchmark, stop sites.
#
# Usage:
#   ./benchmark.sh --site shopping --tasks 0-20
#   ./benchmark.sh --site shopping --tasks 0-50 --parallel 4  # 4 tasks at once
#   ./benchmark.sh --site shopping --tasks 0-50 --resume      # resume crashed run
#   ./benchmark.sh --webarena-verified --tasks 0-50
#   ./benchmark.sh --coverage                          # run coverage benchmark (bundled target)
#   ./benchmark.sh --coverage --url http://localhost:3000  # custom target
#   ./benchmark.sh --coverage-test                     # verify coverage tracking works
#   ./benchmark.sh --trace benchmark/results/0/        # generate trace viewer for task 0
#   ./benchmark.sh --trace benchmark/results/ --all     # generate viewers for all tasks
#   ./benchmark.sh down          # stop sites + remove leftover containers
#   ./benchmark.sh --cleanup     # remove leftover containers only
#
# Ctrl+C during a run will finish the current task, write a partial
# report, and clean up all containers before exiting.

set -e

cd "$(dirname "$0")"

COMPOSE_FILE="compose.d/.webarena/compose.yml"

# ── Load .env (same file docker compose uses) ────────────────
if [ -f .env ]; then
    set -a
    # shellcheck source=/dev/null
    source .env
    set +a
fi

# ── "down" subcommand: stop everything ────────────────────────
if [ "${1:-}" = "down" ]; then
    echo "Stopping WebArena sites..."
    docker compose -f "$COMPOSE_FILE" down 2>/dev/null || true
    echo "Cleaning up benchmark containers..."
    python3 benchmark/run.py --cleanup
    exit 0
fi

# ── "--trace" mode: generate trace viewer HTML ────────────────
if [ "${1:-}" = "--trace" ]; then
    shift
    if [ $# -eq 0 ]; then
        echo "Usage: ./benchmark.sh --trace <path> [--all] [-o output.html]"
        echo ""
        echo "  <path>  Session dir, task dir, or results dir (with --all)"
        echo "  --all   Process all task dirs under <path>"
        echo "  -o      Output file (default: trace.html in session dir)"
        exit 1
    fi
    exec python3 benchmark/trace_viewer.py "$@"
fi

# ── "--coverage-test" mode: verify coverage tracking works ────
if [ "${1:-}" = "--coverage-test" ]; then
    echo "╔══════════════════════════════════════╗"
    echo "║   Coverage Verification Test         ║"
    echo "╚══════════════════════════════════════╝"

    # Build agent image if needed
    if ! docker image inspect cua-agent >/dev/null 2>&1; then
        echo "Building CUA agent image..."
        ./run.sh build
    fi

    echo ""
    echo "── Test 1/2: Simple (4 equal functions) ──"
    docker run --rm \
        --name bench-coverage-test \
        --entrypoint bash \
        cua-agent -c '
            /app/start.sh &
            sleep 4
            python3 -u /app/benchmark/coverage-test/verify.py
        '
    SIMPLE_RC=$?

    echo ""
    echo "── Test 2/2: Complex (nesting, branches, closures, dead code) ──"
    docker run --rm \
        --name bench-coverage-test \
        --entrypoint bash \
        cua-agent -c '
            /app/start.sh &
            sleep 4
            python3 -u /app/benchmark/coverage-test/verify_complex.py
        '
    COMPLEX_RC=$?

    echo ""
    echo "══════════════════════════════════════"
    if [ $SIMPLE_RC -eq 0 ] && [ $COMPLEX_RC -eq 0 ]; then
        echo "  ✅ Both tests passed"
    else
        [ $SIMPLE_RC -ne 0 ] && echo "  ❌ Simple test failed (exit $SIMPLE_RC)"
        [ $COMPLEX_RC -ne 0 ] && echo "  ❌ Complex test failed (exit $COMPLEX_RC)"
    fi
    echo "══════════════════════════════════════"
    exit $(( SIMPLE_RC + COMPLEX_RC ))
fi

# ── "--coverage" mode: run coverage benchmark ─────────────────
if [ "${1:-}" = "--coverage" ]; then
    shift
    echo "╔══════════════════════════════════════╗"
    echo "║     Coverage Benchmark Mode          ║"
    echo "╚══════════════════════════════════════╝"

    COVERAGE_COMPOSE="compose.d/.coverage/compose.yml"
    COVERAGE_NGINX_TEMPLATE="compose.d/.coverage/nginx.conf.template"
    COVERAGE_NGINX="compose.d/.coverage/nginx.conf"

    # Build agent image if needed
    if ! docker image inspect cua-agent >/dev/null 2>&1; then
        echo "Building CUA agent image..."
        ./run.sh build
    fi

    # Collect remaining args
    COVERAGE_ARGS=("$@")

    # Check if --url is provided (external target)
    HAS_URL=false
    TARGET_URL=""
    RECALIBRATE=false
    FILTERED_ARGS=()
    for i in "${!COVERAGE_ARGS[@]}"; do
        if [ "${COVERAGE_ARGS[$i]}" = "--url" ]; then
            HAS_URL=true
            TARGET_URL="${COVERAGE_ARGS[$((i+1))]:-}"
        fi
        if [ "${COVERAGE_ARGS[$i]}" = "--recalibrate" ]; then
            RECALIBRATE=true
            continue  # don't pass to coverage.py
        fi
        FILTERED_ARGS+=("${COVERAGE_ARGS[$i]}")
    done
    COVERAGE_ARGS=("${FILTERED_ARGS[@]}")

    if ! $HAS_URL; then
        TARGET_URL="http://coverage-target/"
        COVERAGE_ARGS+=(--url "$TARGET_URL")
    fi

    CAL_VOLUME="${CUA_CALIBRATION_VOLUME:-cua-calibration}"

    # Clear calibration cache if requested
    if $RECALIBRATE; then
        echo "  Clearing calibration cache..."
        docker run --rm -v "${CAL_VOLUME}:/cal" alpine sh -c "rm -f /cal/*.json" 2>/dev/null \
            && echo "  Calibration cleared — will re-calibrate." \
            || echo "  ⚠ Could not clear calibration (volume may not exist yet)."
    fi

    # Extract LLM port and path from base URL
    LLM_BASE="${OPENAI_BASE_URL:-http://localhost:11434}"
    read LLM_PORT LLM_PATH <<< $(python3 -c "
from urllib.parse import urlparse
u = urlparse('$LLM_BASE')
port = u.port or (443 if u.scheme == 'https' else 80)
print(port, u.path or '/')
")

    # Agent talks to gateway, which forwards to host LLM
    LLM_BASE_VIA_GATEWAY="http://host.docker.internal:${LLM_PORT}${LLM_PATH}"

    # Generate nginx config from template
    sed "s/LLM_PORT_PLACEHOLDER/${LLM_PORT}/g" "$COVERAGE_NGINX_TEMPLATE" > "$COVERAGE_NGINX"
    echo "  LLM gateway: port $LLM_PORT → host"

    # Start coverage target + gateway
    if ! $HAS_URL; then
        echo "Starting coverage target + gateway..."
        docker compose -f "$COVERAGE_COMPOSE" up -d

        # Debug: check container states
        echo "  Container status:"
        docker ps --filter name=coverage --format '  {{.Names}}: {{.Status}}' 2>/dev/null || \
            docker ps --filter name=coverage 2>/dev/null

        # Wait for target
        echo -n "  Waiting for coverage target..."
        TARGET_READY=false
        for i in $(seq 1 60); do
            if docker exec coverage-target python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1/', timeout=2)" 2>/dev/null; then
                echo " ready"
                TARGET_READY=true
                break
            fi
            echo -n "."
            sleep 2
        done
        if ! $TARGET_READY; then
            echo " TIMEOUT"
            echo "  Debug: coverage-target logs:"
            docker logs coverage-target 2>&1 | tail -10
            echo "  Debug: listing /site in container:"
            docker exec coverage-target ls -la /site 2>&1 | head -5
        fi
    fi

    # Get gateway IP on coverage-net
    echo "  Resolving gateway IP..."
    GATEWAY_IP=$(python3 << 'PYEOF'
import subprocess, json, sys
try:
    out = subprocess.check_output(['docker', 'inspect', 'coverage-gateway'], text=True)
    info = json.loads(out)
    nets = info[0].get('NetworkSettings', {}).get('Networks', {})
    print('  Networks found: ' + ', '.join(nets.keys()), file=sys.stderr)
    for name, net in nets.items():
        ip = net.get('IPAddress', '')
        print(f'    {name}: IPAddress={ip}', file=sys.stderr)
    # Find coverage-net IP
    for name, net in nets.items():
        if 'coverage-net' in name:
            ip = net.get('IPAddress', '')
            if ip:
                print(ip)
                sys.exit(0)
    # Fallback: first non-empty IP
    for name, net in nets.items():
        ip = net.get('IPAddress', '')
        if ip:
            print(ip)
            sys.exit(0)
    print('  No IP found in any network', file=sys.stderr)
except Exception as e:
    print(f'  Error: {e}', file=sys.stderr)
PYEOF
)

    if [ -z "$GATEWAY_IP" ]; then
        echo "  ⚠ Could not determine gateway IP."
        echo "  Raw NetworkSettings.Networks:"
        docker inspect coverage-gateway 2>&1 | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    nets = data[0].get('NetworkSettings', {}).get('Networks', {})
    print(json.dumps(nets, indent=2))
except Exception as e:
    print(f'Parse error: {e}')
" 2>/dev/null
        exit 1
    fi
    echo "  Gateway IP: $GATEWAY_IP"

    cleanup_coverage() {
        echo ""
        if ! $HAS_URL; then
            echo "Stopping coverage target + gateway..."
            docker compose -f "$COVERAGE_COMPOSE" down 2>/dev/null || true
        fi
        docker rm -f bench-coverage 2>/dev/null || true
        rm -f "$COVERAGE_NGINX"
    }
    trap cleanup_coverage EXIT

    # Write args to temp file
    ARGS_FILE=$(mktemp)
    for arg in "${COVERAGE_ARGS[@]}"; do
        echo "$arg" >> "$ARGS_FILE"
    done

    echo ""
    echo "Launching agent → $TARGET_URL"
    echo ""

    docker run -it --rm \
        --name bench-coverage \
        --network coverage-net \
        --entrypoint bash \
        -v "$ARGS_FILE:/tmp/coverage_args:ro,z" \
        -v "${CAL_VOLUME}:/var/cua/calibration:z" \
        --add-host "host.docker.internal:${GATEWAY_IP}" \
        -e OPENAI_BASE_URL="$LLM_BASE_VIA_GATEWAY" \
        -e OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
        -e CUA_MODEL="${CUA_MODEL:-}" \
        -e CUA_TEMPERATURE="${CUA_TEMPERATURE:-}" \
        -e CUA_TOP_P="${CUA_TOP_P:-}" \
        -e CUA_PRESENCE_PENALTY="${CUA_PRESENCE_PENALTY:-}" \
        -e CUA_LLM_EXTRA_PARAMS="${CUA_LLM_EXTRA_PARAMS:-}" \
        -e CUA_MAX_STEPS="${CUA_MAX_STEPS:-50}" \
        -e CUA_START_URL="$TARGET_URL" \
        -p "${CUA_VNC_PORT:-5900}:5900" \
        cua-agent -c '
            CUA_START_URL=file:///app/calibration/index.html /app/start.sh &
            sleep 4
            ARGS=()
            while IFS= read -r line; do
                ARGS+=("$line")
            done < /tmp/coverage_args
            exec python3 -u /app/benchmark/coverage.py "${ARGS[@]}"
        '
    EXIT_CODE=$?
    rm -f "$ARGS_FILE"
    exit $EXIT_CODE
fi

# ── Preflight checks ──────────────────────────────────────────

if [ ! -f "$COMPOSE_FILE" ]; then
    echo "Error: $COMPOSE_FILE not found."
    echo "Run this script from the project root."
    exit 1
fi

if [ ! -f "benchmark/tasks/test.raw.json" ]; then
    echo "Task file not found. Downloading from WebArena repo..."
    mkdir -p benchmark/tasks
    curl -L -o benchmark/tasks/test.raw.json \
        "https://raw.githubusercontent.com/web-arena-x/webarena/main/config_files/test.raw.json"
    echo "Downloaded benchmark/tasks/test.raw.json"
fi

# Check Python dependencies
if ! python3 -c "import docker" 2>/dev/null; then
    echo "Installing benchmark dependencies..."
    pip install -r benchmark/requirements.txt --quiet
fi

# Check if --webarena-verified is in args
WA_VERIFIED=false
for arg in "$@"; do
    if [ "$arg" = "--webarena-verified" ]; then
        WA_VERIFIED=true
        break
    fi
done

if $WA_VERIFIED; then
    if ! python3 -c "import webarena_verified" 2>/dev/null; then
        echo "Installing webarena-verified..."
        pip install webarena-verified --quiet
    fi
    echo "WebArena-Verified mode enabled"
fi

# ── Start WebArena sites ──────────────────────────────────────

echo "Starting WebArena sites..."
docker compose -f "$COMPOSE_FILE" up -d

echo "Waiting for sites to initialize..."

MAX_WAIT=300
INTERVAL=10
elapsed=0

check_site() {
    curl -sf -o /dev/null --max-time 5 "$1" 2>/dev/null
}

while [ $elapsed -lt $MAX_WAIT ]; do
    all_ready=true

    for pair in "shopping|http://localhost:7770" \
                "shopping_admin|http://localhost:7780" \
                "reddit|http://localhost:9999" \
                "gitlab|http://localhost:8023"; do
        name="${pair%%|*}"
        url="${pair#*|}"
        if ! check_site "$url"; then
            all_ready=false
            break
        fi
    done

    if $all_ready; then
        echo "All sites are ready. (${elapsed}s)"
        break
    fi

    echo "  Waiting... (${elapsed}s / ${MAX_WAIT}s)"
    sleep $INTERVAL
    elapsed=$((elapsed + INTERVAL))
done

if [ $elapsed -ge $MAX_WAIT ]; then
    echo "Warning: Not all sites responded within ${MAX_WAIT}s."
    echo "Proceeding anyway — some tasks may fail."
fi

# ── Run benchmark (with cleanup trap) ─────────────────────────

cleanup() {
    echo ""
    echo "Stopping WebArena sites..."
    docker compose -f "$COMPOSE_FILE" down 2>/dev/null || true
}
trap cleanup EXIT

echo ""
echo "Running benchmark..."
echo "========================================"
python3 benchmark/run.py "$@"
exit_code=$?

exit $exit_code
