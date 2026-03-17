#!/bin/bash
# Start WebArena sites, run benchmark, stop sites.
#
# Usage:
#   ./benchmark.sh --site shopping --tasks 0-20
#   ./benchmark.sh --webarena-verified --tasks 0-50
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
