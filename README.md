# cualm

**A sandboxed computer-use desktop for coding agents.**

cualm runs a disposable Linux desktop (Xvfb + Chromium) in a container and
exposes it to agents like Claude Code or OpenCode through an MCP server. The
agent can look at a real browser and operate it. Typical uses are verifying
a frontend change, reproducing a UI bug, or filling in a form, all without
touching your own desktop.

```
 coding agent (Claude Code, OpenCode, …)
        │  MCP (streamable HTTP, localhost:8765/mcp)
        ▼
┌───────────────────────── cualm container ─────────────────────────┐
│  cualm_mcp.py ── primitives ──► cua-* tools ──► xdotool / CDP      │
│        └──────── run_task ───► agent.py loop ──► any OpenAI-compat │
│  Xvfb · fluxbox · Chromium (CDP :9222) · VNC :5900                 │
└────────────────────────────────────────────────────────────────────┘
```

## Quick start

```bash
./cualm build          # podman or docker
./cualm up             # MCP on http://localhost:8765/mcp, VNC on :5900
./cualm mcp-config     # prints the setup snippets below
```

**Claude Code**

```bash
claude mcp add -s user --transport http cualm http://localhost:8765/mcp
cp -r skills/cualm ~/.claude/skills/        # optional, recommended
claude mcp list                             # cualm: ... ✔ Connected
```

`-s user` makes cualm available from every folder. Without it, Claude Code
registers the server only for the folder you ran the command in (`local`
scope), and `claude mcp list` won't show it anywhere else. With user scope,
the cualm tools appear in every session and fail while the sandbox is down,
so run `./cualm up` first. To limit it to one project, leave out `-s user`
and run the command from that project's folder.

**OpenCode** (`opencode.json`)

```json
{ "mcp": { "cualm": { "type": "remote", "url": "http://localhost:8765/mcp", "enabled": true } } }
```

The skill in `skills/cualm/SKILL.md` teaches the agent the observe → act
loop, the rules for using refs, and to keep screenshots in a subagent. The
tools work without it, but agents use them better with it. OpenCode reads
skills from its own skills directory; see its docs for the exact path.

To watch the agent work, open any VNC viewer on `localhost:5900`.

## MCP tools

| Tool | What it does |
|------|--------------|
| `snapshot` | Accessibility tree as text; interactive elements get `[ref=eN]` |
| `screenshot` | PNG of the screen, with red ref badges drawn on interactive elements |
| `navigate` | Open a URL (via CDP) and wait for load |
| `click_element` / `fill` / `select_option` | Act on an element by ref (`e15`) or Playwright selector |
| `eval_js` | Evaluate JavaScript in the page |
| `click` / `type_text` / `press_key` / `scroll` / `drag` | Raw input in screen pixels |
| `shell` | Run bash inside the container |
| `run_task` | Delegate a whole task to cualm's own agent loop (see below) |

Refs are renumbered on every `snapshot`/`screenshot`. Pixel coordinates are
native screen pixels (default 1280×720).

To reach a dev server on the host, use `http://host.docker.internal:<port>`
from inside the sandbox, with the server listening on `0.0.0.0`.

## Built-in agent loop (optional)

cualm also ships its own screenshot → LLM → shell-command loop (`agent.py`).
It works with any OpenAI-compatible endpoint, which makes it a good fit for
cheap or local models. Use it through the `run_task` MCP tool, or headless:

```bash
export OPENAI_BASE_URL=http://localhost:8000/v1 CUA_MODEL=your-model
./cualm run "Find the population of Iceland on Wikipedia" \
    --start-url https://en.wikipedia.org --json
# → {"status": "completed", "summary": "...", "result": "...", "steps": 7, ...}
```

The loop's design:

- The model emits `run: <command>` and composes the `cua-*` CLI tools in `tools/`.
- A text summary replaces image history, so each step sends only one image.
- The model writes and rewrites a plan (`cua-plan`), and repeated actions are detected.
- A calibration step per model maps the model's pixel space to the screen.
- The prompt includes the accessibility tree with refs and ref badges on the screenshot.
- Plugins (`plugins/`) hook into each step. The bundled `audit.py` saves a step trace and screenshots, and `usage_tracking.py` counts tokens.

Settings live in `config.ini`, with `CUA_*` environment overrides. You can
also put `OPENAI_BASE_URL`, `OPENAI_API_KEY` and `CUA_MODEL` in a `.env`
file next to `cualm`; `./cualm up` and `./cualm run` load it.

## Layout

```
cualm               host CLI: build / up / down / status / logs / mcp-config / run
cualm_mcp.py        MCP server (runs in the container)
agent.py            built-in agent loop (runs in the container)
cdp_a11y.py         accessibility tree, refs, ref badges, CDP navigation
tools/cua-*         action tools (click, type, key, scroll, pw, cdp-js, …)
skills/cualm/       agent skill (SKILL.md)
calibration/        calibration page used by the agent loop
extensions/cua-hud  Chromium extension drawing the status bar
plugins/            agent-loop plugins
start.sh            container entrypoint: agent | serve | <cmd>
```

## Security notes

- Ports are bound to `127.0.0.1` only. The MCP server has no
  authentication, and anything that can reach it can run commands in the
  container.
- The container is the sandbox: Chromium runs `--no-sandbox` inside it, and
  `shell` runs arbitrary commands in it. Don't mount host directories you
  don't want the agent to touch.
