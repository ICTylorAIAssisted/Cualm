"""Audit plugin — persist screenshots and session metadata.

Creates a timestamped session directory under the configured audit
base path.  Every screenshot is saved as a PNG, and a metadata.json
file tracks task info, timing, and outcome.

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

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = os.path.join(session_dir, f"{ts}.png")
    with open(path, "wb") as f:
        f.write(base64.b64decode(img_b64))


def on_shutdown(ctx):
    """Finalize metadata with outcome, timing, and usage stats."""
    session_dir = ctx.get("audit_session_dir")
    if not session_dir:
        return

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
