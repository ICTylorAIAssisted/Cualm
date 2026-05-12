#!/usr/bin/env python3
"""Create a timelapse video from audit screenshots.

Compiles step screenshots into a video with step number and command
overlaid as captions. More reliable than VNC recording.

Prerequisites:
  pip install Pillow
  apt install ffmpeg

Usage:
  python3 demo/screenshots_to_video.py benchmark/results/0/
  python3 demo/screenshots_to_video.py benchmark/results/0/ -o demo.mp4 --fps 0.5
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Install Pillow: pip install Pillow", file=sys.stderr)
    sys.exit(1)


def find_session(task_dir: Path) -> Path | None:
    """Find the audit session directory."""
    for parent in [task_dir, task_dir / "audit"]:
        sessions = sorted(parent.glob("session_*"))
        if sessions:
            return sessions[-1]
    if (task_dir / "trace.json").exists():
        return task_dir
    return None


def load_trace(session_dir: Path) -> list[dict]:
    """Load trace.json."""
    trace_path = session_dir / "trace.json"
    if trace_path.exists():
        with open(trace_path) as f:
            return json.load(f)
    return []


def annotate_screenshot(png_path: Path, step_num: int, command: str,
                       output_path: Path) -> None:
    """Add step number and command caption to a screenshot."""
    img = Image.open(png_path)
    draw = ImageDraw.Draw(img)

    # Use a basic font (monospace if available)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 14)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 12)
    except (OSError, IOError):
        font = ImageFont.load_default()
        font_small = font

    w, h = img.size

    # Step badge (top-left)
    badge_text = f" Step {step_num} "
    bbox = draw.textbbox((0, 0), badge_text, font=font)
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.rectangle([8, 8, 8 + bw + 8, 8 + bh + 8], fill=(220, 50, 50, 200))
    draw.text((12, 10), badge_text, fill="white", font=font)

    # Command bar (bottom)
    if command:
        cmd_display = command[:120]
        bar_h = 28
        draw.rectangle([0, h - bar_h, w, h], fill=(0, 0, 0, 200))
        draw.text((8, h - bar_h + 6), f"run: {cmd_display}", fill="#5b9cf5",
                 font=font_small)

    img.save(output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Create timelapse video from audit screenshots")
    parser.add_argument("task_dir", help="Task results directory")
    parser.add_argument("-o", "--output", default=None,
                       help="Output video path (default: <task_dir>/demo.mp4)")
    parser.add_argument("--fps", type=float, default=0.5,
                       help="Frames per second (default: 0.5 = 2s per step)")
    parser.add_argument("--pause-last", type=int, default=4,
                       help="Extra seconds to hold on the last frame")
    args = parser.parse_args()

    task_dir = Path(args.task_dir)
    session = find_session(task_dir)
    if not session:
        print(f"No audit session found in {task_dir}", file=sys.stderr)
        sys.exit(1)

    trace = load_trace(session)
    if not trace:
        print(f"Empty trace in {session}", file=sys.stderr)
        sys.exit(1)

    # Collect screenshots with their step info
    frames = []
    for step in trace:
        if step.get("event") == "task_complete":
            continue
        png_name = step.get("screenshot", "")
        if not png_name:
            continue
        png_path = session / png_name
        if not png_path.exists():
            continue
        frames.append({
            "png": png_path,
            "step": step.get("step", 0),
            "command": step.get("command", ""),
        })

    if not frames:
        print("No screenshots found", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(frames)} screenshots")

    # Create annotated frames in a temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, frame in enumerate(frames):
            out_path = Path(tmpdir) / f"frame_{i:04d}.png"
            annotate_screenshot(frame["png"], frame["step"],
                              frame["command"], out_path)
            print(f"  Frame {i + 1}/{len(frames)}: Step {frame['step']}")

        # Duplicate last frame for pause effect
        last_frame = Path(tmpdir) / f"frame_{len(frames) - 1:04d}.png"
        extra_frames = int(args.pause_last * args.fps)
        for j in range(extra_frames):
            dup_path = Path(tmpdir) / f"frame_{len(frames) + j:04d}.png"
            os.link(last_frame, dup_path)

        # Compile to video with ffmpeg
        output = args.output or str(task_dir / "demo.mp4")
        print(f"\nCompiling video at {args.fps} fps → {output}")

        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(args.fps),
            "-i", f"{tmpdir}/frame_%04d.png",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            output,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"ffmpeg error: {result.stderr}", file=sys.stderr)
            sys.exit(1)

        print(f"✓ {output}")
        size = os.path.getsize(output)
        duration = (len(frames) + extra_frames) / args.fps
        print(f"  {len(frames)} steps, {duration:.0f}s, "
              f"{size / 1024:.0f}KB")


if __name__ == "__main__":
    main()
