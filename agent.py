#!/usr/bin/env python3
"""CUA agent: screenshot → LLM → shell command loop.

The model sees a Linux desktop via screenshots and controls it by
emitting shell commands.  Each action is a stateless CLI tool (click,
type, key, …) that the model invokes through a single "run" interface.

Calibration is handled by the tools themselves: the `click` tool
auto-calibrates on first use, and other coordinate tools refuse to
run until calibration exists.

Task completion is signalled by the `done` tool, which emits a
structured marker the agent detects.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from cua_config import load_config

# ── Done marker (must match tools/done) ───────────────────
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

AUDIT_BASE = cfg.get("audit", "base_dir")

# ── OpenAI client ─────────────────────────────────────────
client = OpenAI(
    base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1"),
    api_key=os.environ.get("OPENAI_API_KEY", "not-needed"),
)
MODEL = os.environ.get("CUA_MODEL", "your-model-name")

# ── Audit ─────────────────────────────────────────────────


def create_session_dir() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = os.path.join(AUDIT_BASE, f"session_{ts}")
    os.makedirs(d, exist_ok=True)
    return d


SESSION_DIR = create_session_dir()

# ── Token tracking ────────────────────────────────────────


class UsageTracker:
    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.llm_calls = 0
        self.total_llm_time_sec = 0.0

    def record(self, prompt: int, completion: int, total: int, elapsed: float) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total
        self.llm_calls += 1
        self.total_llm_time_sec += elapsed

    def tokens_per_sec(self) -> float:
        return (
            self.completion_tokens / self.total_llm_time_sec
            if self.total_llm_time_sec > 0
            else 0.0
        )

    def print_step(self, prompt: int, completion: int, elapsed: float) -> None:
        tps = completion / elapsed if elapsed > 0 else 0.0
        print(
            f"   Tokens: {prompt}→{completion}"
            f" ({elapsed:.1f}s, {tps:.1f} tok/s)"
            f" | Session: {self.total_tokens} total"
            f", {self.tokens_per_sec():.1f} avg tok/s"
        )

    def info(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "llm_calls": self.llm_calls,
            "total_llm_time_sec": round(self.total_llm_time_sec, 2),
            "avg_completion_tokens_per_sec": round(self.tokens_per_sec(), 2),
        }


usage_tracker = UsageTracker()

# ── Run tool ──────────────────────────────────────────────


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


def run_command(command: str) -> str:
    """Execute *command* in a shell, return formatted output."""
    call_uid = uuid.uuid4().hex[:12]

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=30,
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


# ── Screenshot ────────────────────────────────────────────


def take_screenshot() -> str:
    """Take a screenshot via the screenshot tool, return base64."""
    result = subprocess.run(
        ["cua-screenshot"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"screenshot failed: {result.stderr.strip()}")

    b64 = result.stdout.strip()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    audit_path = os.path.join(SESSION_DIR, f"{ts}.png")
    with open(audit_path, "wb") as f:
        f.write(base64.b64decode(b64))

    return b64


# ── LLM helpers ───────────────────────────────────────────

SYSTEM_PROMPT = f"""You are a computer-use agent. You see screenshots of a Linux desktop ({SCREEN_W}x{SCREEN_H}) with Chromium.

You control the computer by running shell commands. Each turn, think
briefly about what to do, then write exactly one command on a line
starting with "run: ". Everything after "run: " is executed in a shell.

Available CLI tools (run any with --help for usage, or run "help" for a summary):

  cua-click X Y [--button N] [--double] [--right]   Click at coordinates
  cua-type "text" [--delay MS]                       Type text
  cua-key COMBO                                      Press keys (ctrl+a, Return, alt+F4)
  cua-scroll X Y [--direction up|down] [--clicks N]  Scroll at position
  cua-drag X1 Y1 X2 Y2                               Drag between points
  cua-wait [MS]                                       Wait (default 1000ms)
  cua-screenshot                                      Take screenshot (base64 to stdout)
  cua-done "summary" [--result "value"]               Signal task completion
  cua-help [tool]                                     List tools or show detailed usage

You may also run arbitrary shell commands (ls, cat, curl, grep, etc.).
Coordinates are in the model's pixel space — calibration mapping is automatic.

When the task is complete, use the done tool with a summary.
When asked to find or report a value, pass it via --result.

Examples:
  run: cua-click 640 400
  run: cua-type "hello world"
  run: cua-key ctrl+a
  run: cua-done "Opened Wikipedia" --result "https://en.wikipedia.org"
  run: cat /etc/os-release | head -5
"""

CALIBRATION_PROMPT = "Click the exact center of the screen."


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


def llm_call(messages: list[ChatCompletionMessageParam]) -> str:
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

    p = response.usage.prompt_tokens if response.usage else 0
    c = response.usage.completion_tokens if response.usage else 0
    t = response.usage.total_tokens if response.usage else 0
    usage_tracker.record(p, c, t, elapsed)
    usage_tracker.print_step(p, c, elapsed)

    return content


def trim_messages(
    messages: list[ChatCompletionMessageParam],
) -> list[ChatCompletionMessageParam]:
    max_len = PREAMBLE_SIZE + MAX_HISTORY_PAIRS * 2
    if len(messages) <= max_len:
        return messages
    return messages[:PREAMBLE_SIZE] + messages[-(MAX_HISTORY_PAIRS * 2) :]


def extract_command(text: str) -> str:
    """Extract the shell command from an LLM reply.

    Looks for a line starting with 'run:' (case-insensitive), after
    stripping <think> blocks, markdown fences, and surrounding prose.
    Returns the command string, or raises ValueError if not found.
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

    raise ValueError(f"No 'run: <command>' line found in response")


# ── Agent loop ────────────────────────────────────────────


def run_agent(task: str, max_steps: int = MAX_STEPS) -> None:
    agent_start = time.monotonic()
    print(f"🤖 Task: {task}")
    print(f"📁 Audit: {SESSION_DIR}")

    # Remove stale calibration from previous sessions
    cal_file = cfg.get("calibration", "file")
    if os.path.isfile(cal_file):
        os.remove(cal_file)
        print(f"   Cleared old calibration: {cal_file}")

    with open(os.path.join(SESSION_DIR, "metadata.json"), "w") as f:
        json.dump(
            {
                "task": task,
                "model": MODEL,
                "started_at": datetime.now().isoformat(),
                "screen": f"{SCREEN_W}x{SCREEN_H}",
            },
            f,
            indent=2,
        )

    messages: list[ChatCompletionMessageParam] = [make_system_msg(SYSTEM_PROMPT)]
    errors_in_a_row = 0
    step = 0
    task_done = False
    done_payload: dict[str, Any] = {}

    # ── Step 0: calibration ──
    print("\n── Calibration ──")
    img_b64 = take_screenshot()
    messages.append(make_user_msg(img_b64, CALIBRATION_PROMPT))

    try:
        reply = llm_call(messages)
        messages.append(make_assistant_msg(reply))
        print(f"   LLM → {reply[:200]}")

        command = extract_command(reply)
        output = run_command(command)
        print(f"   Output: {output}")
        messages.append({"role": "user", "content": output})

    except Exception as e:
        print(f"   ⚠ Calibration failed: {e}, running identity calibration")
        # Force identity calibration by clicking actual center
        cx, cy = SCREEN_W // 2, SCREEN_H // 2
        output = run_command(f"click {cx} {cy}")
        print(f"   Fallback: {output}")
        messages.append(make_assistant_msg(f"run: click {cx} {cy}"))
        messages.append({"role": "user", "content": output})

    # ── Main loop ──
    for step in range(max_steps):
        print(f"\n── Step {step + 1} ──")

        try:
            img_b64 = take_screenshot()
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
            reply = llm_call(messages)
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
        output = run_command(command)
        print(f"   Output: {output[:300]}{'…' if len(output) > 300 else ''}")

        # ── Check for done marker ──
        if DONE_MARKER in output:
            task_done = True
            # Parse the JSON payload after the marker
            marker_idx = output.index(DONE_MARKER) + len(DONE_MARKER)
            remainder = output[marker_idx:].strip()
            # The next line should be JSON
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

            print(f"\n✅ Done: {summary}")
            if result:
                print(f"   Result: {result}")
            break

        # Feed output back
        messages.append({"role": "user", "content": output})
        time.sleep(POST_ACTION_DELAY)
        messages = trim_messages(messages)

    # ── Summary ──
    elapsed = time.monotonic() - agent_start
    print("\n── Session Summary ──")
    print(f"   Steps: {step + 1}")
    print(f"   Wall time: {elapsed:.1f}s")
    print(f"   LLM calls: {usage_tracker.llm_calls}")
    print(f"   LLM time:  {usage_tracker.total_llm_time_sec:.1f}s")
    print(
        f"   Tokens: {usage_tracker.prompt_tokens} prompt"
        f" + {usage_tracker.completion_tokens} completion"
        f" = {usage_tracker.total_tokens} total"
    )
    print(f"   Avg throughput: {usage_tracker.tokens_per_sec():.1f} completion tok/s")

    with open(os.path.join(SESSION_DIR, "metadata.json"), "r+") as f:
        meta = json.load(f)
        meta["finished_at"] = datetime.now().isoformat()
        meta["steps"] = step + 1
        meta["outcome"] = "completed" if task_done else "max_steps"
        meta["wall_time_sec"] = round(elapsed, 2)
        meta["usage"] = usage_tracker.info()
        if done_payload.get("result") is not None:
            meta["result"] = done_payload["result"]
        f.seek(0)
        json.dump(meta, f, indent=2)
        f.truncate()

    if not task_done:
        print("\n⚠ Reached max steps.")


if __name__ == "__main__":
    task = (
        " ".join(sys.argv[1:])
        or "Open Chromium and search Artificial Intelligence in Wikipedia"
        " (by loading wikipedia.org first)."
        " Answer with the first link from the references."
    )
    run_agent(task)
