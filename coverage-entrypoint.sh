#!/bin/bash
# Entrypoint for the cua-agent service in compose. Brings up the
# in-container browser stack (Xvfb + Chromium via start.sh), waits
# briefly for Chromium to expose its CDP port, then runs the coverage
# benchmark with knobs read from environment variables.
set -e

# Boot Xvfb + fluxbox + Chromium in the background.
CUA_START_URL=file:///app/calibration/index.html /app/start.sh &
sleep 4

# Compose coverage.py args from env.
ARGS=(--url "${TARGET_URL:-http://coverage-target/}")
ARGS+=(--max-steps "${CUA_MAX_STEPS:-50}")
ARGS+=(--timeout "${COVERAGE_TIMEOUT:-900}")
ARGS+=(--snapshot-interval "${COVERAGE_SNAPSHOT_INTERVAL:-1}")
[ -n "${COVERAGE_TARGET_PCT:-}" ] && ARGS+=(--target "${COVERAGE_TARGET_PCT}")
[ -n "${COVERAGE_CREDENTIALS:-}" ] && ARGS+=(--credentials "${COVERAGE_CREDENTIALS}")
[ -n "${COVERAGE_JSON_OUT:-}" ]   && ARGS+=(--json "${COVERAGE_JSON_OUT}")

exec python3 -u /app/benchmark/coverage.py "${ARGS[@]}"
