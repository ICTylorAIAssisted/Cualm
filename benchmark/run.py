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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    vnc_port: int | None = None,
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
    _vnc_port = vnc_port if vnc_port is not None else args.vnc_port

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
    # Forward optional LLM tuning env vars
    for var in ("CUA_TEMPERATURE", "CUA_TOP_P", "CUA_PRESENCE_PENALTY",
                "CUA_LLM_EXTRA_PARAMS", "CUA_HISTORY_PAIRS"):
        val = os.environ.get(var)
        if val:
            env[var] = val

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
            ports={"5900/tcp": _vnc_port} if _vnc_port else {},
            detach=True,
            mem_limit="2g",
            shm_size="256m",
        )
        if _vnc_port:
            print(f"    VNC: localhost:{_vnc_port}")
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
        try:
            active_containers.remove(container)
        except ValueError:
            pass

    return {
        "task_id": task_id,
        "outcome": outcome,
        "summary": summary,
        "result": task_result,
        "steps": steps,
        "elapsed": round(elapsed, 1),
        "har_path": str(har_path) if har_path else None,
    }


# ── Human mode ────────────────────────────────────────────────────


def spawn_human(
    client: docker.DockerClient,
    task: dict,
    args: argparse.Namespace,
    gateway_ip: str | None = None,
) -> dict:
    """Start a container for manual task completion via VNC.

    Opens the browser, prints the task and credentials, waits for the
    human to enter a result via stdin, then tears down the container.
    Supports HAR capture and WA-Verified evaluation.
    """
    task_id = task["task_id"]
    name = f"bench-human-{task_id}-{int(time.time())}"
    host_target = gateway_ip or "host-gateway"
    start_url = task["resolved_start_url"]
    sites = task.get("sites", [])
    needs_login = task.get("require_login", False)

    port_forwards = ",".join(
        f"{lport}:{host}:{rport}"
        for lport, (host, rport) in sorted(SITE_PORT_FORWARDS.items())
    )

    env = {
        "CUA_START_URL": start_url,
        "CUA_PORT_FORWARDS": port_forwards,
        "PYTHONUNBUFFERED": "1",
    }

    # ── HAR capture: start proxy sidecar ──
    har_proxy = None
    har_path = None
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
        except Exception as e:
            print(f"    ⚠ Failed to start HAR proxy: {e}")
            har_proxy = None

    t0 = time.monotonic()

    try:
        container = client.containers.run(
            image=args.agent_image,
            # No "agent" arg — start.sh starts Xvfb, Chromium, VNC
            # and waits with tail -f /dev/null
            name=name,
            environment=env,
            network=args.network,
            extra_hosts={"host.docker.internal": host_target},
            ports={"5900/tcp": args.vnc_port},
            detach=True,
            mem_limit="2g",
            shm_size="256m",
        )
    except Exception as e:
        if har_proxy:
            har_proxy.cleanup()
        return {
            "task_id": task_id,
            "outcome": "error",
            "summary": f"Failed to start container: {e}",
            "result": None,
            "steps": 0,
            "elapsed": 0,
        }

    # Print task info for the human
    eval_info = task.get("eval", {})
    ref = eval_info.get("reference_answers", {})
    must_include = ref.get("must_include", [])
    exact = ref.get("exact_match", "N/A")
    eval_types = eval_info.get("eval_types", [])

    print()
    print(f"    ╔══════════════════════════════════════════════════════════")
    print(f"    ║  HUMAN MODE — Task {task_id}")
    print(f"    ║")
    print(f"    ║  VNC: localhost:{args.vnc_port}")
    print(f"    ║  URL: {start_url}")
    print(f"    ║")
    print(f"    ║  Task: {task['resolved_intent']}")
    if needs_login:
        for site in sites:
            creds = SITE_CREDENTIALS.get(site)
            if creds:
                print(f"    ║  Login ({site}): {creds['username']} / {creds['password']}")
    print(f"    ║")
    print(f"    ║  Eval type: {', '.join(eval_types)}")
    if must_include:
        print(f"    ║  Expected (must include): {must_include}")
    if isinstance(exact, str) and exact != "N/A":
        print(f"    ║  Expected (exact match): {exact}")
    raw_annotation = ref.get("reference_answer_raw_annotation", "")
    if not raw_annotation:
        raw_annotation = eval_info.get("reference_answer_raw_annotation", "")
    if raw_annotation:
        print(f"    ║  Raw annotation: {raw_annotation}")
    print(f"    ║")
    print(f"    ║  Connect via VNC, complete the task, then come back here.")
    print(f"    ╚══════════════════════════════════════════════════════════")
    print()

    # Wait for human input
    try:
        result_text = input("    Enter result (or 'skip' to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        result_text = ""

    elapsed = time.monotonic() - t0

    if result_text.lower() == "skip" or not result_text:
        outcome = "skipped"
        summary = "Skipped by human"
        task_result = None
    else:
        outcome = "completed"
        summary = "Completed by human"
        task_result = result_text

    # Extract HAR
    if har_proxy:
        har_path = har_proxy.stop_and_extract()
        if har_path:
            print(f"    HAR: {har_path}")

    # Cleanup
    try:
        container.remove(force=True)
    except Exception:
        pass

    return {
        "task_id": task_id,
        "outcome": outcome,
        "summary": summary,
        "result": task_result,
        "steps": 0,
        "elapsed": round(elapsed, 1),
        "har_path": str(har_path) if har_path else None,
    }


# ── Incremental progress (crash-safe) ────────────────────────────

PROGRESS_FILENAME = "progress.jsonl"


def _progress_path(output_dir: str | Path) -> Path:
    return Path(output_dir) / PROGRESS_FILENAME


def load_progress(output_dir: str | Path) -> list[dict]:
    """Load completed results from progress.jsonl."""
    path = _progress_path(output_dir)
    results = []
    if not path.exists():
        return results
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # skip corrupted lines
    return results


def append_progress(output_dir: str | Path, result: dict) -> None:
    """Append one result to progress.jsonl (crash-safe)."""
    path = _progress_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(result) + "\n")
        f.flush()
        os.fsync(f.fileno())


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

    # ── Resume: load previous progress, skip completed tasks ──
    resumed_results: list[dict] = []
    if args.resume:
        resumed_results = load_progress(args.output_dir)
        if resumed_results:
            done_ids = {r["task_id"] for r in resumed_results}
            before = len(tasks)
            tasks = [t for t in tasks if t["task_id"] not in done_ids]
            passed = sum(1 for r in resumed_results if r.get("passed"))
            print(f"  Resuming: {len(done_ids)} tasks already done "
                  f"({passed} passed), {len(tasks)}/{before} remaining")
            if not tasks:
                print("All tasks already completed.")
                # Still write the final report from resumed data
                tasks_all = load_tasks(args.task_file, args)
                output_dir = Path(args.output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)
                report = format_report(resumed_results, tasks_all, args)
                output_path = output_dir / f"report_{int(time.time())}.json"
                output_path.write_text(json.dumps(report, indent=2))
                s = report["summary"]
                print(f"Results: {s['success_rate']:.1%} ({s['passed']}/{s['total']})")
                print(f"Full report: {output_path}")
                return
        else:
            print("  Resume: no previous progress found, starting fresh")
    else:
        # Fresh run — clear stale progress so it doesn't contaminate
        # a future --resume
        progress = _progress_path(args.output_dir)
        if progress.exists():
            progress.unlink()

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
    run_mode = "human" if args.human else "agent"

    print(f"Loaded {len(tasks)} tasks")
    print(f"  Mode        : {run_mode}")
    print(f"  Agent image : {args.agent_image}")
    if not args.human:
        print(f"  Model       : {args.model}")
        print(f"  Max steps   : {args.max_steps}")
        print(f"  Timeout     : {args.timeout}s")
    print(f"  HAR capture : {args.har_capture}")
    print(f"  Evaluation  : {eval_mode}")
    print(f"  Network     : {args.network}")
    print(f"  VNC port    : {args.vnc_port}")
    parallel = max(1, args.parallel)
    if parallel > 1:
        print(f"  Parallel    : {parallel}")
    print()

    # ── Signal handling for clean Ctrl+C ──
    interrupted = False
    # Thread-safe list of running containers for Ctrl+C cleanup.
    active_containers: list = []
    active_lock = threading.Lock()

    def on_interrupt(signum, frame):
        nonlocal interrupted
        if interrupted:
            print("\nForce exit. Cleaning up...")
            cleanup_benchmark(client)
            sys.exit(1)
        interrupted = True
        print("\nInterrupted. Stopping running tasks...")
        with active_lock:
            for c in list(active_containers):
                try:
                    c.remove(force=True)
                except Exception:
                    pass
            active_containers.clear()

    old_handler = signal.signal(signal.SIGINT, on_interrupt)

    # ── Thread-safe output ──
    print_lock = threading.Lock()
    completed_count = [0]  # mutable for closure access

    def tprint(*a, **kw):
        with print_lock:
            print(*a, **kw)

    # ── Per-task worker ──
    def run_one_task(idx: int, raw_task: dict, vnc_port: int | None) -> dict:
        """Run a single task: spawn agent, evaluate, return result dict."""
        task = resolve_urls(raw_task)
        task_id = task["task_id"]
        intent_short = task["resolved_intent"][:80]
        prefix = f"[{idx + 1}/{len(tasks)}] Task {task_id}"

        tprint(f"{prefix}: {intent_short}")

        if args.human:
            result = spawn_human(client, task, args, gateway_ip)
        else:
            result = spawn_agent(
                client, task, args, gateway_ip,
                active_containers, vnc_port=vnc_port,
            )

        # ── Evaluate ──
        if args.webarena_verified:
            from verified import build_agent_response
            build_agent_response(result, raw_task, Path(args.output_dir))
            try:
                wa_result = evaluate_task_verified(
                    raw_task, result, Path(args.output_dir), wa_config
                )
            except Exception as e:
                tprint(f"    ⚠ Evaluation error: {e}")
                wa_result = {
                    "passed": False, "score": 0.0, "status": "error",
                }
            result["passed"] = wa_result["passed"]
            result["wa_score"] = wa_result.get("score", 0.0)
            result["wa_status"] = wa_result.get("status", "error")
        else:
            result["passed"] = evaluate_task(raw_task, result)

        # ── Save to progress file (crash-safe) ──
        append_progress(args.output_dir, result)

        # ── Save task info for trace viewer ──
        try:
            task_dir = Path(args.output_dir) / str(task_id)
            task_dir.mkdir(parents=True, exist_ok=True)
            task_info = {
                "task_id": task_id,
                "intent": raw_task.get("intent", ""),
                "sites": raw_task.get("sites", []),
                "reference_answers": raw_task.get("eval", {}).get(
                    "reference_answers", {}),
                "eval_types": raw_task.get("eval", {}).get("eval_types", []),
                "agent_result": result.get("result"),
                "agent_outcome": result.get("outcome"),
                "passed": result.get("passed", False),
            }
            (task_dir / "task_info.json").write_text(
                json.dumps(task_info, indent=2))
        except Exception:
            pass

        # ── Report ──
        with print_lock:
            completed_count[0] += 1
            done = completed_count[0]
        if result["outcome"] == "skipped":
            tprint(f"{prefix} → SKIP ({result['elapsed']}s)  [{done}/{len(tasks)}]")
        else:
            status = "PASS" if result["passed"] else "FAIL"
            tprint(
                f"{prefix} → {status} ({result['steps']} steps, "
                f"{result['elapsed']}s, {result['outcome']})  [{done}/{len(tasks)}]"
            )
        if result.get("result"):
            tprint(f"  → Result: {str(result['result'])[:120]}")

        return result

    results: list[dict] = []

    try:
        if parallel <= 1:
            # ── Sequential execution (original behavior) ──
            for i, raw_task in enumerate(tasks):
                if interrupted:
                    print(f"\nSkipping remaining {len(tasks) - i} tasks.")
                    break
                result = run_one_task(i, raw_task, args.vnc_port)
                results.append(result)
        else:
            # ── Parallel execution ──
            # VNC ports: -j0 means no VNC, otherwise auto-assign from base
            vnc_base = args.vnc_port if args.parallel > 0 else None

            futures = {}
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                for i, raw_task in enumerate(tasks):
                    if interrupted:
                        break
                    vnc_p = (vnc_base + (i % parallel)) if vnc_base else None
                    future = pool.submit(run_one_task, i, raw_task, vnc_p)
                    futures[future] = i

                for future in as_completed(futures):
                    if interrupted:
                        # Cancel pending futures
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as e:
                        idx = futures[future]
                        tprint(f"  ⚠ Task {idx} raised: {e}")

        # Write report (even if interrupted — partial results are useful)
        # Merge with resumed results for a complete picture
        all_results = resumed_results + results
        if all_results:
            # Reload full task list for report (tasks may have been filtered)
            all_tasks = load_tasks(args.task_file, args)
            output_dir = Path(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            report = format_report(all_results, all_tasks, args)
            output_path = output_dir / f"report_{int(time.time())}.json"
            output_path.write_text(json.dumps(report, indent=2))

            s = report["summary"]
            print(f"\n{'=' * 60}")
            print(f"Results: {s['success_rate']:.1%} ({s['passed']}/{s['total']})")
            print(f"  Passed : {s['passed']}")
            print(f"  Failed : {s['failed']}")
            print(f"  Errored: {s['errored']}")
            if resumed_results and results:
                print(f"  (includes {len(resumed_results)} resumed "
                      f"+ {len(results)} new)")

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
    p.add_argument(
        "--parallel", "-j",
        type=int,
        default=1,
        help="Run N tasks in parallel (default: 1). "
             "VNC ports auto-assigned starting from --vnc-port. "
             "Use -j0 to disable VNC in parallel mode.",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Resume from a previous run. Reads progress.jsonl in "
             "--output-dir, skips already-completed tasks.",
    )
    # ── Human mode ──
    p.add_argument(
        "--human",
        action="store_true",
        help="Manual mode: open VNC for each task, human completes it, "
             "types result. Validates environment and task feasibility.",
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
