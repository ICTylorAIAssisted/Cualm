# CUA Agent — Shell-based Computer Use

A minimal computer-use agent where the LLM controls a Linux desktop by
emitting shell commands.  Each action is a standalone CLI tool that the
agent invokes through a single `run` interface.

## Architecture

```
 LLM                        Agent                     Shell
 ───                        ─────                     ─────
  ← screenshot ────────────── take screenshot
  → "I'll click center       ─── extract_command() ─→ run: click 640 400
     run: click 640 400"     ─── run_command()     ─→ click 640 400
                              ←── capture stdout,  ←  Calibrated. model(640,400) → ...
                                   stderr, timing
  ← formatted output ──────
  ← screenshot ──────────────
  → "Task complete.          ─── run_command()     ─→ done "Finished" --result "42"
     run: done ..."          ←── detects marker    ←  @@CUA_TASK_COMPLETE@@
                                                       {"summary":"Finished","result":"42"}
```

The model writes natural prose and ends with a single `run: <command>`
line.  The agent extracts that line, executes it in a shell, and feeds
the output back.  No JSON wrapper — just a prefix convention.

## CLI Tools

| Tool         | Purpose                                    |
|--------------|--------------------------------------------|
| `click`      | Click / double-click / right-click         |
| `type`       | Type text via simulated keypresses          |
| `key`        | Press key combinations                      |
| `scroll`     | Scroll at a position                        |
| `drag`       | Drag between two points                     |
| `wait`       | Sleep for a duration                        |
| `screenshot` | Take screenshot (base64 to stdout)          |
| `done`       | Signal task completion                      |

Every tool uses [Typer](https://typer.tiangolo.com/) and has `--help`
with full usage docs, reads defaults from `config.ini`, validates
inputs, and gives actionable error messages.

## Calibration

Coordinate tools need a calibration that maps model pixel space to
actual screen pixels.  This is handled automatically:

1. The agent asks the model to "click the exact center of the screen"
2. The model emits `click X Y` with its best guess
3. The `click` tool detects no calibration exists, computes scale
   factors from the model's guess vs. actual screen center, and
   saves them to `/tmp/cua_calibration.json`
4. All subsequent coordinate tools (`click`, `scroll`, `drag`)
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
run: click 640 120
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

The recommended way to run the agent is with Docker or Podman.  A
helper script is provided that works with both:

```bash
# Build the image
./run.sh build

# Start an interactive container (VNC on port 5900)
./run.sh start

# Run the agent with a task directly
./run.sh agent "Open Chromium and go to wikipedia.org"

# Stop the container
./run.sh stop
```

### Environment Variables

Pass LLM connection details via environment variables.  You can either
export them before running or create an `.env` file (see below):

| Variable           | Default                         | Purpose                              |
|--------------------|---------------------------------|--------------------------------------|
| `CUA_CONFIG`       | *(none)*                        | Path to config.ini override          |
| `OPENAI_BASE_URL`  | `http://localhost:8000/v1`      | LLM endpoint                         |
| `OPENAI_API_KEY`   | `not-needed`                    | API key                              |
| `CUA_MODEL`        | `your-model-name`               | Model name                           |
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
./run.sh start --env-file .env
```

### Accessing a Local Server from Inside the Container

If you serve the example test page (or any other site) from your host
machine, the container needs to reach `localhost` on the host.  Both
Docker and Podman support `host.docker.internal` for this, but the
setup differs slightly:

**Docker (Linux):**
```bash
# Serve the example page on port 8080
cd example && python3 -m http.server 8080 &

# Start the container with host access
docker run -d --name cua \
    --add-host=host.docker.internal:host-gateway \
    -p 5900:5900 \
    -e OPENAI_BASE_URL=http://host.docker.internal:1234/v1 \
    cua-agent

# Then tell the agent to open the page
docker exec cua /app/venv/bin/python3 /app/agent.py \
    "Open http://host.docker.internal:8080 in Chromium"
```

Docker Desktop (macOS / Windows) resolves `host.docker.internal`
automatically — you do not need `--add-host`.

**Podman:**
```bash
podman run -d --name cua \
    --network slirp4netns:allow_host_loopback=true \
    -p 5900:5900 \
    -e OPENAI_BASE_URL=http://host.containers.internal:1234/v1 \
    cua-agent
```

Podman uses `host.containers.internal` by default (available since
Podman 4.5).  If you prefer the Docker-compatible alias you can add
`--add-host=host.docker.internal:host-gateway`.

The `run.sh` helper script handles these differences automatically —
see `./run.sh --help` for details.

### VNC Debugging

The container exposes a VNC server on port `5900`.  Connect with any
VNC client to watch the agent interact with the desktop in real time:

```bash
# macOS
open vnc://localhost:5900

# Linux (e.g. with tigervnc)
vncviewer localhost:5900
```

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
