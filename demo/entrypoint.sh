#!/bin/bash
# Entrypoint for the demo-runner container.
# Runs inside Docker with the socket mounted, so it can launch
# agent containers. Sites are managed by demo.sh on the host.
#
# Calls Python scripts directly (not benchmark.sh) to avoid
# docker compose dependency inside the container.

set -e

DEMO_DIR="/output/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$DEMO_DIR"

log() { echo -e "\033[1;36m▶ $*\033[0m"; }
err() { echo -e "\033[1;31m✗ $*\033[0m" >&2; }

# ═══════════════════════════════════════════════════════════════
# Demo 1: WebArena task
# ═══════════════════════════════════════════════════════════════
demo_webarena() {
    local out="$DEMO_DIR/webarena"
    mkdir -p "$out"
    log "Demo: WebArena task $DEMO_TASK"

    # Debug: show env vars the agent will use
    log "Environment:"
    echo "  CUA_MODEL=$CUA_MODEL"
    echo "  OPENAI_BASE_URL=$OPENAI_BASE_URL"
    echo "  OPENAI_API_KEY=${OPENAI_API_KEY:+(set)}"
    echo "  DOCKER_HOST=$DOCKER_HOST"

    # Record terminal — call run.py directly (sites already running on host)
    log "Recording benchmark run..."
    asciinema rec -c "\
        python3 benchmark/run.py \
            --tasks ${DEMO_TASK:-0} \
            --model ${CUA_MODEL:-default} \
            --llm-base-url ${OPENAI_BASE_URL:-http://host.docker.internal:8000/v1}" \
        --overwrite "$out/terminal.cast" \
        || err "Benchmark exited with $? (recording saved)"

    # Generate screenshot timelapse
    local task_dir
    task_dir=$(find benchmark/results/ -maxdepth 1 -name "$DEMO_TASK" -type d | head -1)
    if [ -n "$task_dir" ]; then
        log "Creating screenshot timelapse..."
        python3 demo/screenshots_to_video.py "$task_dir" \
            -o "$out/steps.mp4" --fps 0.5 || err "Timelapse failed"
    fi

    # Generate trace viewer HTML
    log "Generating trace viewer..."
    python3 benchmark/trace_viewer.py benchmark/results/ --all 2>/dev/null || true
    local trace_html
    trace_html=$(find benchmark/results/ -name "trace.html" | head -1)
    if [ -n "$trace_html" ]; then
        cp "$trace_html" "$out/trace.html"
        log "Recording trace viewer walkthrough..."
        python3 demo/record_trace.py "$out/trace.html" "$out/trace_scroll.mp4" \
            || err "Trace recording failed"
    fi

    log "WebArena demo complete → $out/"
    ls -lh "$out/"
}

# ═══════════════════════════════════════════════════════════════
# Demo 2: Coverage benchmark (runs on host via demo.sh)
# ═══════════════════════════════════════════════════════════════
demo_coverage() {
    err "Coverage demo runs on the host via benchmark.sh — not inside this container."
    exit 1
}

# ═══════════════════════════════════════════════════════════════
# Demo 3: Wikipedia via XMPP (runs on host via demo.sh)
# ═══════════════════════════════════════════════════════════════
demo_wikipedia() {
    err "Wikipedia demo runs on the host via demo.sh — not inside this container."
    exit 1
}

# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
case "${1:-help}" in
    webarena)  demo_webarena ;;
    coverage)  demo_coverage ;;
    wikipedia) demo_wikipedia ;;
    all)
        demo_webarena
        demo_coverage
        demo_wikipedia
        log "All demos → $DEMO_DIR/"
        ;;
    *)
        echo "Usage: docker compose -f demo/compose.yml run demo-runner {webarena|coverage|wikipedia|all}"
        exit 1
        ;;
esac
