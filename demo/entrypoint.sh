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
# Demo 2: Coverage benchmark
# ═══════════════════════════════════════════════════════════════
demo_coverage() {
    local out="$DEMO_DIR/coverage"
    mkdir -p "$out"
    local timeout_secs=$(( ${DEMO_COVERAGE_MINUTES:-5} * 60 ))
    log "Demo: Coverage benchmark (${DEMO_COVERAGE_MINUTES:-5}min = ${timeout_secs}s)"

    local url="${COVERAGE_URL:-http://coverage-target/}"

    # Coverage runs inside the cua-agent container (needs /app/agent.py etc.)
    # Pass URL and timeout as env vars to avoid nested quoting issues.
    asciinema rec -c "
        docker run --rm \
            --name bench-coverage-demo \
            --network coverage-net \
            --entrypoint bash \
            --add-host host.docker.internal:host-gateway \
            -e OPENAI_BASE_URL=${OPENAI_BASE_URL:-http://host.docker.internal:8000/v1} \
            -e OPENAI_API_KEY=${OPENAI_API_KEY:-not-needed} \
            -e CUA_MODEL=${CUA_MODEL:-} \
            -e CUA_MAX_STEPS=${CUA_MAX_STEPS:-50} \
            -e CUA_START_URL=file:///app/calibration/index.html \
            -e COV_URL=$url \
            -e COV_TIMEOUT=$timeout_secs \
            -p ${VNC_PORT:-5900}:5900 \
            cua-agent -c '
                /app/start.sh &
                sleep 4
                exec python3 -u /app/benchmark/coverage.py \
                    --url \$COV_URL \
                    --timeout \$COV_TIMEOUT
            '
    " \
        --overwrite "$out/terminal.cast" \
        || err "Coverage benchmark exited with $?"

    log "Coverage demo complete → $out/"
    ls -lh "$out/"
}

# ═══════════════════════════════════════════════════════════════
# Demo 3: Wikipedia via XMPP
# ═══════════════════════════════════════════════════════════════
demo_wikipedia() {
    local out="$DEMO_DIR/wikipedia"
    mkdir -p "$out"
    log "Demo: Wikipedia task via XMPP"
    log "This demo requires a cua-agent with the XMPP plugin configured."
    log "The agent needs XMPP_JID and XMPP_HTTP_HOST set."
    log ""
    log "Start the agent manually first, e.g.:"
    log "  docker run -d --name cua-wiki-demo \\"
    log "    -e CUA_MODEL=\$CUA_MODEL \\"
    log "    -e OPENAI_BASE_URL=\$OPENAI_BASE_URL \\"
    log "    -e XMPP_JID=agent@your-server \\"
    log "    -e XMPP_HTTP_HOST=your-server:5280 \\"
    log "    -p 5900:5900 \\"
    log "    --add-host host.docker.internal:host-gateway \\"
    log "    cua-agent"
    log ""
    log "Then run: ./demo.sh wikipedia"
    log ""

    # Check if XMPP vars are set
    if [ -z "${XMPP_USER_JID:-}" ] || [ -z "${XMPP_AGENT_JID:-}" ]; then
        err "XMPP_USER_JID and XMPP_AGENT_JID must be set."
        err "Example:"
        err "  XMPP_USER_JID=user@localhost XMPP_AGENT_JID=agent@localhost ./demo.sh wikipedia"
        return 1
    fi

    # Extract XMPP server host from JID
    local xmpp_host="${XMPP_USER_JID#*@}"
    local xmpp_port="${XMPP_PORT:-5222}"

    log "Checking XMPP server at $xmpp_host:$xmpp_port..."
    if ! timeout 5 bash -c "echo >/dev/tcp/$xmpp_host/$xmpp_port" 2>/dev/null; then
        err "Cannot reach XMPP server at $xmpp_host:$xmpp_port"
        err "Make sure the agent is running with XMPP enabled."
        return 1
    fi
    log "XMPP server reachable."

    # Write wrapper script (avoids quoting issues with apostrophe in "today's")
    local wrapper="$out/run_xmpp.sh"
    cat > "$wrapper" << WRAPPER_EOF
#!/bin/bash
python3 demo/xmpp_client.py \\
    --jid "${XMPP_USER_JID}" \\
    --password "${XMPP_USER_PASS:-demo_pass}" \\
    --to "${XMPP_AGENT_JID}" \\
    --host "$xmpp_host" \\
    --port "$xmpp_port" \\
    --message "Open wikipedia.org, click on the English Wikipedia link, and give me the link of today's featured article" \\
    --wait 180
WRAPPER_EOF
    chmod +x "$wrapper"

    log "Recording XMPP conversation..."
    asciinema rec -c "bash $wrapper" \
        --overwrite "$out/xmpp_chat.cast" \
        || err "XMPP client exited with $?"
    rm -f "$wrapper"

    log "Wikipedia demo complete → $out/"
    ls -lh "$out/"
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
