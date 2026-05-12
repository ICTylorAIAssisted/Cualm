# cua-monkey

A standalone **coverage / monkey tester** for web applications. A computer-use agent drives a real Chromium browser against a target URL, fed live coverage snapshots through Chrome DevTools so it can steer toward un-exercised code. Single-purpose: maximize JS+CSS coverage. Stops on a configurable timeout, step cap, or target coverage percentage.

Runs as a Docker (or Podman) compose stack. The agent container ships with Xvfb, Chromium, xdotool, Playwright, ffmpeg, and all the agent tooling — your host needs only `docker` (or `podman`) and an OpenAI-compatible LLM endpoint to point at.

```
┌─────────────────────── coverage-net (internal) ───────────────────────┐
│                                                                       │
│   coverage-target  ◀──────  cua-agent  ──────▶  coverage-gateway      │
│   (built-in demo)           (Chromium +                │              │
│                              CDP coverage              │              │
│                              loop)                     ▼              │
│                                                  host.docker.internal │
│                                                       LLM endpoint    │
└───────────────────────────────────────────────────────────────────────┘
```

The agent lives on an internal Docker network and can only reach the target site and the LLM gateway — it has no other network access.

---

## Quick start

```bash
# 1. Configure: copy the example, point UPSTREAM_HOST/PORT at your LLM
cp .env.example .env
$EDITOR .env

# 2. Run against the bundled demo site, 5-minute budget
./coverage.sh --minutes 5

# 3. Or run against your own URL
./coverage.sh --url http://localhost:3000 --minutes 10 --target 80

# 4. Watch live (optional) — connect any VNC viewer to localhost:5900
```

Outputs land in `./out/` by default:

```
out/
├── audit/session_<ts>/   # per-step screenshots, a11y dumps, LLM I/O
├── terminal.log          # stdout of the run
└── steps.mp4             # only if --video was passed
```

---

## `coverage.sh` flags

| Flag | Default | Description |
|---|---|---|
| `--url URL` | bundled demo site | Target URL |
| `--minutes N` | 15 | Wall-clock timeout |
| `--max-steps N` | 50 | Hard cap on agent steps |
| `--target PCT` | — | Stop early when coverage reaches this % |
| `--snapshot-interval N` | 1 | Coverage snapshot every N steps |
| `--credentials USER:PASS` | — | Injected into the agent task for login flows |
| `--api-base URL` | from `.env` | OpenAI-compatible endpoint on the host |
| `--api-key KEY` | from `.env` | |
| `--model NAME` | from `.env` | |
| `--output DIR` | `./out` | Audit / log / video destination |
| `--video` | off | Render a `steps.mp4` timelapse at end of run |
| `--vnc-port PORT` | 5900 | Host port for live VNC viewing |
| `--no-vnc` | off | Don't bind VNC |
| `--recalibrate` | off | Wipe cached mouse calibration before run |
| `--keep` | off | Don't `docker compose down` on exit |
| `-h`, `--help` | | This summary |

Flags override anything set in `.env`. See [`.env.example`](.env.example) for every knob.

---

## Direct compose use

`coverage.sh` is a thin wrapper. You can drive the stack directly:

```bash
# Build the agent image once
docker compose build

# Run with .env supplying all knobs
docker compose up --abort-on-container-exit --exit-code-from cua-agent

# Tear down
docker compose down
```

---

## How it works

### Agent loop

Each step:

1. **Screenshot** captured via `scrot`.
2. **Accessibility tree** fetched from Chromium via CDP (`Accessibility.getFullAXTree`), truncated to ~150 lines, decorated with `[ref=eN]` tags on interactive elements.
3. **Coverage snapshot** every N steps via `cua-cdp-coverage report` — file-by-file JS/CSS coverage percentages injected into the agent's context.
4. **Prompt → LLM → command.** The agent picks one `cua-*` tool invocation per step.
5. **Execute** in shell; capture output for next step.

### Calibration

Vision models interpret screen coordinates differently. On first run, the agent works through a calibration page (clickable dots at known offsets), computes a per-model offset/scale, and caches it in the `cua-calibration` Docker volume. Subsequent runs skip this. `--recalibrate` wipes the cache.

### Coverage tracking

`cua-cdp-coverage` runs as a background daemon inside the container, keeping a persistent CDP websocket so V8 profiler state survives across snapshot/report calls. It handles several CDP quirks:

- **Counter resets** — `Profiler.takePreciseCoverage` zeroes counters after each call, so the daemon unions covered byte ranges cumulatively. Coverage only ever goes up.
- **Nested ranges** — V8 returns nested ranges where inner ranges override outer counts. Each byte's coverage is resolved by the innermost enclosing range.
- **Script lifecycle** — scripts that get garbage-collected disappear from CDP results; cumulative tracking preserves their coverage.

A self-test is available: `docker compose run --rm cua-agent python3 /app/benchmark/coverage-test/verify.py`.

### Loop detection

The agent watches its own action history. Three identical actions in a row, or six-step click-wait alternation, triggers a warning that nudges toward alternative strategies (JS extraction via `cua-cdp-js`, URL navigation, Ctrl+F search).

### Tools available to the agent

| Tool | Purpose |
|---|---|
| `cua-click` / `cua-drag` / `cua-key` / `cua-type` / `cua-scroll` | Coordinate-based input via xdotool (calibrated) |
| `cua-pw` | Element-based input via Playwright + CDP (preferred — uses `[ref=eN]` from the a11y tree) |
| `cua-wait` / `cua-screenshot` | Pacing + manual capture |
| `cua-cdp-js` | Run JavaScript in the page via CDP |
| `cua-cdp-coverage` | Snapshot / report / reset coverage |
| `cua-plan` / `cua-done` | Plan management + completion signal |
| `cua-help` | Self-describing tool index |

Tools are auto-discovered from their `--help` output and injected into the system prompt.

### HUD overlay

A Chrome extension (`extensions/cua-hud/`) renders a thin status bar at the bottom of every page:

```
scroll: 45% ↓1200px left │ page: 3400px (4.2 screens) │ dom: changed 2s ago │ focus: input#email │ url: /settings
```

Helpful both for the agent (it reads this from the a11y tree) and for humans watching via VNC.

---

## Project layout

```
.
├── compose.yml              # 3 services: coverage-target, coverage-gateway, cua-agent
├── coverage.sh              # host-side wrapper around `docker compose up`
├── coverage-entrypoint.sh   # in-container: boots Xvfb+Chromium, runs coverage.py
├── nginx.conf.template      # gateway template, envsubst-rendered at boot
├── Dockerfile               # cua-agent image
├── .env.example             # every configurable knob
│
├── agent.py                 # the agent: screenshot → LLM → command loop
├── cdp_a11y.py              # a11y tree extraction via CDP
├── cua_config.py            # config.ini + calibration loading
├── plugin_host.py           # plugin discovery
├── tool_discovery.py        # tool discovery + system-prompt injection
├── config.ini               # screen / LLM / mouse / scroll defaults
├── start.sh                 # Xvfb + Fluxbox + VNC + Chromium launcher
│
├── benchmark/
│   ├── coverage.py          # orchestrates the coverage loop
│   ├── coverage-target/     # built-in demo site (served on coverage-net)
│   └── coverage-test/       # CDP coverage tracking sanity checks
│
├── tools/                   # cua-* tools the agent shells out to
├── plugins/{audit,usage_tracking}.py
├── extensions/cua-hud/      # in-page HUD overlay
├── calibration/             # calibration target HTML
│
├── screenshots_to_video.py  # --video timelapse renderer
├── cualm_preflight.py       # standalone environment diagnostics
├── cualm_selftest.py        # 4-level smoke test (infra / image / LLM / e2e)
│
└── docs/coverage/           # example run output
```

---

## Configuration cheat sheet

Every knob is documented in [`.env.example`](.env.example). The most common:

| Variable | Purpose |
|---|---|
| `UPSTREAM_HOST`, `UPSTREAM_PORT`, `UPSTREAM_PATH` | Where the gateway forwards LLM traffic on your host |
| `OPENAI_API_KEY`, `CUA_MODEL` | LLM credentials and model name |
| `TARGET_URL` | Site under test (defaults to bundled demo) |
| `CUA_MAX_STEPS`, `COVERAGE_TIMEOUT`, `COVERAGE_TARGET_PCT` | Termination conditions |
| `OUTPUT_DIR` | Where audit + logs land on the host |
| `CUA_TEMPERATURE`, `CUA_TOP_P`, `CUA_PRESENCE_PENALTY`, `CUA_LLM_EXTRA_PARAMS` | Sampling overrides for the LLM |

LLM sampling params fall back to `config.ini` when unset.

---

## Diagnostics

If a run fails to start, the standalone diagnostics scripts in the repo root are useful:

```bash
python3 cualm_preflight.py     # check runtime, image, GPU, host LLM reachability
python3 cualm_selftest.py      # 4-level smoke test
```

---

## License

MIT
