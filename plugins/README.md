# CUA Plugins

Drop Python files into this directory (or any subdirectory) to extend
the agent.  Each `.py` file is loaded automatically at startup.

## How it works

A plugin is a plain Python file that defines one or more hook
functions.  Hook names start with `on_`.  The agent calls each hook at
the appropriate moment, passing a shared `ctx` dict plus hook-specific
keyword arguments.  A plugin only needs to define the hooks it cares
about — everything else is ignored.

```python
# plugins/my_webhook.py

import requests

def on_startup(ctx):
    ctx["webhook_url"] = os.environ.get("CUA_WEBHOOK")

def on_post_command(ctx, *, command, output):
    requests.post(ctx["webhook_url"], json={
        "step": ctx["step"],
        "command": command,
        "output": output[:500],
    })
```

## Hooks

All hooks receive `ctx` as the first positional argument.  Additional
arguments are keyword-only.

| Hook                | When                         | Extra kwargs                                     |
|---------------------|------------------------------|--------------------------------------------------|
| `on_startup`        | After config, before loop    | —                                                |
| `on_pre_screenshot` | Before taking screenshot     | —                                                |
| `on_post_screenshot`| After screenshot captured    | `img_b64: str`                                   |
| `on_pre_llm_call`   | Before sending to LLM       | `messages: list`                                 |
| `on_post_llm_call`  | After LLM response           | `messages: list, reply: str, usage: dict`        |
| `on_pre_command`    | Before shell execution       | `command: str`                                   |
| `on_post_command`   | After shell execution        | `command: str, output: str`                      |
| `on_task_complete`  | When cua-done is detected    | `summary: str, result: str or None`              |
| `on_shutdown`       | Session ending               | —                                                |

## The `ctx` dict

The context dict is the shared state for the entire session.  Core
fields set by the agent:

| Key              | Type     | Description                          |
|------------------|----------|--------------------------------------|
| `cfg`            | ConfigParser | Parsed config.ini                |
| `task`           | str      | The user's task string               |
| `model`          | str      | LLM model name                       |
| `step`           | int      | Current step number (updated in loop)|
| `screen_w`       | int      | Screen width in pixels               |
| `screen_h`       | int      | Screen height in pixels              |
| `extra_tools`    | list[str]| Extra tool descriptions for prompt   |
| `extra_prompt`   | list[str]| Extra sections appended to prompt    |
| `outcome`        | str      | "completed" or "max_steps" (at end)  |
| `wall_time_sec`  | float    | Total elapsed time (at end)          |
| `result`         | str/None | Value from cua-done --result         |

Plugins are free to read any field and to add their own keys (use a
descriptive prefix to avoid collisions, e.g. `webhook_url`,
`audit_session_dir`).

## Swappable functions

These core functions can be replaced by setting the corresponding key
in `ctx` during `on_startup`:

| ctx key            | Signature                            | Default behavior          |
|--------------------|--------------------------------------|---------------------------|
| `llm_call`         | `(messages: list) -> str`            | OpenAI-compatible API     |
| `screenshot_fn`    | `() -> str`                          | Calls cua-screenshot      |
| `command_runner`   | `(command: str) -> str`              | shell=True subprocess     |
| `extract_command`  | `(text: str) -> str`                 | Scans for `run:` prefix   |

Example — swap the LLM backend:

```python
# plugins/anthropic_backend.py
import anthropic

def on_startup(ctx):
    client = anthropic.Anthropic()
    def call(messages):
        resp = client.messages.create(
            model=ctx["model"],
            max_tokens=4096,
            messages=messages,
        )
        return resp.content[0].text
    ctx["llm_call"] = call
```

## Plugin tools

Plugins can ship new CLI tools.  Place executables in a directory and
mount it so it's on `PATH`.  Tools named `cua-*` are auto-discovered:
the agent runs `--help` on each at startup and includes a summary in
the system prompt.

For tools that don't follow the `cua-*` naming convention, register
them via `ctx["extra_tools"]` in `on_startup`:

```python
def on_startup(ctx):
    ctx["extra_tools"].append(
        '  mcp-query SERVER TOOL [ARGS...]   Call a remote MCP tool'
    )
```

## Bundled plugins

The `bundled/` subdirectory ships with the agent:

- **audit.py** — Saves screenshots and session metadata to disk.
  Delete to disable audit logging.
- **usage_tracking.py** — Tracks token usage and prints per-step and
  session-level stats.  Delete to silence token logging.

## Directory structure

```
plugins/
  README.md           ← this file
  bundled/
    audit.py          ← ships with agent
    usage_tracking.py ← ships with agent
  my_webhook.py       ← your plugin (gitignored)
  my_mcp_bridge.py    ← your plugin (gitignored)
```

Or mount external plugins at runtime:

```bash
docker run -v ./my-plugins:/app/plugins/custom ...
```
