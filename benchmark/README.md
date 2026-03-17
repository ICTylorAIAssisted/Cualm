# WebArena Benchmark Integration

Run the [WebArena](https://github.com/web-arena-x/webarena) benchmark against the CUA agent to measure computer-use performance on reproducible, locally-hosted web environments.

Supports two evaluation modes:
- **Legacy (Phase 1)** — built-in `string_match` evaluator, no extra dependencies
- **WebArena-Verified (Phase 3)** — deterministic offline evaluation via [webarena-verified](https://github.com/ServiceNow/webarena-verified), with HAR capture for leaderboard-submittable results

## Quick Start

```bash
# 1. Download task definitions
curl -L -o benchmark/tasks/test.raw.json \
  https://raw.githubusercontent.com/web-arena-x/webarena/main/config_files/test.raw.json

# 2. Build the agent image
./run.sh build

# 3a. Phase 1 — string_match only (no extra deps)
./benchmark.sh --site shopping --tasks 0-20

# 3b. Phase 3 — full WA-Verified evaluation (leaderboard-ready)
pip install webarena-verified
./benchmark.sh --webarena-verified --tasks 0-50
```

## Prerequisites

- Docker with Compose v2
- Python 3.10+ with `docker` package (`pip install docker`)
- The `cua-agent` Docker image built (`./run.sh build`)
- ~10 GB disk for site images, ~4 GB RAM for sites + 2 GB for agent
- For Phase 3: `pip install webarena-verified`

## Evaluation Modes

### Legacy Mode (Phase 1)

Uses built-in `string_match` evaluation. Only covers tasks whose eval type is `string_match` — `url_match` and `program_html` tasks are always scored as failures.

```bash
./benchmark.sh --site shopping --tasks 0-20
```

### WebArena-Verified Mode (Phase 3)

Uses the `webarena-verified` package for deterministic offline evaluation. Automatically enables HAR capture (per-task mitmproxy sidecar). Covers **all** eval types and produces leaderboard-submittable results.

```bash
./benchmark.sh --webarena-verified --tasks 0-50
```

This mode:
1. Spawns a mitmproxy container alongside each agent container
2. Routes Chromium traffic through the proxy to capture a HAR trace
3. Translates the agent's `cua-done --result` into WA-Verified's structured JSON schema
4. Calls `webarena-verified` to evaluate both the agent response and network trace
5. Writes per-task output in WA-Verified's expected format

### HAR Capture Only

Capture HAR traces without running WA-Verified evaluation (for later offline eval):

```bash
./benchmark.sh --har-capture --tasks 0-50

# Evaluate offline later:
webarena-verified eval-tasks \
  --config benchmark/results/wa_config.json \
  --output-dir benchmark/results
```

## CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--tasks SPEC` | `all` | `"all"`, `"0-50"`, `"108"`, `"108,200,305"` |
| `--site SITE` | all Phase 1 | `shopping`, `shopping_admin`, `reddit`, `gitlab` |
| `--eval-type TYPE` | all | `string_match`, `url_match`, `program_html` |
| `--max-steps N` | 30 | Agent steps per task |
| `--timeout N` | 180 | Seconds per task |
| `--model NAME` | `$CUA_MODEL` | LLM model name |
| `--verbose` | off | Print agent stdout in real-time |
| `--har-capture` | off | Enable per-task HAR capture via mitmproxy |
| `--webarena-verified` | off | WA-Verified evaluation (implies `--har-capture`) |
| `--all-sites` | off | Include Wikipedia/Map tasks (if those sites are running) |

## Output Structure

### Legacy mode

```
benchmark/results/
  report_<timestamp>.json    # Aggregate report with per-task results
```

### WebArena-Verified mode

```
benchmark/results/
  wa_config.json             # WA-Verified config (for offline re-eval)
  report_<timestamp>.json    # Aggregate report
  <task_id>/
    agent_response.json      # Structured agent response
    network.har              # HAR trace from mitmproxy
```

This output structure is directly compatible with `webarena-verified eval-tasks` for offline re-evaluation.

## Architecture

```
Host machine
├── benchmark/run.py              # Orchestrates everything
│
├── Docker network: webarena-net
│   ├── Agent container            # Xvfb + Chromium + agent.py (ephemeral)
│   │   └── Chromium ──proxy──→ mitmproxy container (ephemeral)
│   ├── shopping:7770
│   ├── shopping_admin:7780
│   ├── reddit:9999
│   ├── gitlab:8023
│   └── homepage:4399
│
├── Per-task flow:
│   1. Start mitmproxy container (if --har-capture)
│   2. Start agent container (with CUA_HTTP_PROXY set)
│   3. Agent runs: screenshot → LLM → xdotool loop
│   4. Agent completes → read cua_done.json
│   5. Stop mitmproxy → extract HAR file
│   6. Build agent_response.json
│   7. Evaluate (WA-Verified API or legacy string_match)
│   8. Remove both containers
```

## Agent Response Translation

WebArena-Verified requires a structured JSON response:

```json
{
  "task_type": "RETRIEVE",
  "status": "SUCCESS",
  "retrieved_data": ["$42.50"],
  "error_details": null
}
```

The benchmark runner automatically translates the agent's freeform `cua-done --result "$42.50"` into this format. Task type (`RETRIEVE` / `NAVIGATE` / `MUTATE`) is inferred from the task intent using keyword heuristics.

## Troubleshooting

**`webarena-verified` not found:** `pip install webarena-verified`

**mitmproxy image not found:** The runner pulls it automatically, but you can pre-pull: `docker pull mitmproxy/mitmproxy`

**HAR extraction fails:** The proxy container may have exited before writing the HAR. Check Docker logs: `docker logs bench-proxy-<task_id>-*`

**Agent can't reach sites through proxy:** Ensure both the proxy and agent containers are on `webarena-net`. The proxy URL uses the container name as hostname.

**SSL errors through proxy:** mitmproxy is started with `--ssl-insecure` to accept self-signed certs from the WebArena sites.

**GitLab takes forever to start:** Normal — GitLab needs 2-5 minutes. The wrapper script polls and waits up to 5 minutes.
