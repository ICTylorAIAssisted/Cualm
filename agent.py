#!/usr/bin/env python3
"""CUA agent: screenshot → LLM → shell command loop.

The model sees a Linux desktop via screenshots and controls it by
emitting shell commands.  Each action is a standalone CLI tool (cua-click,
cua-type, cua-key, …) that the model invokes through a single "run"
interface.

Plugins extend behavior via hooks and swappable functions.  See
plugins/README.md for the full API.
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from cua_config import load_config, calibration_file_for_model, load_calibration
from cdp_a11y import get_a11y_text
from plugin_host import PluginHost
from tool_discovery import discover_tools

# ── Done marker (must match tools/cua-done) ──────────────
DONE_MARKER = "@@CUA_TASK_COMPLETE@@"

# ── Configuration ─────────────────────────────────────────
cfg = load_config()

SCREEN_W = cfg.getint("screen", "width")
SCREEN_H = cfg.getint("screen", "height")

MAX_STEPS = int(os.environ.get("CUA_MAX_STEPS", cfg.getint("agent", "max_steps")))
POST_ACTION_DELAY = cfg.getfloat("agent", "post_action_delay")

LLM_MAX_TOKENS = cfg.getint("llm", "max_tokens")
LLM_TEMPERATURE = float(os.environ.get("CUA_TEMPERATURE", "") or cfg.getfloat("llm", "temperature"))
LLM_TOP_P = float(os.environ.get("CUA_TOP_P", "") or cfg.getfloat("llm", "top_p"))
LLM_PRESENCE_PENALTY = float(os.environ.get("CUA_PRESENCE_PENALTY", "") or cfg.getfloat("llm", "presence_penalty"))
MAX_API_ERRORS = cfg.getint("llm", "max_api_errors")
MAX_PARSE_ERRORS = cfg.getint("llm", "max_parse_errors")
MAX_HISTORY_PAIRS = int(os.environ.get("CUA_HISTORY_PAIRS", "") or cfg.getint("llm", "max_history_pairs"))

# Extra kwargs passed to chat.completions.create() via extra_body.
# Use for vendor-specific params like top_k, min_p, repetition_penalty.
# Set as JSON, e.g. CUA_LLM_EXTRA_PARAMS='{"top_k":20,"min_p":0.0}'
_extra_params_raw = os.environ.get("CUA_LLM_EXTRA_PARAMS", "")
LLM_EXTRA_PARAMS: dict = {}
if _extra_params_raw:
    import json as _json
    LLM_EXTRA_PARAMS = _json.loads(_extra_params_raw)
PREAMBLE_SIZE = cfg.getint("llm", "preamble_size")

MAX_OUTPUT_BYTES = cfg.getint("run", "max_output_bytes")
RUN_OUTPUT_DIR = cfg.get("run", "output_dir")

MODEL = os.environ.get("CUA_MODEL", "your-model-name")

# Coverage mode — when enabled, agent starts CDP coverage recording after
# navigation and auto-snapshots every N steps, injecting results into prompt.
COVERAGE_ENABLED = os.environ.get("CUA_COVERAGE", "") == "1"
COVERAGE_INTERVAL = int(os.environ.get("CUA_COVERAGE_INTERVAL", "") or 1)

CALIBRATION_PROMPT = "You see a button on screen. Click it."

PLUGINS_DIR = os.environ.get(
    "CUA_PLUGINS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins"),
)


# ── System prompt builder ─────────────────────────────────


PROMPT_TEMPLATE = """\
You are a computer-use agent. You see screenshots of a Linux desktop.

You control the computer by running shell commands. Each turn, think
briefly about what to do, then write exactly one command on a line
starting with "run: ". Everything after "run: " is executed in a shell.

Be efficient — minimize the number of steps. Chain related actions in
a single command with && when you don't need a screenshot in between:
  run: cua-click 450 250 && cua-type "admin" && cua-key Tab && cua-type "pass" && cua-key Return
Each step costs time, so batch actions that logically belong together.

Planning:
  On your FIRST step, create a plan before doing anything else.
  Use | to separate steps. Rewrite the FULL plan each time to update:
    run: cua-plan "Login | Navigate to reports | Filter data | Read results | Report answer"
  After completing a step, rewrite with [DONE] markers:
    run: cua-plan "[DONE] Login | Navigate to reports | Filter data | Read results | Report answer"
  To restructure (add/remove/split steps), just rewrite the whole plan:
    run: cua-plan "[DONE] Login | [DONE] Navigate | Set date range | Choose grouping | Click generate | Read results | Report answer"
  If a step fails, mark it [FAIL] and add a new approach:
    run: cua-plan "[DONE] Login | [DONE] Navigate | [FAIL] Click generate | Use JS to extract data | Report answer"
  Do NOT mark a step [DONE] until its result is fully visible/confirmed.
  Your current plan is shown with each screenshot — use it to stay
  on track and avoid repeating failed approaches.

Available CLI tools (run any with --help for usage):

{tools}

You may also run arbitrary shell commands (ls, cat, curl, grep, etc.).
Coordinates are in your pixel space — calibration mapping is automatic.

Browser JS (cua-cdp-js) — fast data extraction:
  Use cua-cdp-js to run JavaScript directly in the page without opening
  DevTools. This is the fastest way to read page content, check form
  values, inspect dropdowns, and extract data from tables:
    run: cua-cdp-js "document.title"
    run: cua-cdp-js "document.querySelector('h1').innerText"
    run: cua-cdp-js "document.querySelector('select').value"
    run: cua-cdp-js "[...document.querySelectorAll('select option')].map(o => (o.selected ? '> ' : '  ') + o.text).join('\n')"
    run: cua-cdp-js "[...document.querySelectorAll('table tbody tr')].slice(0,5).map(r => r.innerText).join('\n')"
  Use it to check dropdown values and options BEFORE clicking blindly.
  Use it to read table data instead of scrolling through long pages.
  You can also SET values:
    run: cua-cdp-js "document.querySelector('select#sort').value = 'price'"
    run: cua-cdp-js "document.querySelector('input#email').value = 'test@example.com'"
  For complex selectors with nested quotes, use a heredoc:
    run: cua-cdp-js - << 'JS'
    document.querySelector('input[placeholder="Search by name"]').value = "test"
    JS

  IMPORTANT: Never use const/let in cua-cdp-js — the browser context
  persists, so re-running will throw "Identifier already declared".
  Write single expressions instead, or use var if you need a variable:
    WRONG:  cua-cdp-js "const rows = [...document.querySelectorAll('tr')]; rows.map(..."
    RIGHT:  cua-cdp-js "[...document.querySelectorAll('tr')].map(..."
    RIGHT:  cua-cdp-js "var rows = [...document.querySelectorAll('tr')]; rows.map(..."

  For visual debugging (CSS issues, layout), open DevTools with F12.
  Dock it to the bottom or right so you can see both page and console.

Status bar (bottom of screen):
  A thin bar at the bottom of every page shows:
    scroll: 45% ↓1200px left │ page: 3400px (4.2 screens) │ dom: changed 2s ago │ focus: input#email │ url: /settings/profile
  Use this to check: how far you've scrolled, whether scrolling is
  needed, which element has focus before typing, and current URL.
  The "dom:" field shows when the page last changed — if it says
  "changed just now" or "changed 2s ago" after clicking a button,
  the action worked. Scroll down to see the results instead of
  clicking again. If it says "idle" or "changed 15s ago", the click
  may not have hit the right target.

Accessibility tree (included with each screenshot):
  A text representation of the page structure is included alongside
  each screenshot. It shows elements with their roles, names, values,
  and states (focused, selected, expanded, etc.). Use it to:
  - See ALL dropdown/select options and which is selected — without
    clicking to open the dropdown
  - Find elements that are off-screen or hard to read in the screenshot
  - Identify the exact text content of table cells, headings, links
  - Check form field values and states
  The tree is truncated for long pages — use cua-cdp-js for deeper queries.

Page awareness:
- Read the status bar: if "scroll: no scroll needed", the page fits.
  If it shows e.g. "4.2 screens", prefer Console JS or Ctrl+F over
  repeated scrolling.
- The page extends beyond what you see. If you click a button or submit
  a form and nothing seems to change, scroll down — the result, error
  message, or new content may have appeared below the visible area.
- Use End key to jump to the bottom, Home to jump to the top.
- If you need to find something on a long page, use Ctrl+F to search
  rather than scrolling through it manually.

Reports and filters:
- When generating reports or searching with filters, check ALL filter
  options before submitting — not just the obvious ones like date range.
  Dropdowns for grouping, aggregation, sorting, or category are easy to
  miss but dramatically affect results. Wrong settings can produce
  hundreds of rows instead of a useful summary.

Form interaction — clear first, verify after:
  ALWAYS follow this sequence for each form field:
    1. Click the field (or check focus: in status bar)
    2. Clear it: Ctrl+A, Delete, then type the new value
    3. Verify with cua-cdp-js: check the field actually has the right value
  Example for a date field:
    run: cua-click 400 300 && cua-key ctrl+a && cua-key Delete && cua-type "03/15/2024"
    run: cua-cdp-js "document.activeElement.value"
  If the value doesn't match what you typed, the form transformed your
  input. Compare character by character to understand the format:
    Typed: "01-15-2023"  →  Field shows: "01152023"  →  Dashes stripped
    Typed: "admin"       →  Field shows: "adminadmin" → Field wasn't empty
  When you encounter a form with multiple fields, expand your plan to
  include filling AND verifying each field as separate sub-steps.
  Never submit a form without verifying all fields are correct first.
  IMPORTANT: Never repeat an approach that already failed — switch to
  a different format or use cua-cdp-js to set the value directly.
  Date fields vary widely: MM/DD/YYYY, YYYY-MM-DD, Jan 15 2023, etc.
  Check placeholder text, or use cua-cdp-js to read the field's type
  and attributes to determine the expected format.

After each action, check the screenshot to verify it had the expected
effect. If nothing seems to have changed, scroll down first — the
result may be below the viewport.

When the task is complete, use the cua-done tool with a summary.
When asked to find or report a value, pass it via --result.
When the task involves showing something visual, add --screenshot to
include a capture of the current screen in the result.
You will see the output of your previous command alongside each new
screenshot. Read it carefully — it tells you what actually happened
(e.g. "Scrolled up" when you meant to scroll down).

{extra}
Examples:
  run: cua-click 450 300
  run: cua-type "hello world"
  run: cua-key ctrl+a
  run: cua-scroll 500 400 --clicks 5              (scroll DOWN to see more)
  run: cua-scroll 500 400 --up --clicks 3          (scroll UP to go back)
  run: cua-click 450 300 && cua-wait               (click then wait 500ms for page load)
  run: cua-done "Opened Wikipedia" --result "https://en.wikipedia.org"
  run: cua-done "Here is the page" --screenshot
  run: cat /etc/os-release | head -5

Login example (click username, type, Tab to password, type, Enter):
  run: cua-click 450 250
  run: cua-type "myuser" && cua-key Tab && cua-type "mypass" && cua-key Return

Form filling example (clear field, type, verify):
  run: cua-click 400 300 && cua-key ctrl+a && cua-key Delete && cua-type "new value"
  run: cua-cdp-js "document.activeElement.value"

Data extraction example (read page content and table data via JS):
  run: cua-cdp-js "document.title + ' — ' + document.querySelector('h1')?.innerText"
  run: cua-cdp-js "[...document.querySelectorAll('table tbody tr')].slice(0,3).map(r => r.innerText).join('\\n')"
"""


def build_system_prompt(ctx: dict) -> str:
    """Assemble the system prompt from discovered tools and plugin extras."""
    tool_lines = discover_tools()
    tool_lines.extend(ctx.get("extra_tools", []))
    tools_section = "\n".join(tool_lines)

    extra_parts = ctx.get("extra_prompt", [])
    extra_section = "\n\n".join(extra_parts)
    if extra_section:
        extra_section = "\n\n" + extra_section + "\n"
    else:
        extra_section = "\n"

    return PROMPT_TEMPLATE.format(tools=tools_section, extra=extra_section)


# ── Default swappable functions ───────────────────────────
# These are used unless a plugin overrides them in ctx.

_openai_client = None


def _get_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(
            base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1"),
            api_key=os.environ.get("OPENAI_API_KEY", "not-needed"),
        )
    return _openai_client


def default_llm_call(ctx: dict, messages: list[ChatCompletionMessageParam]) -> str:
    """Call the LLM via OpenAI-compatible API."""
    client = _get_client()
    t0 = time.monotonic()
    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=LLM_MAX_TOKENS,
        messages=messages,
        temperature=LLM_TEMPERATURE,
        top_p=LLM_TOP_P,
        presence_penalty=LLM_PRESENCE_PENALTY,
        extra_body=LLM_EXTRA_PARAMS or None,
    )
    elapsed = time.monotonic() - t0

    content = response.choices[0].message.content
    if content is None:
        content = ""

    # Some backends (llama.cpp, vLLM, Lemonade) return thinking tokens
    # in a separate field instead of inline in content.  The OpenAI SDK
    # may not expose unknown fields via getattr, so also check model_extra.
    msg = response.choices[0].message
    reasoning = (
        getattr(msg, "reasoning_content", None)
        or getattr(msg, "reasoning", None)
    )
    if not reasoning:
        extra = getattr(msg, "model_extra", None) or {}
        reasoning = extra.get("reasoning_content") or extra.get("reasoning")

    # One-time debug: log available message fields on first call
    if not getattr(default_llm_call, "_fields_logged", False):
        default_llm_call._fields_logged = True
        known = [k for k in dir(msg) if not k.startswith("_")]
        extra_keys = list((getattr(msg, "model_extra", None) or {}).keys())
        print(f"   [debug] Message fields: {known}")
        if extra_keys:
            print(f"   [debug] Extra fields: {extra_keys}")
        print(f"   [debug] Reasoning captured: {bool(reasoning)}")

    if reasoning:
        content = f"<think>{reasoning}</think>\n{content}"

    if not content.strip():
        raise ValueError("LLM returned empty content")

    # Stash usage info for the hook
    ctx["_last_usage"] = {
        "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
        "completion_tokens": response.usage.completion_tokens if response.usage else 0,
        "total_tokens": response.usage.total_tokens if response.usage else 0,
        "elapsed_sec": elapsed,
    }
    return content


def default_screenshot(ctx: dict) -> str:
    """Take a screenshot via the cua-screenshot tool, return base64."""
    result = subprocess.run(
        ["cua-screenshot"], capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"screenshot failed: {result.stderr.strip()}")
    return result.stdout.strip()


def default_command_runner(ctx: dict, command: str) -> str:
    """Execute *command* in a shell, return formatted output."""
    call_uid = uuid.uuid4().hex[:12]

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        return f"Command timed out after 30s\n[exit:124; {elapsed:.2f}s]"
    elapsed = time.monotonic() - t0

    stdout_text = _truncate(proc.stdout, "stdout", call_uid, "out")
    stderr_text = _truncate(proc.stderr, "stderr", call_uid, "err")

    parts: list[str] = []
    if stdout_text:
        parts.append(stdout_text)
    if stderr_text:
        parts.append(f"stderr: {stderr_text}")
    parts.append(f"[exit:{proc.returncode}; {elapsed:.2f}s]")

    return "\n".join(parts)


def _truncate(data: bytes, label: str, call_uid: str, ext: str) -> str:
    if len(data) <= MAX_OUTPUT_BYTES:
        return data.decode(errors="replace")

    os.makedirs(RUN_OUTPUT_DIR, exist_ok=True)
    full_path = os.path.join(RUN_OUTPUT_DIR, f"{call_uid}.{ext}")
    with open(full_path, "wb") as f:
        f.write(data)

    truncated = data[:MAX_OUTPUT_BYTES].decode(errors="replace")
    notice = (
        f"Truncated {label}. "
        f"Showing only first {MAX_OUTPUT_BYTES} bytes. "
        f"Full output stored in {full_path}"
    )
    return f"{notice}\n{truncated}"


def default_extract_command(ctx: dict, text: str) -> str:
    """Extract the shell command from an LLM reply.

    Looks for a line starting with 'run:' (case-insensitive), after
    stripping <think> blocks, markdown fences, and surrounding prose.
    """
    text = text.strip()

    # Strip thinking wrapper tags from various models.
    # For <think>/[THINK]: discard the content (it's internal reasoning)
    # For <nothink>: keep the content (it's the actual response, Phi4)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"\[THINK\].*?\[/THINK\]", "", text, flags=re.DOTALL)
    # Handle unclosed thinking tags (model started but didn't close)
    if "</think>" in text:
        text = text.split("</think>")[-1]
    if "[/THINK]" in text:
        text = text.split("[/THINK]")[-1]
    # Strip all tag markers (opening/closing), keeping content between
    # nothink tags intact since it IS the response
    text = re.sub(r"</?(?:no)?think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?THINK\]", "", text)
    text = text.strip()

    # Strip markdown fences if the whole response is wrapped
    m = re.search(r"```(?:\w*)\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    # Scan lines for 'run:' prefix
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("run:"):
            command = stripped[4:].strip()
            if command:
                # Fix models that write "cua-type X && run: cua-click Y"
                # — strip the erroneous "run:" mid-chain so the shell
                # gets "cua-type X && cua-click Y" which works fine.
                command = re.sub(r'\s*&&\s*run:\s*', ' && ', command)
                command = re.sub(r'\s*;\s*run:\s*', ' ; ', command)
                return command.strip()

    raise ValueError("No 'run: <command>' line found in response")


# ── Message helpers ───────────────────────────────────────


def make_user_msg(img_b64: str, text: str) -> ChatCompletionMessageParam:
    return {
        "role": "user",
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"},
            },
            {"type": "text", "text": text},
        ],
    }


def make_assistant_msg(text: str) -> ChatCompletionMessageParam:
    return {"role": "assistant", "content": text}


def make_system_msg(text: str) -> ChatCompletionMessageParam:
    return {"role": "system", "content": text}


def trim_messages(
    messages: list[ChatCompletionMessageParam],
) -> list[ChatCompletionMessageParam]:
    """Keep system prompt + last N user/assistant pairs.

    With history_pairs=0: only system prompt survives (the loop
    appends a fresh user message each step).
    With history_pairs=1: system + last user/assistant exchange.
    """
    # Always keep the system message(s) at the start
    sys_count = 0
    for m in messages:
        if m.get("role") == "system":
            sys_count += 1
        else:
            break

    if MAX_HISTORY_PAIRS == 0:
        return messages[:sys_count]

    # Keep sys messages + last N pairs (user+assistant = 2 msgs each)
    keep = MAX_HISTORY_PAIRS * 2
    max_len = sys_count + keep
    if len(messages) <= max_len:
        return messages
    return messages[:sys_count] + messages[-keep:]


# ── Coverage helpers ──────────────────────────────────────

def _start_coverage() -> bool:
    """Start CDP coverage recording. Returns True on success."""
    try:
        r = subprocess.run(
            ["python3", "/app/tools/cua-cdp-coverage", "start"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            print(f"   📊 Coverage recording started")
            return True
        print(f"   ⚠ Coverage start failed: {r.stderr.strip() or r.stdout.strip()}")
    except Exception as e:
        print(f"   ⚠ Coverage start error: {e}")
    return False


def _coverage_snapshot() -> str:
    """Take a coverage snapshot, return summary text for prompt injection."""
    try:
        r = subprocess.run(
            ["python3", "/app/tools/cua-cdp-coverage", "snapshot"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return ""


# ── Agent loop ────────────────────────────────────────────


def run_agent(task: str, max_steps: int = MAX_STEPS) -> None:
    agent_start = time.monotonic()

    # ── Build context ──
    ctx: dict[str, Any] = {
        "cfg": cfg,
        "task": task,
        "model": MODEL,
        "step": 0,
        "screen_w": SCREEN_W,
        "screen_h": SCREEN_H,
        "extra_tools": [],
        "extra_prompt": [],
        "outcome": "unknown",
        "result": None,
        "wall_time_sec": 0,
        # Swappable functions — plugins can replace these in on_startup
        "llm_call": default_llm_call,
        "screenshot_fn": default_screenshot,
        "command_runner": default_command_runner,
        "extract_command": default_extract_command,
    }

    # ── Load plugins ──
    plugins = PluginHost()
    loaded = plugins.load_directory(PLUGINS_DIR)
    if loaded:
        print(f"🔌 Plugins: {', '.join(loaded)}")

    plugins.emit("on_startup", ctx)

    print(f"🤖 Task: {task}")
    sampling = f"temp={LLM_TEMPERATURE}, top_p={LLM_TOP_P}, presence_penalty={LLM_PRESENCE_PENALTY}"
    if LLM_EXTRA_PARAMS:
        sampling += f", extra={LLM_EXTRA_PARAMS}"
    print(f"   LLM sampling: {sampling}")

    # ── Build system prompt (after plugins had a chance to add tools) ──
    system_prompt = build_system_prompt(ctx)

    # ── Convenience wrappers for swappable functions ──
    def llm_call(messages):
        return ctx["llm_call"](ctx, messages)

    def take_screenshot():
        return ctx["screenshot_fn"](ctx)

    def run_command(command):
        return ctx["command_runner"](ctx, command)

    def extract_command(text):
        return ctx["extract_command"](ctx, text)

    # ── Set up model-specific calibration ──
    cal_path = calibration_file_for_model(cfg, MODEL)
    os.environ["CUA_CALIBRATION_FILE"] = cal_path
    print(f"   Calibration file: {cal_path}")

    messages: list[ChatCompletionMessageParam] = [make_system_msg(system_prompt)]
    errors_in_a_row = 0
    step = 0
    task_done = False
    done_payload: dict[str, Any] = {}

    # ── Step 0: calibration (skipped if cached for this model) ──
    CALIBRATION_STEPS = 3   # must match calibration/index.html STEPS count
    MAX_CAL_ATTEMPTS = 3    # retry if verification fails

    existing_cal = load_calibration(cfg)
    start_url = os.environ.get("CUA_START_URL", "")

    if existing_cal is not None:
        print(f"\n── Calibration (cached) ──")
        print(f"   scale=({existing_cal['scale_x']:.4f}, {existing_cal['scale_y']:.4f})")
    else:
        print("\n── Calibration (multi-step) ──")
        calibrated = False

        for attempt in range(MAX_CAL_ATTEMPTS):
            # Clear any stale calibration so cua-click recalibrates
            if os.path.isfile(cal_path):
                os.remove(cal_path)

            if attempt > 0:
                print(f"   Retry {attempt + 1}/{MAX_CAL_ATTEMPTS}")
                # Reload the calibration page for a fresh attempt
                run_command("cua-key F11")
                time.sleep(0.3)
                run_command("cua-key ctrl+l")
                time.sleep(0.1)
                run_command('cua-type "file:///app/calibration/index.html"')
                run_command("cua-key Return")
                time.sleep(1)
                # Re-enter fullscreen
                run_command("cua-key F11")
                time.sleep(0.5)

            # Do CALIBRATION_STEPS rounds of screenshot → click
            for click_round in range(CALIBRATION_STEPS):
                try:
                    plugins.emit("on_pre_screenshot", ctx)
                    img_b64 = take_screenshot()
                    plugins.emit("on_post_screenshot", ctx, img_b64=img_b64)

                    cal_messages = [
                        make_system_msg(system_prompt),
                        make_user_msg(img_b64, CALIBRATION_PROMPT),
                    ]

                    plugins.emit("on_pre_llm_call", ctx, messages=cal_messages)
                    reply = llm_call(cal_messages)
                    usage = ctx.pop("_last_usage", {})
                    plugins.emit("on_post_llm_call", ctx,
                                 messages=cal_messages, reply=reply, usage=usage)
                    print(f"   Click {click_round + 1}/{CALIBRATION_STEPS}: "
                          f"LLM → {reply[:120]}")

                    command = extract_command(reply)
                    plugins.emit("on_pre_command", ctx, command=command)
                    output = run_command(command)
                    plugins.emit("on_post_command", ctx,
                                 command=command, output=output)
                    print(f"   → {output[:100]}")

                except Exception as e:
                    print(f"   ⚠ Click {click_round + 1} failed: {e}")
                    if click_round == 0 and not os.path.isfile(cal_path):
                        cx, cy = SCREEN_W // 2, SCREEN_H // 2
                        run_command(f"cua-click {cx} {cy}")
                        print("   → Fallback: identity calibration")

                time.sleep(0.5)

            # Wait for page to evaluate and set window title
            time.sleep(1)

            # Check result via window title ("PASS" or "FAIL")
            title_output = run_command(
                "xdotool getactivewindow getwindowname 2>/dev/null || echo UNKNOWN"
            )
            title = title_output.split("\n")[0].strip()

            if "PASS" in title.upper():
                calibrated = True
                cal = load_calibration(cfg)
                if cal:
                    print(f"   ✓ Calibrated: "
                          f"scale=({cal['scale_x']:.4f}, {cal['scale_y']:.4f})")
                break
            else:
                print(f"   ✗ Verification failed (title: {title})")
                if os.path.isfile(cal_path):
                    os.remove(cal_path)

        if not calibrated:
            print("   ⚠ All attempts failed — using identity calibration")
            if os.path.isfile(cal_path):
                os.remove(cal_path)
            cx, cy = SCREEN_W // 2, SCREEN_H // 2
            run_command(f"cua-click {cx} {cy}")

    # ── Start coverage BEFORE navigation ──
    # CDP Profiler.startPreciseCoverage only instruments scripts loaded
    # AFTER the call.  By starting here (while still on calibration page),
    # the subsequent navigation loads the target page's scripts under the
    # profiler from the very first byte.
    if COVERAGE_ENABLED:
        _start_coverage()

    # Navigate to the task start URL (or about:blank if not set)
    run_command("cua-key F11")
    time.sleep(0.3)
    run_command("cua-key ctrl+l")
    time.sleep(0.1)
    nav_url = start_url or "about:blank"
    run_command(f'cua-type "{nav_url}"')
    run_command("cua-key Return")
    time.sleep(3 if start_url else 0.5)

    # Reset context — drop calibration exchange so the model starts fresh
    # and doesn't treat the calibration click as the user's actual task.
    messages = [make_system_msg(system_prompt)]

    # ── Main loop ──
    last_command = ""
    last_output = ""
    last_summary = ""  # text summary from model's think block
    command_history: list[str] = []  # track all commands for loop detection
    PLAN_FILE = "/tmp/cua_plan.json"

    def _extract_summary(reply: str) -> str:
        """Extract a brief status summary from the model's <think> block.

        The model already describes what it sees in its thinking. We
        capture this and inject it as text context on the next step,
        replacing the expensive image-based history.
        """
        import re
        # Extract think block content
        m = re.search(r"<think>(.*?)</think>", reply, re.DOTALL)
        if not m:
            return ""
        think = m.group(1).strip()
        # Take first ~300 chars — enough to capture page state description
        # but not the full reasoning chain
        if len(think) > 300:
            # Try to cut at a sentence boundary
            cut = think[:300].rfind(". ")
            if cut > 100:
                think = think[:cut + 1]
            else:
                think = think[:300] + "…"
        return think

    def _detect_loop() -> str:
        """Check if the agent is stuck repeating similar actions.
        Returns a warning string to inject, or '' if no loop detected."""
        import re
        if len(command_history) < 3:
            return ""

        def _normalize(cmd: str) -> str:
            """Collapse coordinates/numbers so similar clicks match."""
            return re.sub(r'\d{2,}', 'N', cmd).strip()

        last_3 = [_normalize(c) for c in command_history[-3:]]
        # All 3 identical (e.g. clicking same button 3 times)
        if last_3[0] == last_3[1] == last_3[2]:
            return (
                "⚠ WARNING: You have repeated the same action 3 times with "
                "no visible progress. The action may be working but the result "
                "is below the viewport — try scrolling down. Or try a completely "
                "different approach:\n"
                "  - Use F12 DevTools Console to extract data via JS\n"
                "  - Navigate via URL instead of clicking\n"
                "  - Use Ctrl+F to search the page\n"
                "Do NOT repeat the same action again."
            )

        # Check for click-wait loops (alternating click and wait)
        if len(command_history) >= 6:
            last_6_norm = [_normalize(c) for c in command_history[-6:]]
            # Check if it's just 2 alternating commands
            unique = set(last_6_norm)
            if len(unique) <= 2 and all(
                last_6_norm[i] == last_6_norm[i % 2]
                for i in range(6)
            ):
                return (
                    "⚠ WARNING: You are stuck in a click-wait loop (6 steps "
                    "alternating the same 2 actions). The button click IS "
                    "working — the results are likely below the viewport. "
                    "Try: scroll down, or use DevTools Console to read the "
                    "page data directly. Do NOT click the same button again."
                )

        return ""

    def _read_plan() -> str:
        """Read the current plan from disk, return formatted string or ''."""
        try:
            with open(PLAN_FILE) as f:
                plan = json.load(f)
            steps = plan.get("steps", [])
            if not steps:
                return ""
            lines = ["Current plan:"]
            for i, s in enumerate(steps, 1):
                icon = {"pending": "○", "done": "✓", "failed": "✗",
                        "skipped": "–"}.get(s["status"], "?")
                line = f"  {icon} {i}. {s['text']}"
                if s.get("note"):
                    line += f"  ({s['note']})"
                lines.append(line)
            done = sum(1 for s in steps if s["status"] == "done")
            lines.append(f"  Progress: {done}/{len(steps)} done")
            return "\n".join(lines)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return ""

    # Clear any stale plan from previous runs
    if os.path.exists(PLAN_FILE):
        os.remove(PLAN_FILE)

    for step in range(max_steps):
        ctx["step"] = step + 1
        print(f"\n── Step {step + 1} ──")

        # ── Coverage auto-snapshot ──
        coverage_text = ""
        if COVERAGE_ENABLED and step > 0 and step % COVERAGE_INTERVAL == 0:
            coverage_text = _coverage_snapshot()
            if coverage_text:
                # Find the "Total:" line for a compact log message
                for cline in coverage_text.splitlines():
                    if "Total:" in cline or "JS:" in cline:
                        print(f"   📊 {cline.strip()}")
                        break
                else:
                    print(f"   📊 Coverage snapshot taken")

        # ── Screenshot ──
        try:
            plugins.emit("on_pre_screenshot", ctx)
            img_b64 = take_screenshot()
            plugins.emit("on_post_screenshot", ctx, img_b64=img_b64)
        except Exception as e:
            print(f"   ⚠ Screenshot failed: {e}")
            time.sleep(1)
            continue

        plan_text = _read_plan()

        # ── Accessibility tree ──
        t0 = time.monotonic()
        a11y_text = get_a11y_text()
        a11y_ms = (time.monotonic() - t0) * 1000
        if a11y_text:
            a11y_lines = a11y_text.count("\n")
            print(f"   A11y tree: {a11y_lines} lines ({a11y_ms:.0f}ms)")

        if step == 0:
            parts = [
                f"Task: {task}",
                "",
                "Create a plan using cua-plan with the steps needed "
                "to complete this task.",
            ]
            if a11y_text:
                parts.append("")
                parts.append(a11y_text)
            prompt = "\n".join(parts)
        else:
            parts = [f"Task: {task}", ""]
            if last_summary:
                parts.append(f"Previous observation: {last_summary}")
                parts.append("")
            parts.extend([f"Command: {last_command}", f"Output: {last_output}"])
            if plan_text:
                parts.append("")
                parts.append(plan_text)
            if a11y_text:
                parts.append("")
                parts.append(a11y_text)
            loop_warning = _detect_loop()
            if loop_warning:
                print(f"   🔄 Loop detected — injecting warning")
                parts.append("")
                parts.append(loop_warning)
            if coverage_text:
                parts.append("")
                parts.append(f"Coverage status:\n{coverage_text}")
            parts.append("")
            parts.append(
                "Here is the updated screenshot. "
                "Update your plan progress and continue with the next step."
            )
            prompt = "\n".join(parts)
        messages.append(make_user_msg(img_b64, prompt))

        # ── LLM call ──
        try:
            plugins.emit("on_pre_llm_call", ctx, messages=messages)
            reply = llm_call(messages)
            usage = ctx.pop("_last_usage", {})
            plugins.emit("on_post_llm_call", ctx, messages=messages, reply=reply, usage=usage)
        except Exception as e:
            print(f"   ⚠ API error: {e}")
            messages.pop()
            errors_in_a_row += 1
            if errors_in_a_row >= MAX_API_ERRORS:
                print(f"   ✗ Too many API errors ({MAX_API_ERRORS}), stopping.")
                break
            time.sleep(2)
            continue

        messages.append(make_assistant_msg(reply))
        print(f"   LLM → {reply[:200]}{'…' if len(reply) > 200 else ''}")

        # Extract status summary for next step's context
        last_summary = _extract_summary(reply)

        # ── Parse ──
        try:
            command = extract_command(reply)
        except Exception as e:
            print(f"   ⚠ Parse error: {e}")
            errors_in_a_row += 1
            if errors_in_a_row >= MAX_PARSE_ERRORS:
                print(f"   ✗ Too many parse errors ({MAX_PARSE_ERRORS}), stopping.")
                break
            continue

        errors_in_a_row = 0
        print(f"   Command: {command}")

        if not command:
            print("   ⚠ Empty command, skipping")
            continue

        # ── Execute ──
        plugins.emit("on_pre_command", ctx, command=command)
        output = run_command(command)
        plugins.emit("on_post_command", ctx, command=command, output=output)
        print(f"   Output: {output[:300]}{'…' if len(output) > 300 else ''}")

        # Store for next iteration's user message
        last_command = command
        last_output = output[:500]  # Truncate for context window
        command_history.append(command)

        # ── Check for done marker ──
        if DONE_MARKER in output:
            task_done = True
            marker_idx = output.index(DONE_MARKER) + len(DONE_MARKER)
            remainder = output[marker_idx:].strip()
            for line in remainder.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        done_payload = json.loads(line)
                    except json.JSONDecodeError:
                        pass
                    break

            summary = done_payload.get("summary", "Task completed")
            result = done_payload.get("result")
            ctx["result"] = result

            # Full payload (with screenshot_b64) is written to
            # /tmp/cua_done.json by cua-done itself.

            print(f"\n✅ Done: {summary}")
            if result:
                print(f"   Result: {result}")
            if done_payload.get("has_screenshot"):
                print("   📸 Screenshot included")

            plugins.emit("on_task_complete", ctx, summary=summary, result=result)
            break

        # Feed output back (via last_command/last_output in next prompt)
        time.sleep(POST_ACTION_DELAY)
        messages = trim_messages(messages)

    # ── Shutdown ──
    elapsed = time.monotonic() - agent_start
    ctx["wall_time_sec"] = elapsed
    ctx["step"] = step + 1
    ctx["outcome"] = "completed" if task_done else "max_steps"

    plugins.emit("on_shutdown", ctx)

    if not task_done:
        print("\n⚠ Reached max steps.")


if __name__ == "__main__":
    task = (
        " ".join(sys.argv[1:])
        or "Open Chromium and find Artificial Intelligence article in Wikipedia"
        " (by loading wikipedia.org first and searching)."
        " Answer with the first link from the article references."
    )
    run_agent(task)
