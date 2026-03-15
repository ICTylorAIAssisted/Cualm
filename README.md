# CUA Agent — Shell-based Computer Use

A minimal computer-use agent where the LLM controls a Linux desktop by
emitting shell commands.  Each action is a standalone CLI tool that the
agent invokes through a single `run` interface.

## Architecture

```
 LLM                        Agent                     Shell
 ───                        ─────                     ─────
  ← screenshot ────────────── take screenshot
  → "I see a button.         ─── extract_command() ─→ run: cua-click 450 300
     run: cua-click 450 300" ─── run_command()     ─→ cua-click 450 300
                              ←── capture stdout,  ←  Calibrated. model(450,300) → ...
                                   stderr, timing
  ← formatted output ──────
  ← screenshot ──────────────
  → "Task complete.          ─── run_command()     ─→ cua-done "Finished" --result "42"
     run: cua-done ..."      ←── detects marker    ←  @@CUA_TASK_COMPLETE@@
                                                       {"summary":"Finished","result":"42"}
```

The model writes natural prose and ends with a single `run: <command>`
line.  The agent extracts that line, executes it in a shell, and feeds
the output back.  No JSON wrapper — just a prefix convention.

## Project Structure

```
agent.py             Main loop — screenshot → LLM → command
cua_config.py        Shared configuration and calibration
plugin_host.py       Plugin loader and hook dispatcher
tool_discovery.py    Auto-discovers cua-* tools for the system prompt
config.ini           Default settings (screen, mouse, LLM, etc.)
start.sh             Container entrypoint (Xvfb, Chromium, VNC)
run.sh               Build / run / stop helper (Compose + extensions)
Dockerfile           Container image definition
docker-compose.yml   Base compose file (agent only)
tools/               CLI tools the model invokes
plugins/
  audit.py           Screenshot + metadata logging
  usage_tracking.py  Token counting and throughput stats
  xmpp/              XMPP messaging (directory plugin)
  README.md          Plugin API reference
compose.d/
  audit/              Audit volume mount (pairs with plugins/audit.py)
  xmpp/              Prosody XMPP server (compose extension)
  orchestrator/      XMPP-driven task spawner (per-task containers)
  README.md          Compose extension reference
calibration/
  index.html         Calibration target page
example/
  index.html + .js   Interactive test page for agent validation
```

## CLI Tools

| Tool             | Purpose                                    |
|------------------|--------------------------------------------|
| `cua-click`      | Click / double-click / right-click         |
| `cua-type`       | Type text via simulated keypresses          |
| `cua-key`        | Press key combinations                      |
| `cua-scroll`     | Scroll at a position                        |
| `cua-drag`       | Drag between two points                     |
| `cua-wait`       | Sleep for a duration                        |
| `cua-screenshot` | Take screenshot (base64 to stdout)          |
| `cua-done`       | Signal task completion                      |

Every tool uses [Typer](https://typer.tiangolo.com/) and has `--help`
with full usage docs, reads defaults from `config.ini`, validates
inputs, and gives actionable error messages.

The system prompt is built dynamically at startup — the agent discovers
all `cua-*` executables on `PATH`, runs `--help` on each, and includes
their descriptions.  Drop a new tool in the `tools/` directory and it
appears in the prompt automatically.

## Calibration

Coordinate tools need a calibration that maps model pixel space to
actual screen pixels.  This is handled automatically:

1. On startup, Chromium loads a calibration page with a single large
   button centered on screen
2. The agent shows the model a screenshot and asks it to click the
   button — the model is *not* told the screen resolution, so it must
   rely on its visual perception to locate the target
3. The model emits `cua-click X Y` with the coordinates where it
   sees the button
4. The `cua-click` tool detects no calibration exists, computes scale
   factors from the model's coordinates vs. actual screen center, saves
   them to `/tmp/cua_calibration.json`, and performs the physical click
5. The button's click handler navigates to `about:blank`, clearing the
   calibration page before the main task loop begins
6. All subsequent coordinate tools (`cua-click`, `cua-scroll`, `cua-drag`)
   read the calibration and map coordinates transparently

You can also override calibration via environment variables:
```bash
export CUA_SCALE_X=1.0
export CUA_SCALE_Y=1.0
```

Tools that need coordinates but find no calibration will refuse with
an instructive error message.

## Configuration

All defaults live in `config.ini` (INI format).  Searched in order:

1. `$CUA_CONFIG` (explicit path)
2. `./config.ini` (working directory)
3. `~/.config/cua/config.ini`
4. `/etc/cua/config.ini`

Built-in defaults are used as fallback.

**Note:** When running in Docker/Podman, the `start.sh` entrypoint
automatically syncs `config.ini` screen values from the `SCREEN_WIDTH`,
`SCREEN_HEIGHT`, and `DISPLAY` environment variables so there is a
single source of truth.

## LLM Output Format

The model responds with free-form text and includes exactly one line
starting with `run: ` to indicate the command to execute:

```
I need to click the search box which is near the top of the page.
run: cua-click 450 120
```

The agent scans for the first line matching `run: <command>` (case-insensitive)
and ignores everything else.  This lets the model "think out loud" naturally
without needing structured JSON.

## Run Output Format

Every shell command returns:

```
<stdout>
stderr: <stderr>
[exit:<code>; <seconds>s]
```

When output exceeds `max_output_bytes` (default 5000):

```
Truncated stdout. Showing only first 5000 bytes. Full output stored in /tmp/cua_runs/abc123.out
<first 5000 bytes>
[exit:0; 1.23s]
```

## Quick Start (Docker / Podman)

The agent runs via Docker or Podman Compose.  A helper script detects
which is available and handles everything:

```bash
# Build and start
./run.sh up

# Run the agent with a task
./run.sh agent "Open Chromium and go to wikipedia.org"

# Watch the desktop via VNC
open vnc://localhost:5900    # macOS
vncviewer localhost:5900     # Linux

# Stop everything
./run.sh down
```

Any compose extensions in `compose.d/` are auto-discovered and merged.
For example, the bundled XMPP extension adds a Prosody server
automatically.  See `compose.d/README.md` for details.

### Environment Variables

Pass LLM connection details via environment variables.  You can either
export them before running or create an `.env` file (see below):

| Variable           | Default                         | Purpose                              |
|--------------------|---------------------------------|--------------------------------------|
| `CUA_CONFIG`       | *(none)*                        | Path to config.ini override          |
| `OPENAI_BASE_URL`  | `http://localhost:8000/v1`      | LLM endpoint                         |
| `OPENAI_API_KEY`   | `not-needed`                    | API key                              |
| `CUA_MODEL`        | `your-model-name`               | Model name                           |
| `CUA_AUDIT_DIR`    | `./audit`                       | Host path for audit data             |
| `SCREEN_WIDTH`     | `1280`                          | Virtual screen width                 |
| `SCREEN_HEIGHT`    | `800`                           | Virtual screen height                |
| `SCREEN_DEPTH`     | `24`                            | Virtual screen color depth           |
| `DISPLAY`          | `:99`                           | X11 display number                   |
| `CUA_SCALE_X`      | *(auto)*                        | Override calibration X scale factor  |
| `CUA_SCALE_Y`      | *(auto)*                        | Override calibration Y scale factor  |

Example `.env` file:

```bash
OPENAI_BASE_URL=http://host.docker.internal:8000/v1
OPENAI_API_KEY=sk-my-key
CUA_MODEL=qwen2.5-vl
```

Then run:

```bash
./run.sh up --env-file .env
```

### Accessing a Local Server from Inside the Container

The base compose file maps `host.docker.internal` automatically, so
the agent can reach services on your host machine:

```bash
# Serve the example page on your host
cd example && python3 -m http.server 8080 &

# Point the agent at it
./run.sh agent "Open http://host.docker.internal:8080 in Chromium"
```

Set `OPENAI_BASE_URL` to a host-local LLM the same way:

```bash
OPENAI_BASE_URL=http://host.docker.internal:1234/v1
```

Docker Desktop (macOS/Windows) resolves `host.docker.internal`
natively.  On Linux, the compose file adds `host-gateway` mapping.
For Podman, `host.containers.internal` works natively.

## Plugins

The agent has a lightweight plugin system.  Plugins are plain Python
files dropped into the `plugins/` directory.  Each file can define
hook functions (`on_startup`, `on_post_llm_call`, `on_shutdown`, etc.)
that the agent calls at natural points in the loop.

Two bundled plugins ship with the agent:

- **`plugins/audit.py`** — Saves screenshots and session
  metadata to `/app/audit`.  Delete to disable.
- **`plugins/usage_tracking.py`** — Tracks token usage and
  prints per-step and session-level stats.  Delete to silence.

An XMPP messaging plugin is also included:

- **`plugins/xmpp/`** — Enables the agent to exchange messages with
  a human user via XMPP.  See `plugins/xmpp/README.md`.  The
  matching compose extension (`compose.d/xmpp/`) provides the
  Prosody server — `./run.sh up` starts both automatically.

### Using external plugins

Place plugin files directly in `plugins/`, or mount a directory via
a compose override:

```yaml
# compose.d/my-plugins/compose.yml
services:
  cua:
    volumes:
      - ./my-plugins:/app/plugins/custom
```

### Writing a plugin

A plugin is a single `.py` file that defines one or more `on_*`
functions.  Every hook receives a shared `ctx` dict as its first
argument:

```python
# plugins/my_webhook.py

import os, requests

def on_startup(ctx):
    ctx["webhook_url"] = os.environ.get("CUA_WEBHOOK")

def on_task_complete(ctx, *, summary, result):
    requests.post(ctx["webhook_url"], json={
        "summary": summary,
        "result": result,
    })
```

Plugins can also replace core agent functions (LLM backend, screenshot
method, command runner, command parser) by setting the corresponding
key in `ctx` during `on_startup`.  See `plugins/README.md` for the
full hook reference, `ctx` fields, and swappable function signatures.

## Example Test Page

The `example/` directory contains a self-contained HTML/JS test target
with interactive widgets (counter, task manager, form validation,
calculator, drag & drop, etc.).  It is useful for verifying the agent
can interact with realistic UI elements.

Serve it from your host and point the agent at it:

```bash
cd example && python3 -m http.server 8080
# then inside the container:
# run: chromium http://host.docker.internal:8080
```
