#!/usr/bin/env bash
# Convenience wrapper — runs: ./benchmark.sh --coverage "$@"
set -euo pipefail
cd "$(dirname "$0")"
exec ./benchmark.sh --coverage "$@"
