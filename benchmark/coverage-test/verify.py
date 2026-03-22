#!/usr/bin/env python3
"""Verify coverage tracking with a simple test page.

Run inside the agent container after start.sh has launched Chromium:

  python3 -u /app/benchmark/coverage-test/verify.py

Expected behavior:
  - After load:    ~20-30% (top-level code + log helper)
  - After click A: increases
  - After click B: increases more
  - After click C: increases more
  - After click D: ~100%
  - Coverage should NEVER decrease between steps.
"""

import json
import subprocess
import sys
import time
import urllib.request


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
    return r.stdout.strip(), r.returncode


def cdp_js(expr):
    out, rc = run(f'python3 /app/tools/cua-cdp-js "{expr}"')
    return out


def coverage(cmd):
    out, rc = run(f"python3 /app/tools/cua-cdp-coverage {cmd}")
    return out


def main():
    # Navigate to test page
    print("=== Coverage Verification Test ===\n")

    # Start a simple HTTP server for the test page
    import http.server
    import threading
    import os

    test_dir = os.path.dirname(os.path.abspath(__file__))

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=test_dir, **kwargs)
        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 8765), QuietHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print("Test server on http://127.0.0.1:8765")

    # Start coverage BEFORE navigating (so scripts load under profiler)
    print("\n1. Starting coverage daemon...")
    print(f"   {coverage('start')}")

    # Navigate
    print("\n2. Navigating to test page...")
    cdp_js("location.href='http://127.0.0.1:8765/index.html'")
    time.sleep(2)

    title = cdp_js("document.title")
    print(f"   Page title: {title}")

    # Snapshot after load
    print("\n3. Snapshot after page load (expect ~20-30%):")
    snap = coverage("snapshot")
    print(f"   {snap}")

    prev_pct = 0
    errors = []

    # Parse JS pct from output
    def parse_js_pct(text):
        for line in text.splitlines():
            if "JS:" in line:
                return float(line.split("%")[0].split()[-1])
        return 0

    load_pct = parse_js_pct(snap)
    print(f"   JS: {load_pct}%")
    if load_pct <= 0:
        errors.append(f"FAIL: Load coverage is {load_pct}%, expected > 0%")
    prev_pct = load_pct

    # Click each button and snapshot
    for btn in ["A", "B", "C", "D"]:
        print(f"\n4{btn}. Clicking button {btn}...")
        cdp_js(f"func{btn}()")
        time.sleep(0.5)

        snap = coverage("snapshot")
        pct = parse_js_pct(snap)
        print(f"   {snap}")
        print(f"   JS: {pct}% (was {prev_pct}%)")

        if pct < prev_pct:
            errors.append(f"FAIL: Coverage DECREASED after clicking {btn}: {prev_pct}% -> {pct}%")
        elif pct == prev_pct:
            errors.append(f"WARN: Coverage didn't increase after clicking {btn}: still {pct}%")

        prev_pct = pct

    # Final report
    print(f"\n5. Final report:")
    report = coverage("report")
    print(f"   {report}")

    # Stop
    print(f"\n6. Stop:")
    stop = coverage("stop")
    print(f"   {stop}")

    # Summary
    print("\n" + "=" * 50)
    if errors:
        for e in errors:
            print(f"  ❌ {e}")
        print(f"\n  {len(errors)} issue(s) found")
        return 1
    else:
        print("  ✅ All checks passed — coverage monotonically increased")
        print(f"  Final: {prev_pct}%")
        return 0


if __name__ == "__main__":
    sys.exit(main())
