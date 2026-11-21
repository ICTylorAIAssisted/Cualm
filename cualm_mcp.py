#!/usr/bin/env python3
"""cualm MCP server — runs inside the cualm container.

Exposes the sandboxed desktop (Xvfb + Chromium) to an external agent
(Claude Code, OpenCode, …) over MCP streamable HTTP.

Two layers:
  * Primitives — screenshot, accessibility snapshot with element refs,
    click/type/keys/scroll, element actions by ref, navigation, JS, shell.
    The calling agent's own model does the planning.
  * run_task — delegate a whole task to cualm's built-in agent loop,
    driven by the OpenAI-compatible model configured in the container
    (OPENAI_BASE_URL / CUA_MODEL).  Useful for cheap/local models.

Primitives use the screen's native pixel space (identity calibration):
coordinates you read off a screenshot are the coordinates you click.
"""

import asyncio
import json
import os
import subprocess
from typing import Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError

from cdp_a11y import get_a11y_text, navigate as cdp_navigate
from cdp_coverage import CdpError, CoverageRecorder
from cua_config import load_config

cfg = load_config()
SCREEN_W = cfg.getint("screen", "width")
SCREEN_H = cfg.getint("screen", "height")
MAX_TEXT = 20_000

mcp = FastMCP(
    "cualm",
    instructions=(
        "Sandboxed Linux desktop with Chromium. Typical loop: call `snapshot` "
        "(accessibility tree with [ref=eN] ids) and/or `screenshot` (ref badges "
        "drawn on interactive elements), act with `click_element`/`fill`/"
        "`select_option` using refs (most reliable) or `click`/`type_text`/"
        "`press_key` using screen pixels, then observe again. Refs are "
        "reassigned on every snapshot/screenshot — always use the latest. "
        "`run_task` hands a whole task to cualm's own agent loop instead. "
        f"Screen size: {SCREEN_W}x{SCREEN_H}."
    ),
    host=os.environ.get("CUA_MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("CUA_MCP_PORT", "8765")),
)

# Primitives map model coordinates 1:1 to screen pixels.
_PRIM_ENV = {**os.environ, "CUA_SCALE_X": "1", "CUA_SCALE_Y": "1"}

_task_running = False


class Busy(RuntimeError):
    pass


def _check_idle() -> None:
    if _task_running:
        raise Busy("run_task is in progress; wait for it to finish before "
                   "driving the desktop directly.")


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + f"\n… (truncated, {len(text)} chars total)"


async def _run(args: list[str] | str, *, input: str | None = None,
               timeout: float = 30, shell: bool = False,
               env: dict | None = None) -> str:
    """Run a command in a worker thread; return combined, formatted output."""
    def _do() -> str:
        try:
            p = subprocess.run(
                args, input=input, capture_output=True, text=True,
                timeout=timeout, shell=shell,
                executable="/bin/bash" if shell else None,
                env=env or _PRIM_ENV,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"timed out after {timeout:.0f}s")
        out = p.stdout.strip()
        err = p.stderr.strip()
        if p.returncode != 0 and not shell:
            # Tool failed: surface as an MCP error, last lines are the useful ones
            msg = "\n".join((err or out or "failed").splitlines()[-15:])
            raise ToolError(f"{msg}\n[exit:{p.returncode}]")
        if err:
            out += ("\n" if out else "") + "stderr: " + err
        if p.returncode != 0:
            out += f"\n[exit:{p.returncode}]"
        return _clip(out or "ok")
    return await asyncio.to_thread(_do)


# ── Observation ───────────────────────────────────────────


@mcp.tool()
async def screenshot(with_refs: bool = True) -> Image:
    """Capture the screen as PNG.

    with_refs: first refresh element refs and draw small red ref badges
    (e1, e2, …) on interactive elements, so you can read a ref off the
    image and pass it to click_element/fill/select_option.
    """
    _check_idle()
    if with_refs:
        await asyncio.to_thread(get_a11y_text)

    def _shot() -> bytes:
        p = subprocess.run(["cua-screenshot", "--raw"], capture_output=True,
                           timeout=15, env=_PRIM_ENV)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode(errors="replace").strip()
                               or "screenshot failed")
        return p.stdout
    return Image(data=await asyncio.to_thread(_shot), format="png")


@mcp.tool()
async def snapshot(max_lines: int = 250) -> str:
    """Accessibility tree of the current page as indented text.

    Interactive elements carry [ref=eN] ids usable with click_element,
    fill and select_option. Shows form values, selected options and
    off-screen content. Long pages are truncated; use eval_js for more.
    """
    _check_idle()
    text = await asyncio.to_thread(get_a11y_text, max_lines=max_lines)
    return text or "(no accessibility tree — is a page loaded?)"


# ── Browser-level actions ─────────────────────────────────


@mcp.tool()
async def navigate(url: str) -> str:
    """Open a URL in the browser tab and wait for it to load."""
    _check_idle()
    final = await asyncio.to_thread(cdp_navigate, url)
    return f"loaded {final}"


@mcp.tool()
async def click_element(target: str) -> str:
    """Click an element by ref (e.g. "e15") or Playwright selector
    ("text=Submit", "role=button[name=OK]", "#id"). Waits for the element,
    scrolls it into view and checks it is clickable."""
    _check_idle()
    return await _run(["cua-pw", "click", "--", target])


@mcp.tool()
async def fill(target: str, value: str) -> str:
    """Clear an input/textarea (ref or selector) and fill it with value.
    Reads the value back to confirm."""
    _check_idle()
    return await _run(["cua-pw", "fill", "--", target, value])


@mcp.tool()
async def select_option(target: str, option: str) -> str:
    """Select an option (by visible label) in a <select> (ref or selector)."""
    _check_idle()
    return await _run(["cua-pw", "select", "--", target, option])


@mcp.tool()
async def eval_js(expression: str, await_promise: bool = False) -> str:
    """Evaluate JavaScript in the page and return the result as text.
    Fast way to read data (tables, values, attributes). Avoid top-level
    const/let — the context persists between calls; use expressions or var."""
    _check_idle()
    args = ["cua-cdp-js", "-"]
    if await_promise:
        args.append("--await")
    return await _run(args, input=expression)


# ── Raw input (screen pixels) ─────────────────────────────


@mcp.tool()
async def click(x: int, y: int,
                button: Literal["left", "middle", "right"] = "left",
                double: bool = False) -> str:
    """Click at screen pixel (x, y), as read off the screenshot.
    Prefer click_element for page elements; use this for canvases,
    images, maps, or anything outside the browser page."""
    _check_idle()
    args = ["cua-click", str(x), str(y),
            "--button", {"left": "1", "middle": "2", "right": "3"}[button]]
    if double:
        args.append("--double")
    return await _run(args)


@mcp.tool()
async def type_text(text: str) -> str:
    """Type text into the focused element via simulated keypresses."""
    _check_idle()
    return await _run(["cua-type", "--stdin"], input=text, timeout=120)


@mcp.tool()
async def press_key(keys: str) -> str:
    """Press a key or chord, xdotool style: "Return", "Tab", "ctrl+a",
    "ctrl+shift+t", "End", "F5"."""
    _check_idle()
    return await _run(["cua-key", keys])


@mcp.tool()
async def scroll(x: int, y: int, direction: Literal["up", "down"] = "down",
                 clicks: int = 3) -> str:
    """Scroll the mouse wheel at screen pixel (x, y)."""
    _check_idle()
    return await _run(["cua-scroll", str(x), str(y),
                       "--direction", direction, "--clicks", str(clicks)])


@mcp.tool()
async def drag(x1: int, y1: int, x2: int, y2: int) -> str:
    """Drag with the left mouse button from (x1, y1) to (x2, y2)."""
    _check_idle()
    return await _run(["cua-drag", str(x1), str(y1), str(x2), str(y2)])


@mcp.tool()
async def shell(command: str, timeout: int = 60) -> str:
    """Run a bash command inside the sandbox container (not on the host).
    DISPLAY is set, so X11 tools (xdotool, etc.) work."""
    _check_idle()
    return await _run(command, shell=True, timeout=timeout)


# ── Code coverage ─────────────────────────────────────────

_coverage = CoverageRecorder()


@mcp.tool()
async def coverage_start(url: str | None = None) -> str:
    """Start recording JS + CSS code coverage of the page (V8 / CDP).

    Opens url (or keeps the current page), arms the profiler, then reloads
    so every script is instrumented from the first line. Any earlier
    recording is discarded. Then explore with the other tools and call
    coverage_report to see how much of the page's code has run.
    Only http(s)/file scripts and stylesheets are counted, so eval_js,
    snapshot and screenshot helpers don't inflate the numbers.
    """
    _check_idle()
    try:
        return await asyncio.to_thread(_coverage.start, url)
    except (CdpError, RuntimeError) as e:
        raise ToolError(str(e))


@mcp.tool()
async def coverage_report(top_files: int = 15, list_functions: int = 40,
                          stop: bool = False) -> str:
    """Report cumulative code coverage since coverage_start.

    Shows overall JS and CSS byte coverage, functions called / total,
    per-file percentages (least covered first) and the names of JS
    functions that never ran — a to-do list of unexplored UI paths.
    Coverage only grows between reports. stop=True also ends the recording.
    Works while run_task is running, to measure cualm's own agent.
    """
    def _do() -> str:
        _coverage.snapshot()
        text = _coverage.report(top_files, list_functions)
        if stop:
            _coverage.stop_session()
            text += "\n\nRecording stopped."
        return text
    try:
        return await asyncio.to_thread(_do)
    except CdpError as e:
        raise ToolError(str(e))


# ── Delegation ────────────────────────────────────────────


@mcp.tool()
async def run_task(task: str, start_url: str | None = None,
                   max_steps: int = 30, timeout_sec: int = 1800) -> str:
    """Delegate a whole GUI task to cualm's built-in agent loop.

    The loop is driven by the model configured in the container
    (OPENAI_BASE_URL / CUA_MODEL), not by you. Blocks until done and
    returns JSON: status, summary, result, steps, wall_time_sec, audit_dir.
    Write the task with a concrete goal and success criteria; ask for a
    specific value if you need one back.
    """
    global _task_running
    _check_idle()
    _task_running = True
    try:
        args = ["python3", "/app/agent.py", "--json",
                "--max-steps", str(max_steps)]
        if start_url:
            args += ["--start-url", start_url]
        args += ["--", task]

        def _do() -> dict:
            try:
                p = subprocess.run(args, capture_output=True, text=True,
                                   timeout=timeout_sec, env=dict(os.environ))
            except subprocess.TimeoutExpired:
                return {"status": "timeout", "timeout_sec": timeout_sec}
            lines = p.stdout.strip().splitlines()
            try:
                res = json.loads(lines[-1]) if lines else {}
            except json.JSONDecodeError:
                res = {}
            if not res:
                res = {"status": "error",
                       "log_tail": p.stderr[-3000:]}
            return res
        return json.dumps(await asyncio.to_thread(_do), indent=2)
    finally:
        _task_running = False


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
