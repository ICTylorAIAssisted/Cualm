#!/usr/bin/env python3
"""Re-evaluate existing benchmark results with the current evaluator.

Reads progress.jsonl from a results directory, re-runs evaluation
using the current evaluate.py logic, and updates:
  - task_info.json in each task directory
  - Writes a new report JSON

Usage:
  python benchmark/re_evaluate.py benchmark/results/
  python benchmark/re_evaluate.py benchmark/results/ --task-file benchmark/tasks/test.raw.json
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from evaluate import evaluate_task, check_string_match


def main():
    p = argparse.ArgumentParser(description="Re-evaluate benchmark results")
    p.add_argument("results_dir", help="Directory with progress.jsonl")
    p.add_argument("--task-file", default="benchmark/tasks/test.raw.json",
                   help="Path to test.raw.json")
    args = p.parse_args()

    results_dir = Path(args.results_dir)
    progress_file = results_dir / "progress.jsonl"

    if not progress_file.exists():
        print(f"No progress.jsonl in {results_dir}", file=sys.stderr)
        sys.exit(1)

    # Load task configs
    with open(args.task_file) as f:
        all_tasks = json.load(f)
    task_lookup = {t["task_id"]: t for t in all_tasks}

    # Load results
    results = []
    for line in progress_file.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    if not results:
        print("No results found in progress.jsonl", file=sys.stderr)
        sys.exit(1)

    # Re-evaluate
    old_pass = sum(1 for r in results if r.get("passed"))
    changes = []

    for r in results:
        task_id = r["task_id"]
        task_config = task_lookup.get(task_id)
        if not task_config:
            print(f"  ⚠ Task {task_id} not found in task file, skipping")
            continue

        old_passed = r.get("passed", False)
        new_passed = evaluate_task(task_config, r)
        r["passed"] = new_passed

        if old_passed != new_passed:
            direction = "FAIL→PASS" if new_passed else "PASS→FAIL"
            changes.append((task_id, direction))
            print(f"  Task {task_id}: {direction}")

        # Update task_info.json
        task_dir = results_dir / str(task_id)
        if task_dir.exists():
            eval_config = task_config.get("eval", {})
            task_info = {
                "task_id": task_id,
                "intent": task_config.get("intent", ""),
                "sites": task_config.get("sites", []),
                "reference_answers": eval_config.get("reference_answers", {}),
                "eval_types": eval_config.get("eval_types", []),
                "agent_result": r.get("result"),
                "agent_outcome": r.get("outcome"),
                "passed": new_passed,
            }
            (task_dir / "task_info.json").write_text(
                json.dumps(task_info, indent=2))

    new_pass = sum(1 for r in results if r.get("passed"))

    # Write updated progress
    with open(progress_file, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    # Write new report
    report_path = results_dir / f"report_reeval_{int(time.time())}.json"
    total = len(results)
    report = {
        "summary": {
            "total": total,
            "passed": new_pass,
            "failed": total - new_pass,
            "success_rate": new_pass / total if total else 0,
        },
        "changes": [{"task_id": tid, "change": d} for tid, d in changes],
        "results": results,
    }
    report_path.write_text(json.dumps(report, indent=2))

    # Summary
    print(f"\n{'=' * 50}")
    print(f"Re-evaluated {total} tasks")
    print(f"  Before: {old_pass}/{total} ({old_pass/total:.1%})")
    print(f"  After:  {new_pass}/{total} ({new_pass/total:.1%})")
    if changes:
        flipped_pass = sum(1 for _, d in changes if d == "FAIL→PASS")
        flipped_fail = sum(1 for _, d in changes if d == "PASS→FAIL")
        print(f"  Changed: {len(changes)} tasks "
              f"({flipped_pass} ↑ {flipped_fail} ↓)")
    else:
        print("  No changes")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
