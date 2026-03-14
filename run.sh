#!/usr/bin/env bash
#
# run.sh — build and run the CUA agent via Docker/Podman Compose
#
# Auto-discovers compose extensions in compose.d/ and merges them
# with the base docker-compose.yml.
#
# Usage:
#   ./run.sh build              Build all images
#   ./run.sh up [OPTS...]       Start all services
#   ./run.sh down [OPTS...]     Stop and remove all services
#   ./run.sh agent "task"       Run the agent with a task
#   ./run.sh shell              Open a bash shell in the agent
#   ./run.sh logs [SERVICE]     Tail logs (all services or one)
#   ./run.sh --help             Show this help
#
set -euo pipefail

CONTAINER_NAME="${CUA_CONTAINER:-cua}"
COMPOSE_FILE="${CUA_COMPOSE_FILE:-docker-compose.yml}"

# ── Detect compose binary ─────────────────────────────────
detect_compose() {
    if command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
        echo "docker compose"
    elif command -v podman-compose &>/dev/null; then
        echo "podman-compose"
    elif command -v docker-compose &>/dev/null; then
        echo "docker-compose"
    else
        echo ""
    fi
}

COMPOSE="$(detect_compose)"
if [ -z "$COMPOSE" ]; then
    echo >&2 "Error: no compose command found."
    echo >&2 "Install 'docker compose' plugin, 'podman-compose', or 'docker-compose'."
    exit 1
fi

# ── Collect compose files ─────────────────────────────────
# Base file + all compose.d/*/compose.yml fragments.
# Directories starting with '.' are skipped (easy disable).
compose_flags() {
    if [ ! -f "${COMPOSE_FILE}" ]; then
        echo >&2 "Error: '${COMPOSE_FILE}' not found. Run from the project root."
        exit 1
    fi

    local flags="-f ${COMPOSE_FILE}"
    local compose_dir
    compose_dir="$(dirname "${COMPOSE_FILE}")/compose.d"

    if [ -d "${compose_dir}" ]; then
        for fragment in "${compose_dir}"/*/compose.yml; do
            [ -f "$fragment" ] || continue
            local dirname
            dirname="$(basename "$(dirname "$fragment")")"
            case "$dirname" in .*) continue ;; esac
            flags="${flags} -f ${fragment}"
        done
    fi
    echo "${flags}"
}

FLAGS="$(compose_flags)"

# Shorthand
run_compose() {
    # shellcheck disable=SC2086
    $COMPOSE ${FLAGS} "$@"
}

# ── Commands ──────────────────────────────────────────────

cmd_build() {
    echo "Building images..."
    run_compose build "$@"
}

cmd_up() {
    local count
    count=$(echo "$FLAGS" | tr ' ' '\n' | grep -c '^\-f' || true)
    local extensions=$((count - 1))

    echo "Starting services with ${COMPOSE}..."
    if [ "$extensions" -gt 0 ]; then
        echo "   Extensions: ${extensions} from compose.d/"
    fi

    # Create audit dir if the audit extension is active
    if [ -f "$(dirname "${COMPOSE_FILE}")/compose.d/audit/compose.yml" ]; then
        mkdir -p "${CUA_AUDIT_DIR:-./audit}"
    fi
    run_compose up -d --build "$@"
    echo ""
    echo "Services running.  Connect VNC to localhost:${CUA_VNC_PORT:-5900}"
    echo ""
    echo "  ./run.sh agent \"your task\"    Run a task"
    echo "  ./run.sh shell                 Agent shell"
    echo "  ./run.sh logs                  Tail logs"
    echo "  ./run.sh down                  Stop everything"
}

cmd_down() {
    echo "Stopping all services..."
    run_compose down "$@"
    echo "Done."
}

cmd_agent() {
    local task="$1"; shift
    local runtime
    runtime="${COMPOSE%% *}"  # "docker" from "docker compose"

    # Auto-start if container isn't running
    if ! $runtime ps --format '{{.Names}}' 2>/dev/null | grep -qw "${CONTAINER_NAME}"; then
        echo "Container '${CONTAINER_NAME}' is not running — starting services..."
        cmd_up
        # Give services a moment to stabilize
        echo "Waiting for services to be ready..."
        sleep 3
    fi

    # Pass extra flags (e.g. -e KEY=VAL) through to exec
    $runtime exec -it "$@" "${CONTAINER_NAME}" /app/agent.py "${task}"
}

cmd_shell() {
    local runtime
    runtime="${COMPOSE%% *}"
    $runtime exec -it "${CONTAINER_NAME}" /bin/bash
}

cmd_logs() {
    run_compose logs -f "$@"
}

cmd_help() {
    cat <<'EOF'
Usage: ./run.sh <command> [options...]

Commands:
  build [OPTS...]    Build all container images
  up [OPTS...]       Build and start all services
                     Auto-discovers compose.d/*/compose.yml
  down [OPTS...]     Stop and remove all services
  agent "task"       Run the agent with a task
  shell              Open a bash shell in the agent container
  logs [SERVICE]     Tail logs (all services, or a specific one)

Environment variables:
  CUA_CONTAINER      Agent container name   (default: cua)
  CUA_COMPOSE_FILE   Base compose file      (default: docker-compose.yml)
  CUA_AUDIT_DIR      Host audit path        (default: ./audit)
  CUA_VNC_PORT       Host VNC port          (default: 5900)
  OPENAI_BASE_URL    LLM endpoint           (pass via .env file)
  OPENAI_API_KEY     API key                (pass via .env file)
  CUA_MODEL          Model name             (pass via .env file)

Compose extensions (compose.d/):
  Drop a directory with a compose.yml into compose.d/ to add services.
  Disable by prefixing the directory with '.' (e.g. .xmpp/).
  See compose.d/README.md for details.

Examples:
  ./run.sh build
  ./run.sh up
  ./run.sh agent "Open Chromium and search for AI on Wikipedia"
  ./run.sh logs cua
  ./run.sh down

  # Disable XMPP without deleting it
  mv compose.d/xmpp compose.d/.xmpp
  ./run.sh up
EOF
}

# ── Main ──────────────────────────────────────────────────

case "${1:-}" in
    build)
        shift
        cmd_build "$@"
        ;;
    up)
        shift
        cmd_up "$@"
        ;;
    down)
        shift
        cmd_down "$@"
        ;;
    agent)
        shift
        if [ $# -lt 1 ]; then
            echo >&2 "Error: 'agent' requires a task string."
            echo >&2 "Usage: ./run.sh agent \"your task here\""
            exit 1
        fi
        cmd_agent "$@"
        ;;
    shell)
        cmd_shell
        ;;
    logs)
        shift
        cmd_logs "$@"
        ;;
    --help|-h|help|"")
        cmd_help
        ;;
    *)
        echo >&2 "Unknown command: $1"
        cmd_help
        exit 1
        ;;
esac
