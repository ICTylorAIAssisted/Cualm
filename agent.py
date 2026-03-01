#!/usr/bin/env python3
"""Minimal CUA agent: screenshot → LLM → action loop."""

import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

# ── Point at your local endpoint ──────────────────────────
client = OpenAI(
    base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1"),
    api_key=os.environ.get("OPENAI_API_KEY", "not-needed"),
)
MODEL = os.environ.get("CUA_MODEL", "your-model-name")

# ── Configuration Constants ───────────────────────────────

# Screen dimensions
DEFAULT_SCREEN_WIDTH = 1280
DEFAULT_SCREEN_HEIGHT = 800

# File paths
DEFAULT_AUDIT_BASE_DIR = "/app/audit"
DEFAULT_SCREENSHOT_PATH = "/tmp/screen.png"

# Timestamp formats
SESSION_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
AUDIT_FILENAME_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S_%f"

# Timing defaults
MOUSE_MOVE_DELAY_SEC = 0.1
DOUBLE_CLICK_REPEAT_COUNT = 2
DOUBLE_CLICK_DELAY_MS = 100
SCROLL_DEFAULT_CLICKS = 3
WAIT_DEFAULT_MS = 1000
POST_ACTION_DELAY_SEC = 0.8

# Mouse button constants
DEFAULT_BUTTON = 1

# LLM call defaults
LLM_MAX_TOKENS = 8192
LLM_TEMPERATURE = 0

# Error handling thresholds
MAX_API_ERRORS_IN_A_ROW = 3
MAX_PARSE_ERRORS_IN_A_ROW = 5

# Conversation history: keep preamble (4 msgs) + last N screenshot→action pairs
MAX_HISTORY_PAIRS = 4
PREAMBLE_SIZE = 4  # system, calibration user, calibration assistant, first task

# Agent defaults
DEFAULT_MAX_STEPS = 50

# ── Audit ─────────────────────────────────────────────────


def create_session_dir(base: str = DEFAULT_AUDIT_BASE_DIR) -> str:
    """Create a unique session directory for this agent run."""
    timestamp = datetime.now().strftime(SESSION_TIMESTAMP_FORMAT)
    session_dir = os.path.join(base, f"session_{timestamp}")
    os.makedirs(session_dir, exist_ok=True)
    return session_dir


SESSION_DIR = create_session_dir()

# ── Coordinate calibration ────────────────────────────────

SCREEN_W = int(os.environ.get("SCREEN_WIDTH", DEFAULT_SCREEN_WIDTH))
SCREEN_H = int(os.environ.get("SCREEN_HEIGHT", DEFAULT_SCREEN_HEIGHT))


class CoordinateMapper:
    def __init__(self) -> None:
        self.calibrated = False
        self.scale_x = 1.0
        self.scale_y = 1.0

    def calibrate_from_center(self, model_x: float, model_y: float) -> None:
        """Model was asked to click screen center. Compare to actual center."""
        actual_cx = SCREEN_W / 2
        actual_cy = SCREEN_H / 2

        if model_x <= 0 or model_y <= 0:
            print(f"   ⚠ Calibration failed: model returned ({model_x}, {model_y})")
            return

        self.scale_x = actual_cx / model_x
        self.scale_y = actual_cy / model_y
        self.calibrated = True

        print(
            f"   📐 Model center: ({model_x}, {model_y})"
            f" → Actual: ({actual_cx:.0f}, {actual_cy:.0f})"
        )
        print(f"      Scale: {self.scale_x:.3f}x, {self.scale_y:.3f}y")

    def map(self, x: float, y: float) -> tuple[int, int]:
        sx = int(x * self.scale_x)
        sy = int(y * self.scale_y)
        return max(0, min(SCREEN_W - 1, sx)), max(0, min(SCREEN_H - 1, sy))

    def info(self) -> dict[str, Any]:
        return {
            "calibrated": self.calibrated,
            "scale_x": self.scale_x,
            "scale_y": self.scale_y,
        }


coord_mapper = CoordinateMapper()

# ── Token & timing tracking ───────────────────────────────


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
        if self.total_llm_time_sec <= 0:
            return 0.0
        return self.completion_tokens / self.total_llm_time_sec

    def print_step(self, prompt: int, completion: int, elapsed: float) -> None:
        step_tps = completion / elapsed if elapsed > 0 else 0.0
        print(
            f"   Tokens: {prompt}→{completion}"
            f" ({elapsed:.1f}s, {step_tps:.1f} tok/s)"
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

# ── Tool implementations ──────────────────────────────────


def take_screenshot(path: str = DEFAULT_SCREENSHOT_PATH) -> str:
    """Take screenshot, save audit copy, return base64 PNG."""
    subprocess.run(
        ["scrot", "--pointer", path, "--overwrite"],
        check=True,
        env={**os.environ, "DISPLAY": ":99"},
    )

    timestamp = datetime.now().strftime(AUDIT_FILENAME_TIMESTAMP_FORMAT)[:-3]
    audit_path = os.path.join(SESSION_DIR, f"{timestamp}.png")
    subprocess.run(["cp", path, audit_path], check=True)

    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def type_text(text: str) -> None:
    subprocess.run(
        ["xdotool", "type", "--clearmodifiers", "--delay", "30", text], check=True
    )


def press_key(keys: str) -> None:
    subprocess.run(["xdotool", "key", "--clearmodifiers", keys], check=True)


def mouse_click(x: int, y: int, button: int = DEFAULT_BUTTON) -> None:
    sx, sy = coord_mapper.map(x, y)
    print(f"      Coords: model({x},{y}) → screen({sx},{sy})")
    subprocess.run(["xdotool", "mousemove", "--sync", str(sx), str(sy)], check=True)
    time.sleep(MOUSE_MOVE_DELAY_SEC)
    subprocess.run(["xdotool", "click", str(button)], check=True)


def mouse_double_click(x: int, y: int) -> None:
    sx, sy = coord_mapper.map(x, y)
    subprocess.run(["xdotool", "mousemove", "--sync", str(sx), str(sy)], check=True)
    time.sleep(MOUSE_MOVE_DELAY_SEC)
    subprocess.run(
        [
            "xdotool",
            "click",
            "--repeat",
            str(DOUBLE_CLICK_REPEAT_COUNT),
            "--delay",
            str(DOUBLE_CLICK_DELAY_MS),
            "1",
        ],
        check=True,
    )


def scroll(
    x: int, y: int, direction: str = "down", clicks: int = SCROLL_DEFAULT_CLICKS
) -> None:
    sx, sy = coord_mapper.map(x, y)
    subprocess.run(["xdotool", "mousemove", "--sync", str(sx), str(sy)], check=True)
    subprocess.run(
        [
            "xdotool",
            "click",
            "--repeat",
            str(clicks),
            "5" if direction == "down" else "4",
        ],
        check=True,
    )


def drag(x1: int, y1: int, x2: int, y2: int) -> None:
    sx1, sy1 = coord_mapper.map(x1, y1)
    sx2, sy2 = coord_mapper.map(x2, y2)
    subprocess.run(["xdotool", "mousemove", "--sync", str(sx1), str(sy1)], check=True)
    subprocess.run(["xdotool", "mousedown", "1"], check=True)
    subprocess.run(["xdotool", "mousemove", "--sync", str(sx2), str(sy2)], check=True)
    subprocess.run(["xdotool", "mouseup", "1"], check=True)


ACTIONS: dict[str, Any] = {
    "click": lambda a: mouse_click(a["x"], a["y"], a.get("button", DEFAULT_BUTTON)),
    "double_click": lambda a: mouse_double_click(a["x"], a["y"]),
    "type": lambda a: type_text(a["text"]),
    "key": lambda a: press_key(a["keys"]),
    "scroll": lambda a: scroll(
        a["x"],
        a["y"],
        a.get("direction", "down"),
        a.get("clicks", SCROLL_DEFAULT_CLICKS),
    ),
    "drag": lambda a: drag(a["x1"], a["y1"], a["x2"], a["y2"]),
    "wait": lambda a: time.sleep(a.get("ms", WAIT_DEFAULT_MS) / 1000),
    "done": lambda a: None,
}

# ── Prompts ────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a computer-use agent. You see screenshots of a Linux desktop with Chromium.

Given the current screenshot and a task, return EXACTLY ONE JSON action per turn.
Return ONLY the JSON object.

Available actions:
  {"type": "click", "x": <num>, "y": <num>}
  {"type": "double_click", "x": <num>, "y": <num>}
  {"type": "type", "text": "<string>"}
  {"type": "key", "keys": "<combo>"}
  {"type": "scroll", "x": <num>, "y": <num>, "direction": "down"|"up"}
  {"type": "drag", "x1": <num>, "y1": <num>, "x2": <num>, "y2": <num>}
  {"type": "wait", "ms": <int>}
  {"type": "done", "summary": "<what you accomplished>", "result": "<extracted_result>"}

When asked to find, extract, or report something specific (e.g., "what is...", "find...",
"report the..."), include the extracted value in the "result" field.
For other tasks, omit the "result" field.
"""

CALIBRATION_PROMPT = """Click center."""

# Prompt for final result extraction when model omitted the result field
FINAL_EXTRACT_PROMPT = """Based on what you see in the screenshot, extract the answer to the original task.

Original task: {task}

If the task asked you to find or report something specific (like a link, text, value, or measurement),
return the exact content in the "result" field. If no specific result is needed, return an empty string.

Reply with JSON format: {{"type": "done", "summary": "...", "result": "..."}}"""

# ── Helpers ────────────────────────────────────────────────


def make_user_message(img_b64: str, text: str) -> ChatCompletionMessageParam:
    """Build a user message with image and text."""
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


def make_assistant_message(text: str) -> ChatCompletionMessageParam:
    """Build an assistant message."""
    return {"role": "assistant", "content": text}


def make_system_message(text: str) -> ChatCompletionMessageParam:
    """Build a system message."""
    return {"role": "system", "content": text}


def llm_call(messages: list[ChatCompletionMessageParam]) -> str:
    """Call the LLM, track usage/timing, return text content."""
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

    prompt_tok = response.usage.prompt_tokens if response.usage else 0
    completion_tok = response.usage.completion_tokens if response.usage else 0
    total_tok = response.usage.total_tokens if response.usage else 0

    usage_tracker.record(prompt_tok, completion_tok, total_tok, elapsed)
    usage_tracker.print_step(prompt_tok, completion_tok, elapsed)

    return content


def trim_messages(
    messages: list[ChatCompletionMessageParam],
) -> list[ChatCompletionMessageParam]:
    """Keep preamble + last N screenshot→action pairs."""
    max_len = PREAMBLE_SIZE + MAX_HISTORY_PAIRS * 2
    if len(messages) <= max_len:
        return messages
    preamble = messages[:PREAMBLE_SIZE]
    recent = messages[-(MAX_HISTORY_PAIRS * 2) :]
    return preamble + recent


def extract_json(text: str) -> dict[str, Any]:
    """Extract JSON from LLM response, handling think tags and markdown fences."""
    text = text.strip()

    # Strip <think>...</think> blocks (Qwen, DeepSeek, etc.)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    # Handle case where response starts mid-think (no opening tag)
    if "</think>" in text:
        text = text.split("</think>")[-1]

    text = text.strip()

    # Strip markdown code fences if present
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    # Find the first JSON object in the remaining text
    match = re.search(r"\{.*:.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise json.JSONDecodeError("No JSON found", text, 0)


KEY_MAP = {
    "enter": "Return",
    "return": "Return",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "space": "space",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "pagedown": "Page_Down",
    "f1": "F1",
    "f2": "F2",
    "f3": "F3",
    "f4": "F4",
    "f5": "F5",
    "f6": "F6",
    "f7": "F7",
    "f8": "F8",
    "f9": "F9",
    "f10": "F10",
    "f11": "F11",
    "f12": "F12",
    "ctrl": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "super": "super",
    "meta": "super",
    "cmd": "super",
}

TYPE_ALIASES: dict[str, str] = {
    "left_click": "click",
    "right_click": "click",
    "doubleclick": "double_click",
    "double-click": "double_click",
    "enter_text": "type",
    "input_text": "type",
    "text": "type",
    "keypress": "key",
    "hotkey": "key",
    "press": "key",
    "screenshot": "wait",
    "pause": "wait",
    "finish": "done",
    "complete": "done",
}


def normalize_action(action: dict[str, Any]) -> dict[str, Any]:
    """Fix common model quirks: [x,y] arrays, stringified numbers, etc."""
    action = dict(action)

    # Handle coordinates as array: {"x": [906, 102]} → {"x": 906, "y": 102}
    for coord in ("x", "y", "x1", "y1", "x2", "y2"):
        if coord in action and isinstance(action[coord], list):
            arr = action[coord]
            if coord == "x" and "y" not in action and len(arr) >= 2:
                action["x"] = arr[0]
                action["y"] = arr[1]
            elif coord == "x1" and "y1" not in action and len(arr) >= 2:
                action["x1"] = arr[0]
                action["y1"] = arr[1]
            elif coord == "x2" and "y2" not in action and len(arr) >= 2:
                action["x2"] = arr[0]
                action["y2"] = arr[1]

    # Handle {"coordinates": [x, y]} format
    if "coordinates" in action and isinstance(action["coordinates"], list):
        coords = action["coordinates"]
        if len(coords) >= 2:
            action.setdefault("x", coords[0])
            action.setdefault("y", coords[1])

    # Handle {"position": {"x": ..., "y": ...}} nesting
    if "position" in action and isinstance(action["position"], dict):
        action.setdefault("x", action["position"].get("x"))
        action.setdefault("y", action["position"].get("y"))

    # Stringify → int for coordinates
    for key in ("x", "y", "x1", "y1", "x2", "y2", "button", "clicks", "ms"):
        if key in action and isinstance(action[key], str):
            try:
                action[key] = int(float(action[key]))
            except ValueError:
                pass

    # Normalize direction aliases
    if "direction" in action:
        d = str(action["direction"]).lower()
        action["direction"] = "up" if d in ("up", "u", "scroll_up") else "down"

    # Normalize action type aliases
    if action.get("type") in TYPE_ALIASES:
        original = action["type"]
        action["type"] = TYPE_ALIASES[original]
        if original == "right_click":
            action["button"] = 3

    # Handle "text" field named differently
    for alt in ("input", "value", "content", "string"):
        if alt in action and "text" not in action:
            action["text"] = action[alt]

    # Handle "keys" field named differently
    for alt in ("key", "hotkey", "combo", "shortcut"):
        if alt in action and "keys" not in action:
            action["keys"] = action[alt]

    # Normalize keys list → string FIRST
    if "keys" in action and isinstance(action["keys"], list):
        action["keys"] = "+".join(str(k) for k in action["keys"])

    # THEN map key names to xdotool format
    if "keys" in action and isinstance(action["keys"], str):
        parts = re.split(r"[+\s]+", action["keys"])
        mapped = [KEY_MAP.get(p.lower(), p) for p in parts]
        action["keys"] = "+".join(mapped)

    return action


# ── Agent loop ─────────────────────────────────────────────


def run_agent(task: str, max_steps: int = DEFAULT_MAX_STEPS) -> None:
    agent_start = time.monotonic()
    print(f"🤖 Task: {task}")
    print(f"📁 Audit: {SESSION_DIR}")

    # Save task metadata
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

    messages: list[ChatCompletionMessageParam] = [make_system_message(SYSTEM_PROMPT)]
    errors_in_a_row = 0
    step = 0
    action_type = "unknown"

    # ── Step 0: Calibration ──
    print("\n── Calibration ──")
    img_b64 = take_screenshot()
    messages.append(make_user_message(img_b64, CALIBRATION_PROMPT))

    action = {"type": "wait", "ms": 100}  # Default fallback if calibration fails

    try:
        reply = llm_call(messages)
        messages.append(make_assistant_message(reply))
        print(f"   LLM → {reply[:200]}")

        action = normalize_action(extract_json(reply))
        if action.get("type") == "click":
            coord_mapper.calibrate_from_center(action["x"], action["y"])
        else:
            print("   ⚠ Unexpected response, using identity mapping")
    except Exception as e:
        print(f"   ⚠ Calibration failed: {e}, using identity mapping")

    cal_info = coord_mapper.info()
    print(f"   📐 Mapper: {cal_info}")

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
        messages.append(make_user_message(img_b64, prompt))

        # ── LLM call with retry ──
        try:
            reply = llm_call(messages)
        except Exception as e:
            print(f"   ⚠ API error: {e}")
            messages.pop()
            errors_in_a_row += 1
            if errors_in_a_row >= MAX_API_ERRORS_IN_A_ROW:
                print(
                    f"   ✗ Too many API errors ({MAX_API_ERRORS_IN_A_ROW}), stopping."
                )
                break
            time.sleep(2)
            continue

        messages.append(make_assistant_message(reply))
        print(f"   LLM → {reply[:200]}{'...' if len(reply) > 200 else ''}")

        # ── Parse action ──
        try:
            action = normalize_action(extract_json(reply))
        except Exception as e:
            print(f"   ⚠ Parse error: {e}")
            errors_in_a_row += 1
            if errors_in_a_row >= MAX_PARSE_ERRORS_IN_A_ROW:
                print(
                    f"   ✗ Too many parse errors ({MAX_PARSE_ERRORS_IN_A_ROW}),"
                    " stopping."
                )
                break
            continue

        errors_in_a_row = 0
        action_type = action.get("type", "unknown")
        print(f"   Action: {json.dumps(action)}")

        if action_type == "done":
            result = action.get("result")
            summary = action.get("summary", "Task completed")

            # If result is missing but the task asked for something specific,
            # ask the model to extract it from the final screenshot
            if not result:
                print("\n── Final Answer Extraction ──")
                img_b64 = take_screenshot()
                messages.append(
                    make_user_message(img_b64, FINAL_EXTRACT_PROMPT.format(task=task))
                )

                try:
                    reply = llm_call(messages)
                    messages.append(make_assistant_message(reply))

                    action = normalize_action(extract_json(reply))
                    result = action.get("result")
                    summary = action.get("summary", summary)
                    print(f"   Extracted: {result}")
                except Exception as e:
                    print(f"   ⚠ Final extraction failed: {e}")

            print(f"\n✅ Done: {summary}")
            break

        # ── Execute action ──
        if action_type in ACTIONS:
            try:
                ACTIONS[action_type](action)
                print(f"   ✓ Executed: {action_type}")
            except Exception as e:
                print(f"   ⚠ Action failed: {e} — continuing anyway")
        else:
            print(f"   ⚠ Unknown action: {action_type} — skipping")

        time.sleep(POST_ACTION_DELAY_SEC)

        # Trim conversation history
        messages = trim_messages(messages)

    # ── Session Summary ──
    agent_elapsed = time.monotonic() - agent_start
    print("\n── Session Summary ──")
    print(f"   Steps: {step + 1}")
    print(f"   Wall time: {agent_elapsed:.1f}s")
    print(f"   LLM calls: {usage_tracker.llm_calls}")
    print(f"   LLM time: {usage_tracker.total_llm_time_sec:.1f}s")
    print(
        f"   Tokens: {usage_tracker.prompt_tokens} prompt"
        f" + {usage_tracker.completion_tokens} completion"
        f" = {usage_tracker.total_tokens} total"
    )
    print(f"   Avg throughput: {usage_tracker.tokens_per_sec():.1f} completion tok/s")

    # ── Save final metadata ──
    with open(os.path.join(SESSION_DIR, "metadata.json"), "r+") as f:
        meta = json.load(f)
        meta["finished_at"] = datetime.now().isoformat()
        meta["steps"] = step + 1
        meta["outcome"] = "completed" if action_type == "done" else "max_steps"
        meta["wall_time_sec"] = round(agent_elapsed, 2)
        meta["calibration"] = cal_info
        meta["usage"] = usage_tracker.info()
        # Add result field from the done action (for question-based tasks)
        if action_type == "done":
            result_value = action.get("result")
            if result_value is not None:
                meta["result"] = result_value
        f.seek(0)
        json.dump(meta, f, indent=2)
        f.truncate()

    if action_type != "done":
        print("\n⚠ Reached max steps.")


if __name__ == "__main__":
    task = (
        " ".join(sys.argv[1:])
        or "Open Chromium and search Artificial Intelligence in the Wikipedia (by loading wikipedia.org first)."
        " Answer with the first link from the references."
    )
    run_agent(task)
