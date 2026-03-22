#!/usr/bin/env python3
"""Coverage benchmark — measure how much code the agent exercises.

Launches the CUA agent against a target website with the goal of
maximizing JS+CSS code coverage. Uses CDP to record coverage and
injects coverage snapshots into the agent's prompt so it can
prioritize unexplored areas.

Usage:
  # Against a local dev server
  ./coverage-bench.py --url http://localhost:3000 --max-steps 50

  # With a coverage target
  ./coverage-bench.py --url http://localhost:3000 --target 80

  # Against a running CUA container (attach mode)
  ./coverage-bench.py --attach --url http://localhost:3000

  # Output results as JSON
  ./coverage-bench.py --url http://localhost:3000 --json results.json
"""

import argparse
import json
import os
import subprocess
import sys
import time


COVERAGE_STATE = "/tmp/cua_coverage_state.json"

TASK_TEMPLATE = """\
You are testing a web application at: {url}
Your goal is to maximize code coverage by exploring every feature,
clicking every button, filling every form, opening every menu,
and navigating every page.

Coverage snapshots are taken automatically every few steps and shown
in your context. Use "cua-cdp-coverage report" to find which files
still have low coverage and target those areas.

Strategy:
1. Start by surveying the page — read the a11y tree and explore the UI.
2. Systematically explore: navigation menus, links, buttons, forms,
   dropdowns, tabs, modals, accordions, search, filters.
3. Try to trigger error states, edge cases, empty states too.
4. Fill forms with realistic data and submit them.
5. Test interactive features: sorting, pagination, drag-and-drop,
   date pickers, file uploads, etc.
6. When coverage stops increasing, run: cua-cdp-coverage report
   to find the least-covered files, then target those areas.

When you believe you've maximized coverage or exhausted all reachable
features, use: cua-done "Coverage testing complete" --result "<final_%>"
"""


def start_coverage_recording():
    """Start CDP coverage recording via the tool."""
    result = subprocess.run(
        ["python3", "/app/tools/cua-cdp-coverage", "start"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        print(f"⚠ Failed to start coverage:")
        if result.stdout.strip():
            print(f"  stdout: {result.stdout.strip()}")
        if result.stderr.strip():
            print(f"  stderr: {result.stderr.strip()}")
        return False
    print(f"   {result.stdout.strip()}")
    return True


def get_coverage_snapshot():
    """Take a coverage snapshot and return parsed data."""
    result = subprocess.run(
        ["python3", "/app/tools/cua-cdp-coverage", "snapshot"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return None
    try:
        with open(COVERAGE_STATE) as f:
            return json.load(f)
    except Exception:
        return None


def stop_coverage_recording():
    """Stop recording and return final report."""
    result = subprocess.run(
        ["python3", "/app/tools/cua-cdp-coverage", "stop"],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout if result.returncode == 0 else result.stderr


def get_full_report():
    """Get detailed per-file coverage report."""
    result = subprocess.run(
        ["python3", "/app/tools/cua-cdp-coverage", "report", "--top", "30"],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout if result.returncode == 0 else ""


def main():
    parser = argparse.ArgumentParser(
        description="Coverage benchmark for CUA agent"
    )
    parser.add_argument(
        "--url", required=True,
        help="Target URL to test",
    )
    parser.add_argument(
        "--max-steps", type=int, default=50,
        help="Maximum agent steps (default: 50)",
    )
    parser.add_argument(
        "--target", type=float, default=None,
        help="Target coverage %% — stop early if reached",
    )
    parser.add_argument(
        "--timeout", type=int, default=900,
        help="Overall timeout in seconds (default: 900)",
    )
    parser.add_argument(
        "--json", dest="json_out", default=None,
        help="Write results to JSON file",
    )
    parser.add_argument(
        "--snapshot-interval", type=int, default=1,
        help="Take coverage snapshot every N steps (default: 1)",
    )
    parser.add_argument(
        "--credentials", default=None,
        help="Login credentials as 'user:pass' (injected into task)",
    )
    args = parser.parse_args()

    task = TASK_TEMPLATE.format(url=args.url)
    if args.credentials:
        user, passwd = args.credentials.split(":", 1)
        task += f"\n\nSite credentials (use if login is needed): {user} / {passwd}"
    if args.target:
        task += f"\n\nTarget coverage: {args.target}%. Stop when you reach this."

    print("╔══════════════════════════════════════╗")
    print("║     Coverage Benchmark               ║")
    print("╚══════════════════════════════════════╝")
    print(f"  URL:        {args.url}")
    print(f"  Max steps:  {args.max_steps}")
    print(f"  Timeout:    {args.timeout}s")
    if args.target:
        print(f"  Target:     {args.target}%")
    print()

    # Coverage recording is started by agent.py after calibration +
    # navigation, so the target page's scripts are in the profiler.
    # We just set the env vars to enable it.

    # Set up environment for agent
    os.environ["CUA_MAX_STEPS"] = str(args.max_steps)
    os.environ["CUA_COVERAGE"] = "1"
    os.environ["CUA_COVERAGE_INTERVAL"] = str(args.snapshot_interval)

    # Run the agent
    start_time = time.monotonic()
    coverage_history = []

    print(f"\nLaunching agent with task:")
    print(f"  {task[:100]}...")
    print()

    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            ["python3", "-u", "/app/agent.py", task],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line buffered
            env=env,
        )

        step = 0
        last_logged_js = -1
        for line in proc.stdout:
            line = line.rstrip()
            print(line, flush=True)

            # Track steps — agent.py handles snapshots and injects them
            # into its own prompt.  We just read the state file for
            # history tracking and target checking.
            if line.strip().startswith("── Step"):
                step += 1
                if step % args.snapshot_interval == 0:
                    try:
                        with open(COVERAGE_STATE) as f:
                            snap = json.load(f)
                    except Exception:
                        snap = None
                    if snap:
                        elapsed = time.monotonic() - start_time
                        js_pct = snap.get("last_js_pct", 0)
                        css_pct = snap.get("last_css_pct", 0)
                        if js_pct != last_logged_js:
                            coverage_history.append({
                                "step": step,
                                "elapsed_sec": round(elapsed, 1),
                                "js_pct": js_pct,
                                "css_pct": css_pct,
                            })
                            last_logged_js = js_pct

                        # Check target
                        if args.target and js_pct >= args.target:
                            print(f"\n🎯 Target coverage {args.target}% reached!")
                            proc.terminate()
                            break

            # Check timeout
            if time.monotonic() - start_time > args.timeout:
                print(f"\n⏰ Timeout ({args.timeout}s) reached")
                proc.terminate()
                break

        proc.wait(timeout=10)
    except KeyboardInterrupt:
        print("\n⚠ Interrupted")
        proc.terminate()
    except Exception as e:
        print(f"\n⚠ Error: {e}")

    elapsed = time.monotonic() - start_time

    # Final report
    print("\n" + "=" * 60)
    print("FINAL COVERAGE REPORT")
    print("=" * 60)
    final_report = stop_coverage_recording()
    print(final_report)

    # Get final numbers from state file (written by agent's last snapshot)
    try:
        with open(COVERAGE_STATE) as f:
            final_snap = json.load(f)
    except Exception:
        final_snap = {}
    js_final = final_snap.get("last_js_pct", 0)
    css_final = final_snap.get("last_css_pct", 0)

    results = {
        "url": args.url,
        "max_steps": args.max_steps,
        "steps_completed": step,
        "elapsed_sec": round(elapsed, 1),
        "js_coverage_pct": js_final,
        "css_coverage_pct": css_final,
        "target_pct": args.target,
        "target_reached": args.target and js_final >= args.target,
        "coverage_history": coverage_history,
    }

    print(f"\n{'─' * 40}")
    print(f"  Steps:        {step}")
    print(f"  Time:         {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"  JS Coverage:  {js_final}%")
    print(f"  CSS Coverage: {css_final}%")
    if args.target:
        status = "✅ REACHED" if js_final >= args.target else "❌ NOT REACHED"
        print(f"  Target:       {args.target}% → {status}")
    print(f"{'─' * 40}")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults written to {args.json_out}")

    return 0 if (not args.target or js_final >= args.target) else 1


if __name__ == "__main__":
    sys.exit(main())
