"""Audit plugin — persist screenshots, step trace, and session metadata.

Creates a timestamped session directory under the configured audit
base path.  Captures:

  - **Screenshots** — PNG per step (on_post_screenshot)
  - **Step trace** — JSON log with LLM reply, command, output, and
    timing for every step (trace.json)
  - **Metadata** — task, model, outcome, usage totals (metadata.json)

The step trace is the primary debugging tool for benchmark runs.
Each entry records the full LLM reasoning (including <think> blocks),
the extracted command, the command's output, and token usage — enough
to understand why the agent did what it did at each step.

Delete this file to disable audit logging entirely.
"""

import base64
import json
import os
from datetime import datetime


def on_startup(ctx):
    """Create the session directory and write initial metadata."""
    audit_base = ctx["cfg"].get("audit", "base_dir")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = os.path.join(audit_base, f"session_{ts}")
    os.makedirs(session_dir, exist_ok=True)

    ctx["audit_session_dir"] = session_dir
    ctx["audit_steps"] = []
    ctx["audit_current_step"] = {}
    print(f"📁 Audit: {session_dir}")

    meta = {
        "task": ctx.get("task", ""),
        "model": ctx.get("model", ""),
        "started_at": datetime.now().isoformat(),
        "screen": f"{ctx['screen_w']}x{ctx['screen_h']}",
    }
    with open(os.path.join(session_dir, "metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)


def on_post_screenshot(ctx, *, img_b64):
    """Save each screenshot as a PNG in the session directory."""
    session_dir = ctx.get("audit_session_dir")
    if not session_dir:
        return

    step = ctx.get("step", 0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    filename = f"step_{step:03d}_{ts}.png"
    path = os.path.join(session_dir, filename)
    with open(path, "wb") as f:
        f.write(base64.b64decode(img_b64))

    # Record screenshot filename in the current step
    current = ctx.get("audit_current_step", {})
    current["screenshot"] = filename
    ctx["audit_current_step"] = current


def on_post_llm_call(ctx, *, messages, reply, usage):
    """Record the LLM reply and usage for the current step."""
    current = ctx.get("audit_current_step", {})
    current["step"] = ctx.get("step", 0)
    current["timestamp"] = datetime.now().isoformat()
    current["llm_reply"] = reply
    current["usage"] = usage
    ctx["audit_current_step"] = current


def on_post_command(ctx, *, command, output):
    """Record the command and its output, then flush the step."""
    session_dir = ctx.get("audit_session_dir")
    if not session_dir:
        return

    current = ctx.get("audit_current_step", {})
    current["command"] = command
    # Truncate very long outputs (e.g. base64 screenshot data)
    current["output"] = output[:2000] if output else ""

    # Append to the steps list
    steps = ctx.get("audit_steps", [])
    steps.append(current)
    ctx["audit_steps"] = steps
    ctx["audit_current_step"] = {}

    # Write trace incrementally so partial results survive crashes
    trace_path = os.path.join(session_dir, "trace.json")
    try:
        with open(trace_path, "w") as f:
            json.dump(steps, f, indent=2)
    except Exception:
        pass


def on_task_complete(ctx, *, summary, result):
    """Record the completion event in the trace."""
    steps = ctx.get("audit_steps", [])
    steps.append({
        "event": "task_complete",
        "timestamp": datetime.now().isoformat(),
        "summary": summary,
        "result": result,
    })
    ctx["audit_steps"] = steps


def on_shutdown(ctx):
    """Finalize metadata and write final trace."""
    session_dir = ctx.get("audit_session_dir")
    if not session_dir:
        return

    # Write final trace
    steps = ctx.get("audit_steps", [])
    trace_path = os.path.join(session_dir, "trace.json")
    try:
        with open(trace_path, "w") as f:
            json.dump(steps, f, indent=2)
    except Exception:
        pass

    # Update metadata
    meta_path = os.path.join(session_dir, "metadata.json")
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        meta = {}

    meta["finished_at"] = datetime.now().isoformat()
    meta["steps"] = ctx.get("step", 0)
    meta["outcome"] = ctx.get("outcome", "unknown")
    meta["wall_time_sec"] = round(ctx.get("wall_time_sec", 0), 2)

    if "usage" in ctx:
        meta["usage"] = ctx["usage"]
    if ctx.get("result") is not None:
        meta["result"] = ctx["result"]

    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
