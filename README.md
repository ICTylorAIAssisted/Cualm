# Cualm

**Computer Use Agent, Little Manager**

A model-agnostic agent that operates a Linux desktop through a screenshot → LLM → command loop. It runs inside a Docker container with Xvfb, Chromium, and xdotool, and works with any OpenAI-compatible API — local models (Qwen, Llama), Claude, GPT-4V, or anything with a `/v1/chat/completions` endpoint.

Cualm comes with two benchmarks:

- **WebArena** — 812 real web tasks across 5 self-hosted sites (Magento, Reddit, GitLab, Wikipedia, maps)
- **Coverage Benchmark** — explore a website to maximize JS+CSS code coverage

```
┌─────────────────────────────────────┐
│          Agent Container            │
│                                     │
│  Screenshot ──→ LLM ──→ Command    │
│      ↑                    │         │
│      └────────────────────┘         │
│                                     │
│  Xvfb  Chromium  xdotool  tools/   │
└─────────────────────────────────────┘
```

---

## Quick Start

```bash
# 1. Build the agent image
./run.sh build

# 2. Run a single task interactively (opens VNC on :5900)
OPENAI_BASE_URL=http://localhost:8000/v1 \
CUA_MODEL=your-model \
  docker run -it --rm -p 5900:5900 \
    -e OPENAI_BASE_URL -e CUA_MODEL \
    cua-agent agent "Search for 'wireless mouse' on the shopping site"

# 3. Connect to VNC to watch
open vnc://localhost:5900    # macOS
# or use any VNC viewer on port 5900
```

## WebArena Benchmark

```bash
# Start WebArena sites and run tasks
./benchmark.sh --site shopping --tasks 0-20

# Run with WA-Verified evaluation
./benchmark.sh --webarena-verified --tasks 0-50

# Human mode — opens VNC, shows task, no agent
./benchmark.sh --tasks 0 --human

# Stop and clean up
./benchmark.sh down
```

WebArena sites run in isolated Docker networks. An nginx gateway routes traffic so the agent sees `localhost:PORT` URLs matching WebArena's hardcoded addresses, while the LLM API is proxied through to the host.

## Coverage Benchmark

Measures how much of a website's JavaScript and CSS the agent can exercise through exploration.

```bash
# Run against the bundled test app
./benchmark.sh --coverage

# With options
./benchmark.sh --coverage --max-steps 30 --target 80

# Custom URL
./benchmark.sh --coverage --url http://myapp:3000

# Force re-calibration
./benchmark.sh --coverage --recalibrate

# Verify coverage tracking is working correctly
./benchmark.sh --coverage-test
```

The agent automatically receives coverage snapshots every step, injected into its prompt so it knows which areas it has and hasn't explored.

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_BASE_URL` | `http://localhost:8000/v1` | LLM endpoint |
| `OPENAI_API_KEY` | `not-needed` | API key |
| `CUA_MODEL` | `your-model-name` | Model identifier |
| `CUA_MAX_STEPS` | `50` | Max agent steps per task |
| `CUA_TEMPERATURE` | `0.7` | LLM temperature |
| `CUA_TOP_P` | `0.95` | LLM top_p |
| `CUA_PRESENCE_PENALTY` | `1.5` | Presence penalty |
| `CUA_LLM_EXTRA_PARAMS` | `{}` | Extra JSON for `extra_body` (e.g. `{"top_k":20}`) |
| `CUA_START_URL` | *(calibration page)* | URL for Chromium to open |
| `CUA_HISTORY_PAIRS` | `0` | Old message pairs to keep in context |
| `CUA_VNC_PORT` | `5900` | Host port for VNC |

All LLM parameters fall back to `config.ini` if the env var is unset or empty.

### config.ini

Screen resolution, LLM defaults, delays, and calibration settings. The Docker image syncs `SCREEN_WIDTH` / `SCREEN_HEIGHT` env vars into this file at startup.

---

## How It Works

### Agent Loop

Each step:

1. **Screenshot** — captured via `scrot`
2. **Accessibility tree** — fetched via CDP (`Accessibility.getFullAXTree`), truncated to 150 lines
3. **Build prompt** — task + previous observation summary + last command/output + plan + a11y tree + screenshot
4. **Call LLM** — any OpenAI-compatible API
5. **Extract command** — parses `run: <command>` from response
6. **Execute** — runs in shell, captures output
7. **Summary** — extracts text from `<think>` block for next step's context (replaces image history)

### Calibration

Vision models interpret pixel coordinates differently depending on training. On first run, Cualm shows a calibration page with clickable dots. The model clicks them, and the offset/scale is computed and cached per model in a Docker volume. Subsequent runs skip calibration.

### Planning

The model manages a full-rewrite plan via `cua-plan`:

```bash
cua-plan "Login | Navigate | Filter | Read data | Report"
cua-plan "[DONE] Login | [DONE] Navigate | Filter | Read data | Report"
cua-plan "[DONE] Login | [DONE] Navigate | [FAIL] Filter | Use JS instead | Report"
```

`cua-done` refuses to complete if the plan has pending steps (unless `--force`).

### Loop Detection

The agent detects when it's stuck — 3 identical actions in a row or 6-step click-wait alternation triggers a warning injection suggesting alternative approaches (JS extraction, URL navigation, Ctrl+F search).

### Tools

| Tool | Description |
|---|---|
| `cua-click` | Click at coordinates (with calibration mapping) |
| `cua-type` | Type text via xdotool |
| `cua-key` | Key combos (`ctrl+a`, `Return`, `F12`, etc.) |
| `cua-scroll` | Scroll with mousewheel |
| `cua-drag` | Drag from A to B |
| `cua-wait` | Wait (default 500ms, max 3s) |
| `cua-screenshot` | Manual screenshot capture |
| `cua-plan` | Full-rewrite planning |
| `cua-done` | Signal task completion |
| `cua-cdp-js` | Execute JavaScript in the browser via CDP |
| `cua-cdp-coverage` | Manage JS+CSS coverage recording via CDP |
| `cua-help` | Show tool help |

Tools are auto-discovered from their `--help` output and injected into the system prompt.

### Status Bar (HUD)

A Chrome extension displays a thin bar at the bottom of every page:

```
scroll: 45% ↓1200px left │ page: 3400px (4.2 screens) │ dom: changed 2s ago │ focus: input#email │ url: /settings/profile
```

The agent uses this to check scroll position, detect page changes after actions, and verify which element has focus before typing.

---

## Project Structure

```
├── agent.py                 # Main agent loop + prompt template
├── cdp_a11y.py              # Accessibility tree via CDP
├── config.ini               # Screen size, LLM params, delays
├── start.sh                 # Xvfb + Fluxbox + VNC + Chromium launcher
├── Dockerfile               # Debian bookworm, Chromium, Python
├── requirements.txt         # openai, Pillow, typer, websocket-client
├── benchmark.sh             # Entry point for all benchmark modes
├── tools/
│   ├── cua-click            # Click (with calibration)
│   ├── cua-type             # Type text
│   ├── cua-key              # Key combos
│   ├── cua-scroll           # Scroll
│   ├── cua-drag             # Drag
│   ├── cua-wait             # Wait
│   ├── cua-screenshot       # Screenshot
│   ├── cua-plan             # Planning
│   ├── cua-done             # Task completion
│   ├── cua-cdp-js           # JS execution via CDP
│   └── cua-cdp-coverage     # Coverage daemon via CDP
├── extensions/
│   └── cua-hud/             # Chrome extension (status bar)
├── plugins/
│   └── audit.py             # Step-by-step audit logging
├── benchmark/
│   ├── run.py               # WebArena benchmark runner
│   ├── config.py            # URL templates, credentials, defaults
│   ├── gateway.py           # Nginx gateway for network isolation
│   ├── evaluate.py          # Task evaluation (exact/fuzzy/url match)
│   ├── verified.py          # WA-Verified integration
│   ├── har.py               # mitmproxy addon for HAR capture
│   ├── coverage.py          # Coverage benchmark orchestrator
│   ├── coverage-target/     # Bundled test app for coverage mode
│   └── coverage-test/       # Coverage verification tests
└── compose.d/
    ├── .webarena/            # WebArena site containers
    └── .coverage/            # Coverage target + gateway
```

---

## Network Architecture

Both benchmarks use isolated Docker networks so the agent container has no direct internet access — it can only reach the target sites and the LLM via an nginx gateway.

```
┌────────────────────────────────────────────────────┐
│         internal network (no internet)             │
│                                                    │
│  target sites ←──→ nginx gateway ←──→ agent        │
│                         │                          │
└─────────────────────────┼──────────────────────────┘
                          │
┌─────────────────────────┼──────────────────────────┐
│         bridge network                             │
│  nginx gateway → host.docker.internal:LLM_PORT     │
└────────────────────────────────────────────────────┘
```

---

## Coverage Tracking

The coverage daemon (`cua-cdp-coverage`) keeps a persistent CDP websocket connection so profiler state survives across commands. It handles several V8 quirks:

- **Counter resets**: `Profiler.takePreciseCoverage` zeroes all counters after each call, so each snapshot only reports code run since the last call. The daemon unions covered byte ranges cumulatively — coverage can only go up.
- **Nested ranges**: V8 returns nested ranges where inner ranges override outer counts. The daemon resolves coverage at each byte offset by finding the innermost enclosing range.
- **Script lifecycle**: Scripts that get garbage-collected disappear from CDP results. Cumulative tracking preserves their coverage.

Verify it works: `./benchmark.sh --coverage-test`

---

## Performance Notes

- Task 0 (20 steps): ~150K prompt + 3K completion tokens, ~374s, ~$0.04
- Full WebArena (812 tasks): ~$25-39 estimated, ~8h at 10 parallel
- 90% of time is input-bound (prefill), not generation
- Screenshot: ~299 tokens at 1280×720 (Qwen2.5-VL)
- Text summary replaces image history: ~80 tokens vs ~443 tokens → 49% reduction per step

---

## License

MIT
