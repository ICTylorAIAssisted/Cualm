#!/usr/bin/env python3
"""WebArena benchmark runner for the CUA agent.

Spawns one agent container per task against self-hosted WebArena
sites.  Collects results and evaluates using either the built-in
string_match evaluator (Phase 1) or WebArena-Verified's deterministic
offline evaluator (Phase 3).

Usage:
    # Phase 1 — string_match only
    python benchmark/run.py --tasks 0-50 --site shopping

    # Phase 3 — full WA-Verified with HAR capture
    python benchmark/run.py --tasks 0-50 --webarena-verified

    # HAR capture only (for later offline evaluation)
    python benchmark/run.py --tasks 0-50 --har-capture
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import signal
import sys
import tarfile
import time
from pathlib import Path

import docker

# Ensure the benchmark package is importable when invoked as a script
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    DEFAULT_AGENT_IMAGE,
    DEFAULT_CALIBRATION_VOLUME,
    DEFAULT_MAX_STEPS,
    DEFAULT_NETWORK,
    DEFAULT_TIMEOUT,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    PHASE1_SITES,
    SITE_CREDENTIALS,
    SITE_PORT_FORWARDS,
    URL_TEMPLATES,
)
from evaluate import evaluate_task, evaluate_task_verified

# ── Constants ─────────────────────────────────────────────────────

DONE_MARKER = "@@CUA_TASK_COMPLETE@@"

# Prefixes used for benchmark containers — cleanup finds these
BENCH_PREFIXES = ("bench-task-", "bench-proxy-", "bench-gateway")


# ── Cleanup ───────────────────────────────────────────────────────


def cleanup_benchmark(client: docker.DockerClient | None = None) -> None:
    """Remove all leftover benchmark containers and networks.

    Safe to call at any time — finds containers by name prefix.
    """
    if client is None:
        client = docker.from_env()

    # Kill and remove all bench-* containers
    for c in client.containers.list(all=True):
        if any(c.name.startswith(p) for p in BENCH_PREFIXES):
            print(f"  Removing container: {c.name}")
            try:
                c.remove(force=True)
            except Exception:
                pass

    # Remove the ext network
    from gateway import _cleanup_ext_network
    _cleanup_ext_network(client)
    print("  Benchmark cleanup complete.")


# ── Task loading & filtering ──────────────────────────────────────


def load_tasks(task_file: str, args: argparse.Namespace) -> list[dict]:
    """Load WebArena tasks from JSON and apply filters."""
    path = Path(task_file)
    if not path.exists():
        print(f"Error: task file not found: {path}", file=sys.stderr)
        print(
            "Download it from the WebArena repo:\n"
            "  curl -L -o benchmark/tasks/test.raw.json \\\n"
            "    https://raw.githubusercontent.com/web-arena-x/"
            "webarena/main/config_files/test.raw.json\n"
            "Or use the WebArena-Verified dataset:\n"
            "  webarena-verified dataset-get --output benchmark/tasks/test.raw.json",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(path) as f:
        all_tasks = json.load(f)

    # Filter by site
    if args.site:
        sites = {s.strip() for s in args.site.split(",")}
        all_tasks = [
            t for t in all_tasks
            if any(s in sites for s in t.get("sites", []))
        ]

    # Filter to Phase 1 sites only (unless --all-sites is passed)
    if not getattr(args, "all_sites", False):
        all_tasks = [
            t for t in all_tasks
            if any(s in PHASE1_SITES for s in t.get("sites", []))
        ]

    # Filter by eval type
    if args.eval_type:
        eval_types = {e.strip() for e in args.eval_type.split(",")}
        all_tasks = [
            t for t in all_tasks
            if any(
                et in eval_types
                for et in t.get("eval", {}).get("eval_types", [])
            )
        ]

    # Filter by task spec: "all", "0-50", "108", "108,200,305"
    tasks_spec = args.tasks
    if tasks_spec != "all":
        if "-" in tasks_spec and "," not in tasks_spec:
            start, end = tasks_spec.split("-", 1)
            task_ids = set(range(int(start), int(end) + 1))
        else:
            task_ids = {int(x) for x in tasks_spec.split(",")}
        all_tasks = [t for t in all_tasks if t["task_id"] in task_ids]

    return all_tasks


# ── URL resolution ────────────────────────────────────────────────


def resolve_urls(task: dict) -> dict:
    """Replace __SHOPPING__ etc. in start_url and intent."""
    task = dict(task)
    start_url = task.get("start_url", "")
    intent = task.get("intent", "")

    for template, replacement in URL_TEMPLATES.items():
        start_url = start_url.replace(template, replacement)
        intent = intent.replace(template, replacement)

    task["resolved_start_url"] = start_url
    task["resolved_intent"] = intent
    return task


# ── Instruction builder ──────────────────────────────────────────


def build_instruction(task: dict) -> str:
    """Build the task instruction including login hints and start URL."""
    intent = task["resolved_intent"]
    start_url = task.get("resolved_start_url", "")
    sites = task.get("sites", [])
    needs_login = task.get("require_login", False)

    parts = [
        "You are completing a web task on a self-hosted website.",
        f"The browser is already open at: {start_url}",
        f"The task is: {intent}",
        "",
        "IMPORTANT: This is a self-hosted environment. All websites are "
        "accessed via internal hostnames (shopping, shopping_admin, reddit, "
        "gitlab, homepage). Do NOT navigate to google.com or any external "
        "website — they are not accessible. Stay on the self-hosted sites.",
        "",
        "When you find the answer to the task, use cua-done with "
        "--result to return it.",
        'For example: cua-done "Found the price" --result "$42.50"',
        "",
        "If the task asks you to perform an action (create, delete, "
        "modify), complete the action and then use cua-done to confirm.",
        "",
        "Do not call cua-screenshot directly — screenshots are "
        "captured automatically.",
    ]

    if needs_login:
        parts.append("")
        parts.append("Site credentials (use if login is needed):")
        for site in sites:
            creds = SITE_CREDENTIALS.get(site)
            if creds:
                parts.append(
                    f"  {site}: {creds['username']} / {creds['password']}"
                )

    return "\n".join(parts)


# ── Docker helpers ────────────────────────────────────────────────


def read_done_payload(container) -> dict:
    """Read /tmp/cua_done.json from a stopped container."""
    try:
        stream, _ = container.get_archive("/tmp/cua_done.json")
        tar_bytes = b"".join(stream)
        tar = tarfile.open(fileobj=io.BytesIO(tar_bytes))
        member = tar.getmembers()[0]
        f = tar.extractfile(member)
        if f:
            return json.loads(f.read().decode())
    except Exception:
        pass
    return {}


def extract_audit(container, task_id: int, output_dir: Path) -> Path | None:
    """Extract the /app/audit/ directory from a stopped container.

    Saves the audit session (screenshots + metadata.json) to
    ``output_dir/{task_id}/audit/``.  Returns the path, or None
    on failure.
    """
    try:
        stream, _ = container.get_archive("/app/audit/")
        tar_bytes = b"".join(stream)
        tar = tarfile.open(fileobj=io.BytesIO(tar_bytes))

        dest = output_dir / str(task_id)
        dest.mkdir(parents=True, exist_ok=True)
        tar.extractall(path=str(dest))

        audit_path = dest / "audit"
        if audit_path.exists():
            return audit_path
    except Exception:
        pass
    return None


# ── Agent spawner ─────────────────────────────────────────────────


def spawn_agent(
    client: docker.DockerClient,
    task: dict,
    args: argparse.Namespace,
    gateway_ip: str | None = None,
    active_containers: list | None = None,
) -> dict:
    """Spawn one agent container, wait for completion, return result.

    When ``args.har_capture`` is True, also spawns a companion
    mitmproxy container and routes agent traffic through it.

    The agent reaches WebArena sites and the LLM via
    ``host.docker.internal``, which is mapped to the nginx gateway
    when network isolation is active, or to the host directly.
    """
    task_id = task["task_id"]
    name = f"bench-task-{task_id}-{int(time.time())}"

    # Determine where host.docker.internal should point
    host_target = gateway_ip or "host-gateway"

    # Build port forward spec for socat in start.sh
    # Format: "7770:shopping:80,7780:shopping_admin:80,..."
    port_forwards = ",".join(
        f"{lport}:{host}:{rport}"
        for lport, (host, rport) in sorted(SITE_PORT_FORWARDS.items())
    )

    env = {
        "OPENAI_BASE_URL": args.llm_base_url,
        "OPENAI_API_KEY": args.llm_api_key,
        "CUA_MODEL": args.model,
        "CUA_START_URL": task["resolved_start_url"],
        "CUA_MAX_STEPS": str(args.max_steps),
        "CUA_PORT_FORWARDS": port_forwards,
        "PYTHONUNBUFFERED": "1",
    }

    # ── HAR capture: start proxy sidecar ──
    har_proxy = None
    if args.har_capture:
        from har import HarProxy
        har_proxy = HarProxy(
            client=client,
            task_id=task_id,
            network=args.network,
            output_dir=Path(args.output_dir),
            port_forwards=dict(SITE_PORT_FORWARDS),
            host_target=host_target,
        )
        try:
            har_proxy.start()
            env["CUA_HTTP_PROXY"] = har_proxy.proxy_url
            print(f"    HAR proxy: {har_proxy.proxy_url}")
            if active_containers is not None and har_proxy.container:
                active_containers.append(har_proxy.container)
        except Exception as e:
            print(f"    ⚠ Failed to start HAR proxy: {e}")
            har_proxy = None

    instruction = build_instruction(task)

    volumes = {}
    if args.calibration_volume:
        volumes[args.calibration_volume] = {
            "bind": "/var/cua/calibration",
            "mode": "rw",
        }

    t0 = time.monotonic()

    try:
        container = client.containers.run(
            image=args.agent_image,
            command=["agent", instruction],
            name=name,
            environment=env,
            network=args.network,
            extra_hosts={"host.docker.internal": host_target},
            volumes=volumes,
            ports={"5900/tcp": args.vnc_port},
            detach=True,
            mem_limit="2g",
            shm_size="256m",
        )
        print(f"    VNC: localhost:{args.vnc_port}")
        if active_containers is not None:
            active_containers.append(container)
    except Exception as e:
        if har_proxy:
            har_proxy.cleanup()
        return {
            "task_id": task_id,
            "outcome": "error",
            "summary": f"Failed to start agent: {e}",
            "result": None,
            "steps": 0,
            "elapsed": 0,
        }

    # ── Stream logs and parse output ──
    outcome = "max_steps"
    summary = "Reached maximum steps without completing"
    task_result = None
    steps = 0

    try:
        deadline = t0 + args.timeout
        for chunk in container.logs(stream=True, follow=True):
            if time.monotonic() > deadline:
                outcome = "timeout"
                summary = f"Timed out after {args.timeout}s"
                container.stop(timeout=5)
                break

            for line in chunk.decode(errors="replace").splitlines():
                line = line.rstrip()
                # Strip ANSI escape codes (focus events, colors, etc.)
                line = re.sub(r'\x1b\[[^a-zA-Z]*[a-zA-Z]', '', line)
                if not line:
                    continue

                if args.verbose:
                    print(f"    | {line}")

                if line.startswith("── Step"):
                    m = re.search(r"Step (\d+)", line)
                    if m:
                        steps = int(m.group(1))
                elif line.startswith("✅ Done:"):
                    outcome = "completed"
                    summary = line[len("✅ Done:") :].strip()
                elif line.strip().startswith("Result:"):
                    task_result = line.split("Result:", 1)[1].strip()
                elif "Reached max steps" in line:
                    outcome = "max_steps"
                    summary = "Reached maximum steps without completing"

    except Exception as e:
        if outcome not in ("completed", "timeout"):
            outcome = "error"
            summary = f"Log streaming error: {e}"

    elapsed = time.monotonic() - t0

    # Wait for container exit
    try:
        container.wait(timeout=15)
    except Exception:
        pass

    # Read done payload
    done_payload = {}
    if outcome == "completed":
        done_payload = read_done_payload(container)
        if done_payload.get("result"):
            task_result = done_payload["result"]
        if done_payload.get("summary"):
            summary = done_payload["summary"]

    # ── HAR capture: extract HAR before removing containers ──
    har_path = None
    if har_proxy:
        har_path = har_proxy.stop_and_extract()
        if har_path:
            print(f"    HAR: {har_path}")

    # ── Extract audit trail (screenshots + metadata) ──
    audit_path = extract_audit(container, task_id, Path(args.output_dir))
    if audit_path:
        print(f"    Audit: {audit_path}")

    # Cleanup agent container
    try:
        container.remove(force=True)
    except Exception:
        pass

    # Unregister from active list
    if active_containers is not None:
        active_containers.clear()

    return {
        "task_id": task_id,
        "outcome": outcome,
        "summary": summary,
        "result": task_result,
        "steps": steps,
        "elapsed": round(elapsed, 1),
        "har_path": str(har_path) if har_path else None,
    }


# ── Report formatting ─────────────────────────────────────────────


def format_report(results: list[dict], tasks: list[dict], args) -> dict:
    """Build the final JSON report."""
    total = len(results)
    passed = sum(1 for r in results if r.get("passed"))
    failed = total - passed
    errored = sum(
        1 for r in results if r.get("outcome") in ("error", "timeout")
    )

    # Per-site breakdown
    site_stats: dict[str, dict] = {}
    task_lookup = {t["task_id"]: t for t in tasks}
    for r in results:
        t = task_lookup.get(r["task_id"], {})
        for site in t.get("sites", ["unknown"]):
            if site not in site_stats:
                site_stats[site] = {"total": 0, "passed": 0}
            site_stats[site]["total"] += 1
            if r.get("passed"):
                site_stats[site]["passed"] += 1

    for stats in site_stats.values():
        stats["rate"] = (
            stats["passed"] / stats["total"] if stats["total"] else 0
        )

    # Per-eval-type breakdown
    eval_stats: dict[str, dict] = {}
    for r in results:
        t = task_lookup.get(r["task_id"], {})
        for et in t.get("eval", {}).get("eval_types", ["unknown"]):
            if et not in eval_stats:
                eval_stats[et] = {"total": 0, "passed": 0}
            eval_stats[et]["total"] += 1
            if r.get("passed"):
                eval_stats[et]["passed"] += 1

    return {
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errored": errored,
            "success_rate": passed / total if total else 0,
        },
        "by_site": site_stats,
        "by_eval_type": eval_stats,
        "config": {
            "model": args.model,
            "max_steps": args.max_steps,
            "timeout": args.timeout,
            "agent_image": args.agent_image,
            "tasks_spec": args.tasks,
            "site_filter": args.site or "all",
            "har_capture": args.har_capture,
            "webarena_verified": args.webarena_verified,
        },
        "results": results,
    }


# ── Main ──────────────────────────────────────────────────────────


def run_benchmark(args: argparse.Namespace) -> None:
    """Main benchmark loop."""
    # --webarena-verified implies --har-capture
    if args.webarena_verified:
        args.har_capture = True

    tasks = load_tasks(args.task_file, args)
    if not tasks:
        print("No tasks matched the filters.", file=sys.stderr)
        sys.exit(1)

    client = docker.from_env()

    # Pre-flight: ensure sidecar images are available
    from gateway import ensure_nginx_image
    ensure_nginx_image(client)
    if args.har_capture:
        from har import ensure_proxy_image
        ensure_proxy_image(client)

    # Clear calibration cache if requested
    if args.recalibrate and args.calibration_volume:
        print("  Clearing calibration cache...")
        try:
            client.containers.run(
                image="alpine",
                command=["sh", "-c", "rm -f /cal/*.json"],
                volumes={args.calibration_volume: {"bind": "/cal", "mode": "rw"}},
                remove=True,
            )
            print("  Calibration cleared — first task will re-calibrate.")
        except Exception as e:
            print(f"  ⚠ Could not clear calibration: {e}")

    # Start the nginx gateway (sits on internal webarena-net +
    # external webarena-ext, forwards site and LLM traffic)
    from gateway import Gateway
    gateway = Gateway(client, args.network, args.llm_base_url)
    gateway.start()
    gateway_ip = gateway.ip

    # Pre-flight: generate WA-Verified config if needed
    wa_config = None
    if args.webarena_verified:
        from verified import generate_wa_config
        wa_config_path = Path(args.output_dir) / "wa_config.json"
        wa_config = generate_wa_config(wa_config_path)
        print(f"  WA-Verified config: {wa_config_path}")

    eval_mode = "webarena-verified" if args.webarena_verified else "legacy"

    print(f"Loaded {len(tasks)} tasks")
    print(f"  Agent image : {args.agent_image}")
    print(f"  Model       : {args.model}")
    print(f"  Network     : {args.network}")
    print(f"  Max steps   : {args.max_steps}")
    print(f"  Timeout     : {args.timeout}s")
    print(f"  HAR capture : {args.har_capture}")
    print(f"  Evaluation  : {eval_mode}")
    print()

    # ── Signal handling for clean Ctrl+C ──
    interrupted = False
    # Mutable list holding the currently running containers so the
    # interrupt handler can kill them immediately.
    active_containers: list = []

    def on_interrupt(signum, frame):
        nonlocal interrupted
        if interrupted:
            # Second Ctrl+C — force exit
            print("\nForce exit. Cleaning up...")
            cleanup_benchmark(client)
            sys.exit(1)
        interrupted = True
        print("\nInterrupted. Stopping current task...")
        # Kill any active containers (agent + proxy sidecars)
        for c in list(active_containers):
            try:
                c.remove(force=True)
            except Exception:
                pass
        active_containers.clear()

    old_handler = signal.signal(signal.SIGINT, on_interrupt)

    results: list[dict] = []

    try:
        for i, raw_task in enumerate(tasks):
            if interrupted:
                print(f"\nSkipping remaining {len(tasks) - i} tasks.")
                break

            task = resolve_urls(raw_task)
            intent_short = task["resolved_intent"][:80]
            print(
                f"[{i + 1}/{len(tasks)}] Task {task['task_id']}: {intent_short}"
            )

            result = spawn_agent(client, task, args, gateway_ip, active_containers)

            # ── Evaluate ──
            if args.webarena_verified:
                from verified import build_agent_response
                build_agent_response(
                    result, raw_task, Path(args.output_dir)
                )

                try:
                    wa_result = evaluate_task_verified(
                        raw_task, result, Path(args.output_dir), wa_config
                    )
                except Exception as e:
                    print(f"    ⚠ Evaluation error: {e}")
                    wa_result = {
                        "passed": False, "score": 0.0, "status": "error",
                    }
                result["passed"] = wa_result["passed"]
                result["wa_score"] = wa_result.get("score", 0.0)
                result["wa_status"] = wa_result.get("status", "error")
            else:
                result["passed"] = evaluate_task(raw_task, result)

            results.append(result)

            status = "PASS" if result["passed"] else "FAIL"
            print(
                f"  → {status} ({result['steps']} steps, "
                f"{result['elapsed']}s, {result['outcome']})"
            )
            if result.get("result"):
                print(f"  → Result: {str(result['result'])[:120]}")

        # Write report (even if interrupted — partial results are useful)
        if results:
            output_dir = Path(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            report = format_report(results, tasks, args)
            output_path = output_dir / f"report_{int(time.time())}.json"
            output_path.write_text(json.dumps(report, indent=2))

            s = report["summary"]
            print(f"\n{'=' * 60}")
            print(f"Results: {s['success_rate']:.1%} ({s['passed']}/{s['total']})")
            print(f"  Passed : {s['passed']}")
            print(f"  Failed : {s['failed']}")
            print(f"  Errored: {s['errored']}")

            if report["by_site"]:
                print("\nBy site:")
                for site, stats in sorted(report["by_site"].items()):
                    print(
                        f"  {site:20s} {stats['passed']:3d}/{stats['total']:3d} "
                        f"({stats['rate']:.0%})"
                    )

            print(f"\nFull report: {output_path}")

            if args.webarena_verified:
                print(
                    f"\nWA-Verified output: {args.output_dir}/{{task_id}}/"
                    f"\n  Each task dir contains agent_response.json + network.har"
                    f"\n  Re-evaluate offline with:"
                    f"\n    webarena-verified eval-tasks \\"
                    f"\n      --config {args.output_dir}/wa_config.json \\"
                    f"\n      --output-dir {args.output_dir}"
                )

    finally:
        # Always clean up gateway + any stale containers
        print("\nCleaning up benchmark containers...")
        gateway.stop()
        signal.signal(signal.SIGINT, old_handler)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="WebArena benchmark runner for the CUA agent"
    )
    p.add_argument(
        "--task-file",
        default="benchmark/tasks/test.raw.json",
        help="Path to test.raw.json (default: benchmark/tasks/test.raw.json)",
    )
    p.add_argument(
        "--tasks",
        default="all",
        help='Task filter: "all", "0-50", "108", "108,200,305"',
    )
    p.add_argument(
        "--site",
        default=None,
        help="Filter by site: shopping, reddit, gitlab, etc.",
    )
    p.add_argument(
        "--eval-type",
        default=None,
        help="Filter by eval type: string_match, url_match, program_html",
    )
    p.add_argument(
        "--all-sites",
        action="store_true",
        help="Include tasks for all sites (not just Phase 1 sites)",
    )
    p.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help=f"Max agent steps per task (default: {DEFAULT_MAX_STEPS})",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Per-task timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    p.add_argument(
        "--output-dir",
        default="benchmark/results",
        help="Where to write results (default: benchmark/results/)",
    )
    p.add_argument(
        "--agent-image",
        default=DEFAULT_AGENT_IMAGE,
        help=f"Docker image name (default: {DEFAULT_AGENT_IMAGE})",
    )
    p.add_argument(
        "--network",
        default=DEFAULT_NETWORK,
        help=f"Docker network (default: {DEFAULT_NETWORK})",
    )
    p.add_argument(
        "--calibration-volume",
        default=DEFAULT_CALIBRATION_VOLUME,
        help="Docker volume for calibration data",
    )
    p.add_argument(
        "--recalibrate",
        action="store_true",
        help="Clear cached calibration before running (forces re-calibration on first task)",
    )
    p.add_argument(
        "--model",
        default=LLM_MODEL,
        help="LLM model name (passed to agent)",
    )
    p.add_argument(
        "--llm-base-url",
        default=LLM_BASE_URL,
        help="LLM API base URL",
    )
    p.add_argument(
        "--llm-api-key",
        default=LLM_API_KEY,
        help="LLM API key",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print agent stdout in real-time",
    )
    p.add_argument(
        "--vnc-port",
        type=int,
        default=5900,
        help="Host port to expose agent VNC on (default: 5900)",
    )
    # ── Phase 3 flags ──
    p.add_argument(
        "--har-capture",
        action="store_true",
        help="Enable HAR capture via per-task mitmproxy sidecar",
    )
    p.add_argument(
        "--webarena-verified",
        action="store_true",
        help=(
            "Use WebArena-Verified for evaluation (implies --har-capture). "
            "Requires: pip install webarena-verified"
        ),
    )
    # ── Maintenance ──
    p.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove all leftover benchmark containers and networks, then exit",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.cleanup:
        print("Cleaning up benchmark containers...")
        cleanup_benchmark()
    else:
        run_benchmark(args)
