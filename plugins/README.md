# CUA Plugins

Drop Python files into this directory to extend the agent.  Plugins
come in two forms:

1. **Flat files** — `plugins/my_webhook.py` is loaded directly.
2. **Directory plugins** — `plugins/xmpp/plugin.py` is loaded as
   the entry point.  The directory is added to `sys.path` so the
   plugin can import its own subpackages.

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

These ship with the agent as top-level files:

- **audit.py** — Saves screenshots and session metadata to disk.
  Delete to disable audit logging.
- **usage_tracking.py** — Tracks token usage and prints per-step and
  session-level stats.  Delete to silence token logging.

The `xmpp/` directory plugin adds XMPP messaging:

- **xmpp/** — Enables the agent to send/receive messages via XMPP.
  Includes a background daemon and CLI tools (`cua-xmpp-send`,
  `cua-xmpp-recv`, `cua-xmpp-wait`, etc.).  See `xmpp/README.md`.

## Directory structure

```
plugins/
  README.md                ← this file
  audit.py                 ← bundled: screenshot + metadata logging
  usage_tracking.py        ← bundled: token counting
  xmpp/                    ← directory plugin
    plugin.py              ← entry point (loaded by plugin host)
    requirements.txt       ← plugin dependencies
    xmpp_tools/            ← shared config module
    tools/                 ← CLI tools (the actual implementations)
      cua-xmpp-send
      cua-xmpp-recv
      ...
  my_webhook.py            ← your flat-file plugin (gitignored)
```

### Directory plugin layout

A directory plugin must contain a `plugin.py` file — that's the only
requirement.  Everything else is up to you:

```
plugins/my_integration/
  plugin.py                ← required — defines on_startup, on_shutdown, etc.
  requirements.txt         ← optional — installed at build time
  tools/                   ← optional — CLI tools (added to PATH in on_startup)
  my_package/              ← optional — internal libraries
```

The plugin host adds the directory itself to `sys.path` before loading
`plugin.py`, so `from my_package import foo` works without any path
manipulation.
