# cualm — Computer Use Agent (Lightweight, Model-agnostic)

## Quick Start

```bash
# Check environment (auto-runs on first use of any command)
./cualm setup

# Run a one-shot task
./cualm agent "Search for AI on Wikipedia"

# Start XMPP + orchestrator for phone access
./cualm serve

# Run benchmarks
./cualm benchmark --tasks 0-10
./cualm coverage --timeout 300

# Record demos
./cualm demo webarena
./cualm demo coverage
./cualm demo wikipedia

# Check status / stop
./cualm status
./cualm down
```

## Environment Detection

On first run, `cualm` detects your system and guides setup:

```
Checking environment...

  ✓ Container runtime: podman 5.3.1 (rootless)
  ✓ Runtime socket: /run/user/1000/podman/podman.sock
  ✓ Agent image: cua-agent:latest
  ✓ GPU: AMD RX 7900 XTX — ROCm (gfx1100)
  ✓ LLM server: http://localhost:8000/v1

  Environment ready.
```

If something is missing, it provides distro-specific install instructions
(Fedora, Ubuntu, Debian, Arch).

## Self-Tests

```bash
./cualm selftest              # levels 1-3 (fast, ~10s)
./cualm selftest --full       # levels 1-4 (includes end-to-end, ~2min)
./cualm selftest --level 2    # specific level only
```

**Level 1 — Infrastructure**: container runtime, networking
**Level 2 — Agent Image**: image exists, Xvfb + Chromium work
**Level 3 — LLM Server**: reachable, model responds
**Level 4 — End-to-End**: agent runs with mock LLM

## Testing

### Unit Tests (no Docker needed)

```bash
pip install pytest
pytest tests/unit/ -v
```

31 tests covering:
- **Evaluator** — string_match, exact_match, normalization, nested alternatives
- **Mock LLM** — scripted responses, request logging, multimodal support
- **Preflight** — system detection, install hints
- **XMPP Client** — XML body extraction, entity handling

### Integration Tests (requires Docker + built image)

```bash
pytest tests/integration/ -v -m slow
```

Tests the full agent loop with a mock LLM:
- Agent calls the LLM
- Screenshots included in requests
- Task completion via cua-done
- Error handling (empty responses, max_steps)

### Mock LLM Server

For development and testing:

```bash
# Standalone server with pre-built scripts
python3 tests/scripts/mock_llm.py --port 8111 --script navigate

# Programmatic use
from mock_llm import MockLLMServer

with MockLLMServer(script=[
    {"match": r"navigate", "response": 'run: cua-type "example.com"'},
    {"match": r"example", "response": 'run: cua-done "done" --result "OK"'},
]) as server:
    print(f"Mock LLM at: {server.url}")
    # Run agent against this...
    assert server.call_count == 2
```

Pre-built scripts: `navigate`, `form`, `calibration`, `echo`.

## Demo System

### WebArena Demo

Records the agent solving a WebArena task:

```bash
CUA_MODEL=your-model DEMO_TASK=0 ./demo.sh webarena
```

Outputs: `terminal.cast` (asciinema), `steps.mp4` (timelapse), `trace.html`

### Coverage Demo

Records the agent exploring a web app for code coverage:

```bash
CUA_MODEL=your-model DEMO_COVERAGE_MINUTES=5 ./demo.sh coverage
```

Runs directly on the host via `benchmark.sh --coverage`. Outputs: `terminal.log`, `steps.mp4`, `audit/` screenshots.

### Wikipedia Demo

Records the agent completing a task via XMPP:

```bash
CUA_MODEL=your-model ./demo.sh wikipedia
```

Starts Prosody + orchestrator via `./run.sh up`, sends a task via XMPP,
captures agent screenshots via `docker exec`, generates timelapse.

Outputs: `xmpp_chat.log`, `screenshots/`, `steps.mp4`

### All Demos

```bash
CUA_MODEL=your-model ./demo.sh all
```

## Configuration

Via environment variables or `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `CUA_MODEL` | *(required)* | LLM model name |
| `OPENAI_BASE_URL` | `http://localhost:8000/v1` | LLM API endpoint |
| `OPENAI_API_KEY` | `not-needed` | API key (if required) |
| `CUA_MAX_STEPS` | `40` | Max agent steps |
| `CUA_AGENT_TIMEOUT` | `600` | Task timeout (seconds) |
| `DEMO_TASK` | `0` | WebArena task ID for demo |
| `DEMO_COVERAGE_MINUTES` | `5` | Coverage demo duration |
| `DEMO_OUTPUT` | `./demo_out` | Demo output directory |
| `XMPP_HUMAN_PASSWORD` | `user-secret` | XMPP user password |
| `XMPP_HTTP_HOST` | *(empty)* | Host:port for image uploads (e.g. `192.168.50.198:5280`) |

### URL Handling

`demo.sh` automatically rewrites `localhost` → `host.docker.internal` in
`OPENAI_BASE_URL` for containerized demos (webarena, wikipedia). The
original URL is preserved for host-side operations (coverage benchmark).

## File Structure

```
cualm                     # CLI entry point
cualm_preflight.py        # Environment detection
cualm_selftest.py         # Sanity tests
demo.sh                   # Demo orchestrator
demo/
  compose.yml             # Demo runner container config
  entrypoint.sh           # Container entrypoint (webarena only)
  xmpp_client.py          # Raw socket XMPP client (no slixmpp)
  agent_capture.py        # Screenshot capture from task containers
  screenshots_to_video.py # Timelapse generator
  record_trace.py         # Trace viewer video capture
tests/
  scripts/mock_llm.py     # Scripted mock LLM server
  unit/
    test_evaluator.py     # Evaluator tests
    test_mock_llm.py      # Mock server tests
    test_preflight.py     # Preflight tests
    test_xmpp_client.py   # XMPP helper tests
  integration/
    test_navigation.py    # Agent navigation tests
pytest.ini                # Test configuration
```
