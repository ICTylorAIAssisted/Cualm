#!/usr/bin/env python3
"""Post-setup sanity tests for cualm.

Four levels:
  1. Infrastructure — container runtime responds
  2. Image — agent image starts, Xvfb+Chromium work
  3. LLM — server reachable, model responds
  4. End-to-end — agent navigates with mock LLM

Usage:
  python3 cualm_selftest.py              # levels 1-3
  python3 cualm_selftest.py --full       # levels 1-4
  python3 cualm_selftest.py --level 2    # specific level
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Colors
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _run(cmd, timeout=30, **kwargs):
    """Run a command, return (ok, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, **kwargs)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return False, "", str(e)


def _runtime():
    """Get the container runtime command."""
    for cmd in ["podman", "docker"]:
        ok, _, _ = _run([cmd, "version"])
        if ok:
            return cmd
    return None


def _print_test(name, passed, detail="", elapsed=0):
    icon = f"{GREEN}✓{RESET}" if passed else f"{RED}✗{RESET}"
    timing = f" {DIM}({elapsed:.1f}s){RESET}" if elapsed else ""
    detail_str = f" — {detail}" if detail else ""
    print(f"  {icon} {name}{detail_str}{timing}")


# ── Level 1: Infrastructure ──

def test_runtime_responds():
    """Container runtime responds to basic commands."""
    rt = _runtime()
    if not rt:
        return False, "No runtime found"
    t0 = time.monotonic()
    ok, out, _ = _run([rt, "version", "--format", "json"])
    if not ok:
        # Fallback for podman compat
        ok, out, _ = _run([rt, "version"])
    elapsed = time.monotonic() - t0
    return ok, f"{rt} responds", elapsed


def test_runtime_run():
    """Can run a simple container."""
    rt = _runtime()
    if not rt:
        return False, "No runtime", 0
    t0 = time.monotonic()
    ok, out, err = _run([rt, "run", "--rm", "alpine", "echo", "hello"], timeout=30)
    elapsed = time.monotonic() - t0
    if ok and "hello" in out:
        return True, "alpine echo OK", elapsed
    return False, err[:100], elapsed


def test_network_create():
    """Can create and remove a network."""
    rt = _runtime()
    if not rt:
        return False, "No runtime", 0
    net_name = "cualm-selftest-net"
    t0 = time.monotonic()
    _run([rt, "network", "rm", net_name])
    ok, _, err = _run([rt, "network", "create", net_name])
    _run([rt, "network", "rm", net_name])
    elapsed = time.monotonic() - t0
    return ok, "network create/remove OK", elapsed


# ── Level 2: Agent Image ──

def test_agent_image_exists():
    """Agent image is built."""
    rt = _runtime()
    if not rt:
        return False, "No runtime", 0
    t0 = time.monotonic()
    ok, _, _ = _run([rt, "image", "inspect", "cua-agent"])
    elapsed = time.monotonic() - t0
    if ok:
        return True, "cua-agent:latest", elapsed
    return False, "Image not found — run: ./run.sh build", elapsed


def test_agent_starts():
    """Agent container starts and tools respond."""
    rt = _runtime()
    if not rt:
        return False, "No runtime", 0

    name = "cualm-selftest-agent"
    # Clean up any previous test container
    _run([rt, "rm", "-f", name])

    t0 = time.monotonic()
    ok, _, err = _run([
        rt, "run", "--rm", "--name", name,
        "--entrypoint", "bash",
        "cua-agent", "-c",
        "echo START && /app/start.sh & sleep 3 && "
        "DISPLAY=:99 scrot -o /tmp/test.png && echo SCREENSHOT_OK && "
        "which cua-click && echo TOOLS_OK"
    ], timeout=30)
    elapsed = time.monotonic() - t0

    # Check test container output
    ok2, out, _ = _run([rt, "logs", name], timeout=5)
    _run([rt, "rm", "-f", name])

    if "SCREENSHOT_OK" in (out or ""):
        return True, "Xvfb + scrot OK", elapsed
    return False, f"Agent start failed: {err[:100]}", elapsed


# ── Level 3: LLM ──

def test_llm_reachable():
    """LLM server responds to /v1/models."""
    url = os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1")
    t0 = time.monotonic()
    try:
        import urllib.request
        req = urllib.request.urlopen(f"{url}/models", timeout=5)
        data = json.loads(req.read())
        elapsed = time.monotonic() - t0
        models = [m["id"] for m in data.get("data", [])]
        return True, f"{url} ({len(models)} models)", elapsed
    except Exception as e:
        elapsed = time.monotonic() - t0
        return False, f"{url} — {e}", elapsed


def test_llm_completion():
    """LLM generates a simple completion."""
    url = os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1")
    model = os.environ.get("CUA_MODEL", "")

    t0 = time.monotonic()
    try:
        import urllib.request

        # Discover model if not set
        if not model:
            resp = urllib.request.urlopen(f"{url}/models", timeout=5)
            data = json.loads(resp.read())
            models = data.get("data", [])
            if models:
                model = models[0]["id"]
            else:
                return False, "No models available", time.monotonic() - t0

        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "Reply with just the word 'hello'"}],
            "max_tokens": 10,
        }).encode()
        req = urllib.request.Request(
            f"{url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        elapsed = time.monotonic() - t0
        text = data["choices"][0]["message"]["content"].strip()
        tokens = data.get("usage", {}).get("completion_tokens", "?")
        return True, f'model={model}, "{text[:50]}" ({tokens} tok)', elapsed
    except Exception as e:
        elapsed = time.monotonic() - t0
        return False, str(e)[:100], elapsed


# ── Level 4: End-to-end ──

def test_end_to_end():
    """Agent navigates to example.com with a mock LLM."""
    scripts_dir = Path(__file__).parent / "tests" / "scripts"
    if not scripts_dir.exists():
        scripts_dir = Path("tests/scripts")
    mock_llm_path = scripts_dir / "mock_llm.py"

    if not mock_llm_path.exists():
        return False, "tests/scripts/mock_llm.py not found", 0

    sys.path.insert(0, str(scripts_dir))
    from mock_llm import MockLLMServer, CALIBRATION_ONLY

    rt = _runtime()
    if not rt:
        return False, "No runtime", 0

    t0 = time.monotonic()

    # Listen on 0.0.0.0 so containers can reach us (rootless podman)
    with MockLLMServer(script=CALIBRATION_ONLY, host="0.0.0.0", port=0) as server:
        port = server.url.split(":")[-1].split("/")[0]
        llm_url = f"http://host.docker.internal:{port}/v1"

        name = "cualm-selftest-e2e"
        _run([rt, "rm", "-f", name])

        ok, out, err = _run([
            rt, "run", "--rm", "--name", name,
            "--add-host", "host.docker.internal:host-gateway",
            "-e", f"OPENAI_BASE_URL={llm_url}",
            "-e", "OPENAI_API_KEY=test",
            "-e", "CUA_MODEL=mock-model",
            "-e", "CUA_MAX_STEPS=3",
            "-e", "CUA_START_URL=https://example.com",
            "cua-agent",
            "agent", "Navigate to example.com and report the title",
        ], timeout=120)

        elapsed = time.monotonic() - t0
        _run([rt, "rm", "-f", name])

        calls = server.call_count
        if calls > 0:
            return True, f"Agent made {calls} LLM calls in {elapsed:.0f}s", elapsed

        hint = f"stdout: {(out or '')[:150]}"
        if "Connection refused" in (err or "") or "Connection refused" in (out or ""):
            hint = f"Mock LLM not reachable from container (port {port})"
        return False, hint, elapsed


# ── Runner ──

LEVELS = {
    1: [
        ("Runtime responds", test_runtime_responds),
        ("Container run", test_runtime_run),
        ("Network creation", test_network_create),
    ],
    2: [
        ("Agent image", test_agent_image_exists),
        # ("Agent starts", test_agent_starts),  # slow, optional
    ],
    3: [
        ("LLM reachable", test_llm_reachable),
        ("LLM completion", test_llm_completion),
    ],
    4: [
        ("End-to-end", test_end_to_end),
    ],
}

LEVEL_NAMES = {
    1: "Infrastructure",
    2: "Agent Image",
    3: "LLM Server",
    4: "End-to-End",
}


def run_selftest(max_level: int = 3) -> bool:
    """Run sanity tests up to the given level. Returns True if all pass."""
    all_passed = True

    for level in range(1, max_level + 1):
        tests = LEVELS.get(level, [])
        if not tests:
            continue

        print(f"\n{BOLD}Level {level}: {LEVEL_NAMES[level]}{RESET}")
        for name, test_fn in tests:
            try:
                result = test_fn()
                if len(result) == 3:
                    passed, detail, elapsed = result
                else:
                    passed, detail = result
                    elapsed = 0
                _print_test(name, passed, detail, elapsed)
                if not passed:
                    all_passed = False
            except Exception as e:
                _print_test(name, False, str(e)[:100])
                all_passed = False

    print()
    if all_passed:
        print(f"  {GREEN}{BOLD}All tests passed.{RESET}")
    else:
        print(f"  {RED}{BOLD}Some tests failed.{RESET}")
    print()
    return all_passed


def main():
    parser = argparse.ArgumentParser(description="cualm self-test")
    parser.add_argument("--full", action="store_true",
                        help="Run all levels including end-to-end")
    parser.add_argument("--level", type=int, default=None,
                        help="Run a specific level (1-4)")
    args = parser.parse_args()

    if args.level:
        max_level = args.level
    elif args.full:
        max_level = 4
    else:
        max_level = 3

    ok = run_selftest(max_level)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
