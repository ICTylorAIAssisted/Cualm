# cualm — Project State

**Date:** April 10, 2026 (updated)
**Purpose:** Complete reference for continuing development in a new conversation.

---

## 1. What This Project Is

**cualm** (Computer Use Agent, Lightweight & Model-agnostic) controls a Linux desktop via screenshot → LLM → shell command loop. Runs inside Docker with Xvfb, Chromium, and xdotool. Works with any OpenAI-compatible API.

Three modes of operation:
1. **One-shot tasks** — `./cualm agent "your task"` runs a single task
2. **XMPP/phone access** — `./cualm serve` starts Prosody + orchestrator, tasks via chat
3. **Benchmarks** — WebArena (812 web tasks) and Coverage (JS+CSS exploration)

---

## 2. Architecture Overview

### Agent Loop (agent.py)

```
For each step (up to MAX_STEPS):
  1. Inject ref badges (floating overlays via CDP)
  2. Take screenshot (scrot) — captures badges
  3. Fetch accessibility tree (CDP) — assigns ref IDs to all interactive elements
  4. Build prompt: task + summary + command output + plan + a11y tree + screenshot
  5. Call LLM (OpenAI-compatible API)
  6. Extract "run: <command>" from response
  7. Execute command in shell
  8. Extract text summary from <think> block
  9. Check for DONE_MARKER or loop detection
```

### Key Design Decisions

| Decision | Alternative | Why |
|----------|-------------|-----|
| Shell commands (`run: cmd`) | Function calling / JSON | Composability, model-agnostic, debuggable |
| Text summary replaces image history | Keep all screenshots | 5× token savings (80 vs 443 tokens) |
| Full plan rewrite | Incremental `--complete step N` | Models produce better full lists |
| Two-pass a11y (full walk + truncated render) | Single pass | Ref assignment needs all nodes; prompt needs truncation |
| Ref badges as floating overlays | Child elements | Doesn't break page layout |
| Playwright via `connect_over_cdp` | Direct xdotool | Reliable element interaction by ref ID |
| Coverage daemon (persistent websocket) | Reconnect per call | CDP profiler state is per-session |
| Raw socket XMPP client | slixmpp library | TLS/STARTTLS compat with Prosody self-signed certs |
| Demo coverage runs on host | Inside container | `benchmark.sh` manages its own compose lifecycle |
| Demo wikipedia uses `run.sh up/down` | Manual compose merging | Handles Podman compat, compose.d/ extensions |

### Network Isolation (Benchmarks)

```
┌──────────────────────────────────────────────────┐
│         internal network (no internet)            │
│  target sites  ←──→  nginx gateway  ←──→  agent  │
└────────────────────────┬─────────────────────────┘
                         │
         nginx gateway → host.docker.internal → LLM
```

### XMPP Architecture

```
Phone (Conversations)  →  Prosody  →  Orchestrator  →  spawns agent container
                          ↑                                    │
                          └──── result + screenshot ←──────────┘
```

---

## 3. File Structure

### Core Agent

| File | Purpose |
|------|---------|
| `agent.py` | Main agent loop, prompt template, LLM call, history management |
| `cdp_a11y.py` | Accessibility tree via CDP — ref assignment, badge injection, truncation |
| `cua_config.py` | Config loader, calibration path helpers |
| `plugin_host.py` | Plugin system with hooks |
| `tool_discovery.py` | Auto-discovers tools from `--help` for prompt injection |
| `config.ini` | Screen size, LLM params, delays, calibration settings |
| `start.sh` | Xvfb + Fluxbox + VNC + Chromium + socat launcher |
| `Dockerfile` | Debian bookworm, Chromium, Python, Playwright |

### CLI & Environment

| File | Purpose |
|------|---------|
| `cualm` | CLI entry point — agent, serve, benchmark, coverage, demo, selftest, setup, status, down |
| `cualm_preflight.py` | System detection (OS, GPU, runtime, socket, image, LLM) + per-distro install hints |
| `cualm_selftest.py` | 4-level sanity tests (infrastructure → image → LLM → end-to-end) |

### Documentation

| File | Purpose |
|------|---------|
| `README.md` | Project overview, quick start, feature summary |
| `CUALM.md` | Detailed CLI, testing, demo, and configuration reference |
| `COURSE.md` | 19-module course on building a CUA from scratch |
| `PROJECT_STATE.md` | This file — complete reference for continuing development |
| `docs/` | Generated GitHub Pages demo showcase (via `demo/generate_site.py`) |

### Tools (`tools/`)

| Tool | Purpose |
|------|---------|
| `cua-click` | Click at coordinates (with calibration mapping) |
| `cua-type` | Type text via xdotool |
| `cua-key` | Key combos |
| `cua-scroll` | Scroll with mousewheel |
| `cua-drag` | Drag from A to B |
| `cua-wait` | Wait (capped at 3s) |
| `cua-screenshot` | Screenshot capture |
| `cua-plan` | Full-rewrite planning |
| `cua-done` | Task completion (checks plan) |
| `cua-pw` | Playwright click/fill/select/check by ref ID |
| `cua-cdp-js` | JS execution via CDP (auto-fixes const/let, unwraps console.log) |
| `cua-cdp-coverage` | JS+CSS coverage recording via CDP |
| `cua-help` | Show tool help |

### Plugins (`plugins/`)

| File | Purpose |
|------|---------|
| `audit.py` | Step-by-step audit: screenshots, trace.json, metadata.json |
| `usage_tracking.py` | Token usage tracking with logprobs/entropy |
| `xmpp/` | XMPP daemon plugin (connects to Prosody, receives/sends messages) |

### Benchmark (`benchmark/`)

| File | Purpose |
|------|---------|
| `run.py` | WebArena runner — parallel execution, resume, task spawning |
| `config.py` | URL templates, credentials, port forwards, env var defaults |
| `gateway.py` | Nginx gateway for network isolation |
| `evaluate.py` | Task evaluation (string_match, exact_match with normalization) |
| `verified.py` | WA-Verified integration (auto_eval with HAR traces) |
| `har.py` | mitmproxy addon for HAR capture with URL rewriting |
| `coverage.py` | Coverage benchmark orchestrator |
| `trace_viewer.py` | HTML trace viewer generator |
| `re_evaluate.py` | Re-score existing results with current evaluator |

### Compose Extensions (`compose.d/`)

| Directory | Purpose |
|-----------|---------|
| `.webarena/` | 5 WebArena site containers |
| `.coverage/` | Coverage target + nginx gateway |
| `xmpp/` | Prosody XMPP server (auto-discovered by `run.sh`) |
| `orchestrator/` | Task orchestrator (spawns agent containers per XMPP message) |
| `audit/` | Bind-mount audit directory |

### Demo System (`demo/`)

| File | Purpose |
|------|---------|
| `Dockerfile` | Demo runner image (asciinema, ffmpeg, Playwright, docker-cli) |
| `compose.yml` | Demo container config with all CUA env vars forwarded |
| `entrypoint.sh` | Container entrypoint (webarena only; coverage/wikipedia run on host) |
| `xmpp_client.py` | Raw socket XMPP client — STARTTLS, SASL PLAIN, MUC join, completion detection |
| `agent_capture.py` | Screenshots from orchestrator-spawned task containers via `docker exec` |
| `screenshots_to_video.py` | Timelapse generator from audit screenshots |
| `record_trace.py` | Trace viewer video capture via Playwright |
| `generate_site.py` | GitHub Pages showcase generator from demo output |

### Tests (`tests/`)

| File | Tests | Coverage |
|------|-------|----------|
| `scripts/mock_llm.py` | — | Scripted mock LLM server (regex matching, request logging) |
| `unit/test_evaluator.py` | 22 | string_match, exact_match, normalization, nested alternatives |
| `unit/test_mock_llm.py` | 9 | Server lifecycle, scripted responses, multimodal, callable |
| `unit/test_preflight.py` | 10 | System detection, install hints, `_run` helper |
| `unit/test_xmpp_client.py` | 17 | XML parsing, completion regex, MUC handling |
| `unit/test_selftest.py` | 9 | Test runner structure, runtime detection |
| `unit/test_cualm_cli.py` | 7 | CLI help output, subcommand parsing |
| `integration/test_navigation.py` | 4 | Agent e2e with mock LLM (requires Docker) |

---

## 4. Environment Variables

### Agent reads:

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENAI_BASE_URL` | `http://localhost:8000/v1` | LLM endpoint |
| `OPENAI_API_KEY` | `not-needed` | API key |
| `CUA_MODEL` | *(required)* | Model identifier |
| `CUA_MAX_STEPS` | `50` | Max agent steps |
| `CUA_AGENT_TIMEOUT` | `600` | Task timeout in seconds |
| `CUA_TEMPERATURE` | `0.7` | LLM temperature |
| `CUA_TOP_P` | `0.95` | top_p |
| `CUA_PRESENCE_PENALTY` | `1.5` | Presence penalty |
| `CUA_LLM_EXTRA_PARAMS` | `{}` | Extra JSON for `extra_body` |
| `CUA_START_URL` | *(calibration page)* | URL for Chromium |
| `CUA_PORT_FORWARDS` | | socat spec: `7770:shopping:80,...` |
| `CUA_HTTP_PROXY` | | mitmproxy URL for HAR capture |
| `CUA_HISTORY_PAIRS` | `0` | Old message pairs to keep |
| `CUA_SHOW_REFS` | `1` | Show/hide ref badges (set `0` to disable) |
| `CUA_AUDIT_DIR` | | Bind-mount for audit data (coverage demo) |

### XMPP / Orchestrator:

| Variable | Default | Purpose |
|----------|---------|---------|
| `XMPP_JID` | | Agent's XMPP JID |
| `XMPP_PASSWORD` | | Agent's XMPP password |
| `XMPP_PEER_JID` | | Human user's JID |
| `XMPP_HOST` | | XMPP server hostname |
| `XMPP_PORT` | `5222` | XMPP server port |
| `XMPP_VERIFY_CERT` | `true` | TLS cert verification |
| `XMPP_AUTO_START` | `false` | Start XMPP daemon on agent boot |
| `XMPP_HTTP_HOST` | | External host:port for image uploads |
| `XMPP_HUMAN_PASSWORD` | `user-secret` | Human user password (Prosody) |
| `XMPP_AGENT_PASSWORD` | `agent-secret` | Agent password (Prosody) |

### Demo:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DEMO_TASK` | `0` | WebArena task ID |
| `DEMO_COVERAGE_MINUTES` | `5` | Coverage demo duration |
| `DEMO_OUTPUT` | `./demo_out` | Output directory |

---

## 5. Ref-Based Element Interaction

Two-pass approach in `cdp_a11y.py`:
1. `_assign_all_refs()` — walks full tree, assigns `e1`, `e2`, ... to ALL interactive elements
2. `_render_tree()` — renders truncated text for prompt (MAX_CHILDREN=15, MAX_LINES=250, MAX_DEPTH=10)

Refs stored in `/tmp/cua_a11y_refs.json` with `backendDOMNodeId`.

Visual badges: floating overlay divs positioned via `getBoundingClientRect()`. Viewport-only (skip off-screen), occlusion check via `elementFromPoint`, `aria-hidden="true"`, CSS visibility check. Cleaned up before each step via `_cleanup_ref_overlays()`.

Agent loop order: a11y tree (injects badges) → screenshot (captures badges).

`cua-pw` resolves refs via CDP `DOM.resolveNode` + `data-cua-ref` tagging.

---

## 6. Demo System

### WebArena Demo
`demo.sh` starts sites on host → demo container runs `benchmark/run.py` directly → generates timelapse + trace viewer.

### Coverage Demo
Runs on host via `benchmark.sh --coverage` (it manages its own compose). `CUA_AUDIT_DIR` persists screenshots. Timelapse generated via `demo_demo-runner` container.

### Wikipedia Demo
`demo.sh` calls `./run.sh up` (starts Prosody + orchestrator + agent) → runs XMPP client on host → `agent_capture.py` captures screenshots from spawned task containers → ffmpeg timelapse → `./run.sh down`.

### GitHub Pages Showcase
`demo/generate_site.py` processes demo output into a static site:
```bash
python3 demo/generate_site.py ./demo_out -o docs/
# → Enable GitHub Pages on docs/ in repo settings
```
Parses XMPP logs, coverage traces, selects representative screenshots (8 of 80 for wikipedia, 6 of 10 for coverage), generates dark-themed HTML with embedded videos, chat view, and screenshot gallery with lightbox. Total site size: ~8MB.

### URL Handling
`demo.sh` maintains `OPENAI_BASE_URL` (host) and `OPENAI_BASE_URL_CONTAINER` (localhost→host.docker.internal rewrite). Coverage uses the host URL. Containerized demos use the container URL.

### Podman Compatibility
- `:z` on all bind mounts (SELinux)
- `security_opt: label=disable` for socket access
- `DOCKER_SOCKET` derived from `DOCKER_HOST`
- `CUA_RUNTIME_SOCKET` exported for orchestrator
- `bash` entrypoint (not exec, avoids permission issues)

---

## 7. Testing

### Running tests:
```bash
pip install pytest
pytest tests/unit/ -v              # 78 tests, ~5s, no Docker
pytest tests/integration/ -m slow  # needs Docker + cua-agent image
./cualm selftest --full            # infrastructure → e2e
```

### Mock LLM Server (`tests/scripts/mock_llm.py`):
- Regex matching against prompt content
- Pre-built scripts: `NAVIGATE_AND_READ`, `FORM_FILL`, `CALIBRATION_ONLY`
- `host` parameter (default `127.0.0.1`, use `0.0.0.0` for rootless Podman e2e)
- Request logging, call counting, callable responses, multimodal support
- Context manager: `with MockLLMServer(script=[...]) as server:`

### Selftest levels:
1. **Infrastructure** — runtime responds, container run, network create
2. **Image** — cua-agent exists
3. **LLM** — reachable, completion works (auto-discovers model name)
4. **End-to-end** — agent runs with mock LLM (binds 0.0.0.0)

---

## 8. Completed Features

- Agent loop with screenshot → LLM → command cycle
- Ref-based element interaction (Playwright + CDP)
- Coordinate calibration with per-model caching
- Accessibility tree with smart truncation and ref badges
- Full-rewrite planning with cua-plan/cua-done gating
- Loop detection (3x repeat, 6-step alternation)
- CDP JavaScript execution (auto-fixes const/let)
- JS+CSS coverage daemon with cumulative tracking
- WebArena benchmark (parallel, resume, trace viewer, HAR capture)
- Coverage benchmark with target app
- Evaluator with normalization (unicode, whitespace, ™/®/©)
- Re-evaluation of existing results
- XMPP integration (Prosody + orchestrator + phone access)
- Demo system (webarena, coverage, wikipedia)
- GitHub Pages showcase generator (`demo/generate_site.py`)
- cualm CLI with preflight checks and self-test
- 78 unit tests + integration tests with mock LLM
- Documentation: README.md, CUALM.md, COURSE.md (19 modules)
- Logprobs/entropy tracking per step
- Status bar (HUD) Chrome extension
- Plugin system (audit, usage_tracking, xmpp)

---

## 9. Known Issues / Pending

### Active:
- Wikipedia screenshot capture gets calibration screen if VNC points to base `cua` container (fixed: `agent_capture.py` discovers task containers)
- `podman-compose` doesn't support `--project-directory` (workaround: `PROJECT_ROOT` env var in compose.yml)
- Coverage report shows 0% in final summary despite correct per-step tracking (display bug in `coverage.py` final report)

### Pending features:
- **Entropy-based features**: verification nudge on high entropy, model tier escalation, confidence-gated completion
- **Single executable**: Go/Rust CLI binary wrapping all shell scripts (Phase 1 discussed)
- **LLM server management**: auto-detect GPU, install Lemonade/LMStudio/ollama
- **Cross-platform**: macOS via Lima VM, Windows via WSL2
- `url_match` and `program_html` evaluators
- Wikipedia and Map site integration for WebArena

---

## 10. Quick Start for New Conversation

```bash
# Extract the tarball
tar xzf cualm-full.tar.gz

# Key files to modify:
#   agent.py           — prompt + loop
#   cdp_a11y.py        — accessibility tree + refs
#   tools/cua-*        — individual tools
#   benchmark.sh       — benchmark entry point
#   benchmark/run.py   — WebArena runner
#   benchmark/coverage.py — coverage orchestrator
#   demo.sh            — demo orchestration
#   cualm_selftest.py  — self-tests

# After changes:
./run.sh build        # rebuild Docker image
./cualm selftest      # verify environment
./cualm agent "test"  # quick test

# Run benchmarks:
./cualm benchmark --tasks 0
./cualm coverage --timeout 120

# Run tests:
pytest tests/unit/ -v
```
