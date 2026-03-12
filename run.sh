#!/usr/bin/env bash
#
# run.sh — build and run the CUA agent container
#
# Works with both Docker and Podman.  Detects which runtime is
# available and adjusts host-access flags accordingly.
#
# Usage:
#   ./run.sh build              Build the container image
#   ./run.sh start [OPTS...]    Start an interactive container (VNC on :5900)
#   ./run.sh agent "task"       Run the agent with a task and exit
#   ./run.sh stop               Stop and remove the container
#   ./run.sh shell              Open a bash shell inside the running container
#   ./run.sh logs               Tail container logs
#   ./run.sh --help             Show this help
#
# Extra OPTS after "start" or "agent" are passed directly to the
# container runtime (e.g. --env-file .env, -e CUA_MODEL=foo).
#
set -euo pipefail

IMAGE_NAME="${CUA_IMAGE:-cua-agent}"
CONTAINER_NAME="${CUA_CONTAINER:-cua}"
VNC_PORT="${CUA_VNC_PORT:-5900}"

# ── Detect runtime ────────────────────────────────────────
detect_runtime() {
    if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
        echo "docker"
    elif command -v podman &>/dev/null; then
        echo "podman"
    else
        echo >&2 "Error: neither docker nor podman found in PATH."
        exit 1
    fi
}

RUNTIME="$(detect_runtime)"

# ── Host-access flags ─────────────────────────────────────
# Allow the container to reach services on the host via a well-known
# hostname.  The mechanism differs between Docker and Podman.
host_access_flags() {
    if [ "$RUNTIME" = "podman" ]; then
        # Podman 4.5+ exposes host.containers.internal automatically
        # with slirp4netns:allow_host_loopback.  Also add the Docker-
        # compatible alias so OPENAI_BASE_URL can use either name.
        echo "--network slirp4netns:allow_host_loopback=true"
    else
        # Docker on Linux needs --add-host; Docker Desktop (macOS/Win)
        # resolves it natively but the flag is harmless there too.
        echo "--add-host=host.docker.internal:host-gateway"
    fi
}

# ── Commands ──────────────────────────────────────────────

cmd_build() {
    echo "Building image '${IMAGE_NAME}' with ${RUNTIME}..."
    $RUNTIME build -t "${IMAGE_NAME}" .
}

cmd_start() {
    echo "Starting container '${CONTAINER_NAME}' (VNC on port ${VNC_PORT})..."
    # shellcheck disable=SC2046
    $RUNTIME run -d \
        --name "${CONTAINER_NAME}" \
        $(host_access_flags) \
        -p "${VNC_PORT}:5900" \
        -v "${CUA_AUDIT_DIR:-./audit}:/app/audit:z" \
        "$@" \
        "${IMAGE_NAME}"
    echo ""
    echo "Container running.  Connect VNC to localhost:${VNC_PORT}"
    echo "Run a task:  ./run.sh agent \"your task here\""
    echo "Open shell:  ./run.sh shell"
    echo "Stop:        ./run.sh stop"
}

cmd_agent() {
    local task="$1"; shift
    # If the container is already running, exec into it
    if $RUNTIME ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$" || \
       $RUNTIME ps --format '{{.Names}}' 2>/dev/null | grep -qw "${CONTAINER_NAME}"; then
        echo "Running agent inside existing container '${CONTAINER_NAME}'..."
        $RUNTIME exec -it "${CONTAINER_NAME}" \
            /app/agent.py "${task}"
    else
        echo "Starting new container for one-shot agent run..."
        # shellcheck disable=SC2046
        $RUNTIME run --rm \
            --name "${CONTAINER_NAME}-run" \
            $(host_access_flags) \
            -p "${VNC_PORT}:5900" \
            -v "${CUA_AUDIT_DIR:-./audit}:/app/audit:z" \
            "$@" \
            "${IMAGE_NAME}" agent "${task}"
    fi
}

cmd_stop() {
    echo "Stopping container '${CONTAINER_NAME}'..."
    $RUNTIME stop "${CONTAINER_NAME}" 2>/dev/null || true
    $RUNTIME rm "${CONTAINER_NAME}" 2>/dev/null || true
    echo "Done."
}

cmd_shell() {
    $RUNTIME exec -it "${CONTAINER_NAME}" /bin/bash
}

cmd_logs() {
    $RUNTIME logs -f "${CONTAINER_NAME}"
}

cmd_help() {
    cat <<'EOF'
Usage: ./run.sh <command> [options...]

Commands:
  build              Build the container image
  start [OPTS...]    Start a background container (VNC on :5900)
                     Extra OPTS are passed to docker/podman run.
                     Example: ./run.sh start --env-file .env
  agent "task"       Run the agent with a task
                     If the container is running, execs into it.
                     Otherwise starts a one-shot container.
  stop               Stop and remove the container
  shell              Open a bash shell in the running container
  logs               Tail container logs

Environment variables:
  CUA_IMAGE          Image name         (default: cua-agent)
  CUA_CONTAINER      Container name     (default: cua)
  CUA_VNC_PORT       Host VNC port      (default: 5900)
  OPENAI_BASE_URL    LLM endpoint       (pass via -e or .env file)
  OPENAI_API_KEY     API key            (pass via -e or .env file)
  CUA_MODEL          Model name         (pass via -e or .env file)

Host access:
  Docker:  host.docker.internal  (added automatically)
  Podman:  host.containers.internal  (native with slirp4netns)
           host.docker.internal also works as a compat alias.

Examples:
  ./run.sh build
  ./run.sh start -e OPENAI_BASE_URL=http://host.docker.internal:11434/v1
  ./run.sh start --env-file .env
  ./run.sh agent "Open Chromium and search for AI on Wikipedia"
  ./run.sh stop
EOF
}

# ── Main ──────────────────────────────────────────────────

case "${1:-}" in
    build)
        cmd_build
        ;;
    start)
        shift
        cmd_start "$@"
        ;;
    agent)
        shift
        if [ $# -lt 1 ]; then
            echo >&2 "Error: 'agent' requires a task string."
            echo >&2 "Usage: ./run.sh agent \"your task here\" [OPTS...]"
            exit 1
        fi
        cmd_agent "$@"
        ;;
    stop)
        cmd_stop
        ;;
    shell)
        cmd_shell
        ;;
    logs)
        cmd_logs
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
