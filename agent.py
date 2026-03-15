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
from plugin_host import PluginHost
from tool_discovery import discover_tools

# ── Done marker (must match tools/cua-done) ──────────────
DONE_MARKER = "@@CUA_TASK_COMPLETE@@"

# ── Configuration ─────────────────────────────────────────
cfg = load_config()

SCREEN_W = cfg.getint("screen", "width")
SCREEN_H = cfg.getint("screen", "height")

MAX_STEPS = cfg.getint("agent", "max_steps")
POST_ACTION_DELAY = cfg.getfloat("agent", "post_action_delay")

LLM_MAX_TOKENS = cfg.getint("llm", "max_tokens")
LLM_TEMPERATURE = cfg.getfloat("llm", "temperature")
MAX_API_ERRORS = cfg.getint("llm", "max_api_errors")
MAX_PARSE_ERRORS = cfg.getint("llm", "max_parse_errors")
MAX_HISTORY_PAIRS = cfg.getint("llm", "max_history_pairs")
PREAMBLE_SIZE = cfg.getint("llm", "preamble_size")

MAX_OUTPUT_BYTES = cfg.getint("run", "max_output_bytes")
RUN_OUTPUT_DIR = cfg.get("run", "output_dir")

MODEL = os.environ.get("CUA_MODEL", "your-model-name")

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

Available CLI tools (run any with --help for usage):

{tools}

You may also run arbitrary shell commands (ls, cat, curl, grep, etc.).
Coordinates are in your pixel space — calibration mapping is automatic.

When the task is complete, use the cua-done tool with a summary.
When asked to find or report a value, pass it via --result.
{extra}
Examples:
  run: cua-click 450 300
  run: cua-type "hello world"
  run: cua-key ctrl+a
  run: cua-done "Opened Wikipedia" --result "https://en.wikipedia.org"
  run: cat /etc/os-release | head -5
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
    )
    elapsed = time.monotonic() - t0

    content = response.choices[0].message.content
    if content is None:
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

    # Strip <think>…</think> blocks (Qwen, DeepSeek, etc.)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "</think>" in text:
        text = text.split("</think>")[-1]
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
                return command

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
    max_len = PREAMBLE_SIZE + MAX_HISTORY_PAIRS * 2
    if len(messages) <= max_len:
        return messages
    return messages[:PREAMBLE_SIZE] + messages[-(MAX_HISTORY_PAIRS * 2):]


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
    if existing_cal is not None:
        print(f"\n── Calibration (cached) ──")
        print(f"   scale=({existing_cal['scale_x']:.4f}, {existing_cal['scale_y']:.4f})")
        # Exit fullscreen and dismiss the calibration page
        run_command("cua-key F11")
        time.sleep(0.3)
        run_command("cua-key ctrl+l")
        time.sleep(0.1)
        run_command('cua-type "about:blank"')
        run_command("cua-key Return")
        time.sleep(0.5)
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

        # Exit fullscreen and dismiss calibration page
        run_command("cua-key F11")
        time.sleep(0.3)
        run_command("cua-key ctrl+l")
        time.sleep(0.1)
        run_command('cua-type "about:blank"')
        run_command("cua-key Return")
        time.sleep(0.5)

    # Reset context — drop calibration exchange so the model starts fresh
    # and doesn't treat the calibration click as the user's actual task.
    messages = [make_system_msg(system_prompt)]

    # ── Main loop ──
    for step in range(max_steps):
        ctx["step"] = step + 1
        print(f"\n── Step {step + 1} ──")

        # ── Screenshot ──
        try:
            plugins.emit("on_pre_screenshot", ctx)
            img_b64 = take_screenshot()
            plugins.emit("on_post_screenshot", ctx, img_b64=img_b64)
        except Exception as e:
            print(f"   ⚠ Screenshot failed: {e}")
            time.sleep(1)
            continue

        prompt = (
            f"Task: {task}"
            if step == 0
            else "Here is the updated screenshot. Continue with the task."
        )
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

            print(f"\n✅ Done: {summary}")
            if result:
                print(f"   Result: {result}")

            plugins.emit("on_task_complete", ctx, summary=summary, result=result)
            break

        # Feed output back
        messages.append({"role": "user", "content": output})
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
