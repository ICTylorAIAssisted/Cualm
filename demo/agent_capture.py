#!/usr/bin/env python3
"""Capture screenshots from an orchestrator-spawned agent container.

Polls docker for a container matching the task pattern, then
periodically captures screenshots via `docker exec scrot`.

Usage:
  python3 demo/agent_capture.py --output ./screenshots --interval 3
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


def find_task_container():
    """Find the orchestrator's spawned agent container."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
        for name in result.stdout.strip().split("\n"):
            # Orchestrator names containers like "task-1-..."
            if name.startswith("task-") or name.startswith("cua-task-"):
                return name
    except Exception:
        pass
    return None


def capture_screenshot(container, output_path):
    """Take a screenshot inside the container and copy it out."""
    tmp = "/tmp/_demo_capture.png"
    try:
        # Take screenshot inside container
        result = subprocess.run(
            ["docker", "exec", container, "bash", "-c",
             f"DISPLAY=:99 scrot -o {tmp} 2>/dev/null && echo OK"],
            capture_output=True, text=True, timeout=10,
        )
        if "OK" not in result.stdout:
            return False

        # Copy it out
        result = subprocess.run(
            ["docker", "cp", f"{container}:{tmp}", str(output_path)],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Agent screenshot capture")
    parser.add_argument("--output", "-o", default="./screenshots",
                        help="Output directory")
    parser.add_argument("--interval", "-i", type=float, default=3.0,
                        help="Seconds between captures (default: 3)")
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    running = True
    def handle_signal(sig, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"Agent capture: watching for task containers → {out_dir}/")
    print(f"  Interval: {args.interval}s")

    count = 0
    current_container = None
    waiting_printed = False

    while running:
        container = find_task_container()

        if container is None:
            if current_container:
                # Container stopped
                print(f"  Container {current_container} stopped")
                current_container = None
                # Keep running in case a new task starts
            if not waiting_printed:
                print("  Waiting for task container...")
                waiting_printed = True
            time.sleep(1)
            continue

        if container != current_container:
            current_container = container
            waiting_printed = False
            print(f"  Found: {container}")

        count += 1
        path = out_dir / f"step_{count:04d}.png"
        if capture_screenshot(container, path):
            size = path.stat().st_size
            print(f"  [{count}] {path.name} ({size // 1024}KB)")
        else:
            count -= 1  # don't count failed captures

        time.sleep(args.interval)

    print(f"\nCaptured {count} screenshots → {out_dir}/")


if __name__ == "__main__":
    main()
