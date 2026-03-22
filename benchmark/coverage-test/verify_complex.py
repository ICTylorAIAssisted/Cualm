#!/usr/bin/env python3
"""Verify coverage tracking with complex edge cases.

Tests: nested functions, conditional branches, callbacks, closures,
IIFE, try/catch, switch, and dead code.

Run inside the agent container:
  python3 -u /app/benchmark/coverage-test/verify_complex.py
"""

import json
import os
import subprocess
import sys
import time


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
    return r.stdout.strip(), r.returncode


def cdp_js(expr):
    out, _ = run(f"python3 /app/tools/cua-cdp-js \"{expr}\"")
    return out


def coverage(cmd):
    out, _ = run(f"python3 /app/tools/cua-cdp-coverage {cmd}")
    return out


def parse_js(text):
    """Parse JS coverage line → (pct, used, total)."""
    for line in text.splitlines():
        if "JS:" in line:
            parts = line.split()
            pct = float(parts[1].rstrip("%"))
            # parse "(used/total bytes, ...)"
            bp = line.split("(")[1].split(")")[0]  # "585/1,022 bytes, 1 files"
            nums = bp.split("bytes")[0].strip()     # "585/1,022"
            used_s, total_s = nums.split("/")
            used = int(used_s.replace(",", ""))
            total = int(total_s.replace(",", ""))
            return pct, used, total
    return 0, 0, 0


def main():
    print("=== Complex Coverage Verification ===\n")

    # Start HTTP server
    import http.server, threading
    test_dir = os.path.dirname(os.path.abspath(__file__))

    class Q(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=test_dir, **kw)
        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 8766), Q)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    file_size = os.path.getsize(os.path.join(test_dir, "complex.js"))
    print(f"  complex.js = {file_size} bytes")

    # Start coverage
    print("\n1. Start coverage daemon")
    print(f"   {coverage('start')}")

    # Navigate
    print("\n2. Navigate to complex.html")
    cdp_js("location.href='http://127.0.0.1:8766/complex.html'")
    time.sleep(2)
    print(f"   Title: {cdp_js('document.title')}")

    errors = []
    prev_pct = 0.0
    prev_used = 0
    step = 0

    def snap(label, expect_increase=True, expect_lt_100=None):
        nonlocal prev_pct, prev_used, step
        step += 1
        text = coverage("snapshot")
        pct, used, total = parse_js(text)
        delta = used - prev_used
        indicator = "↑" if delta > 0 else ("=" if delta == 0 else "↓")

        print(f"\n   [{step}] {label}")
        print(f"       JS: {pct}%  ({used:,}/{total:,} bytes)  {indicator}{abs(delta):+,}")

        if total != file_size:
            errors.append(f"[{step}] {label}: total={total}, expected={file_size}")

        if pct < prev_pct:
            errors.append(f"[{step}] {label}: DECREASED {prev_pct}% → {pct}%")
        elif expect_increase and pct == prev_pct and step > 1:
            errors.append(f"[{step}] {label}: expected increase, stayed at {pct}%")

        if expect_lt_100 is True and pct >= 100:
            errors.append(f"[{step}] {label}: expected < 100%, got {pct}%")
        if expect_lt_100 is False and pct < 100:
            errors.append(f"[{step}] {label}: expected 100%, got {pct}%")

        prev_pct = pct
        prev_used = used

    # ── Step-by-step testing ──

    print("\n── After page load ──")
    snap("Page load (top-level + IIFE)", expect_increase=True, expect_lt_100=True)

    print("\n── Nested functions ──")
    cdp_js("outerA()")
    time.sleep(0.3)
    snap("outerA() — calls innerA1 + innerA2", expect_lt_100=True)

    print("\n── Conditional: TRUE branch only ──")
    cdp_js("branchB(true)")
    time.sleep(0.3)
    snap("branchB(true) — FALSE branch still uncovered", expect_lt_100=True)

    print("\n── Conditional: FALSE branch ──")
    cdp_js("branchB(false)")
    time.sleep(0.3)
    snap("branchB(false) — both branches now covered", expect_lt_100=True)

    print("\n── Callback / higher-order ──")
    cdp_js("withCallback([1,2,3], function(x){return x*2})")
    time.sleep(0.3)
    snap("withCallback with inline cb", expect_lt_100=True)

    print("\n── Closure / factory ──")
    cdp_js("var c=makeCounter(0); c.increment(); c.increment(); c.decrement(); c.getCount()")
    time.sleep(0.3)
    snap("makeCounter — all 3 inner fns called", expect_lt_100=True)

    print("\n── Try/catch: success path ──")
    cdp_js("riskyE(false)")
    time.sleep(0.3)
    snap("riskyE(false) — catch block still uncovered", expect_lt_100=True)

    print("\n── Try/catch: error path ──")
    cdp_js("riskyE(true)")
    time.sleep(0.3)
    snap("riskyE(true) — catch block now covered", expect_lt_100=True)

    print("\n── Switch: all cases ──")
    cdp_js("switchF('a'); switchF('b'); switchF('c'); switchF('z')")
    time.sleep(0.3)
    snap("switchF all cases + default", expect_lt_100=True)
    # Dead code should keep us below 100%

    print("\n── Verify dead code stays dead ──")
    final_pct = prev_pct
    print(f"   Final: {final_pct}% (dead code = {100 - final_pct:.1f}% uncovered)")
    if final_pct >= 100:
        errors.append(f"Final is {final_pct}% but dead code should keep it < 100%")

    # Final report
    print(f"\n── Full report ──")
    print(f"   {coverage('report')}")

    print(f"\n── Stop ──")
    print(f"   {coverage('stop')}")

    # Summary
    print("\n" + "=" * 55)
    if errors:
        for e in errors:
            print(f"  ❌ {e}")
        print(f"\n  {len(errors)} issue(s)")
        return 1
    else:
        print("  ✅ All checks passed")
        print(f"     - Coverage monotonically increased at each step")
        print(f"     - Total bytes = file size ({file_size}) throughout")
        print(f"     - Dead code stayed uncovered ({100 - final_pct:.1f}%)")
        print(f"     - Final: {final_pct}%")
        return 0


if __name__ == "__main__":
    sys.exit(main())
