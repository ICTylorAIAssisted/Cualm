#!/usr/bin/env python3
"""Record a video of scrolling through a trace viewer HTML file.

Uses Playwright to open the HTML, scroll through each step,
open a couple of thinking sections, and record the session.

Usage:
  python3 demo/record_trace.py trace.html output.mp4
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    if len(sys.argv) < 3:
        print("Usage: record_trace.py <trace.html> <output.mp4>")
        sys.exit(1)

    trace_html = Path(sys.argv[1]).resolve()
    output_video = sys.argv[2]

    if not trace_html.exists():
        print(f"File not found: {trace_html}", file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmpdir:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                record_video_dir=tmpdir,
                record_video_size={"width": 1280, "height": 720},
            )
            page = context.new_page()
            page.goto(f"file://{trace_html}")
            page.wait_for_load_state("networkidle")

            # Pause on the header / eval panel
            time.sleep(3)

            # Scroll through each step
            steps = page.query_selector_all(".step[id]")
            for i, step in enumerate(steps):
                step.scroll_into_view_if_needed()
                time.sleep(1.2)

                # Open Thinking on first 2 steps to show reasoning
                if i < 2:
                    summary = step.query_selector("details summary")
                    if summary:
                        summary.click()
                        time.sleep(2)
                        # Close it again
                        summary.click()
                        time.sleep(0.5)

            # Scroll back to top to show eval result
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(3)

            # Close — video is saved automatically
            video_path = page.video.path()
            context.close()
            browser.close()

        # Copy video to output
        shutil.copy(video_path, output_video)
        print(f"✓ {output_video}")


if __name__ == "__main__":
    main()
