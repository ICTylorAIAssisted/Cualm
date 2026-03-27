"""WebArena task evaluation.

Supports two evaluation backends:

  1. **Legacy (Phase 1)** — built-in string_match only, no external
     dependencies.  Used when --webarena-verified is not passed.

  2. **WebArena-Verified (Phase 3)** — deterministic offline evaluation
     via the webarena-verified package.  Requires HAR capture
     (--har-capture) and a structured agent response.

The benchmark runner in run.py decides which path to take.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


# ── Legacy evaluation (Phase 1) ──────────────────────────────────


def evaluate_task(task_config: dict, agent_result: dict) -> bool:
    """Evaluate using the built-in string_match logic (Phase 1).

    Returns False for url_match / program_html — those require
    WebArena-Verified.
    """
    if agent_result.get("outcome") not in ("completed",):
        return False

    eval_config = task_config.get("eval", {})
    eval_types = eval_config.get("eval_types", [])

    for eval_type in eval_types:
        if eval_type == "string_match":
            return check_string_match(
                agent_result.get("result", ""),
                eval_config.get("reference_answers", {}),
            )
        elif eval_type in ("url_match", "program_html"):
            # Requires WA-Verified — use --webarena-verified mode
            return False

    return False


def _normalize(text: str) -> str:
    """Normalize text for comparison: lowercase, strip special chars."""
    import unicodedata
    # Replace common special chars
    text = text.replace("™", "").replace("®", "").replace("©", "")
    # Normalize unicode (e.g. fancy quotes → ascii)
    text = unicodedata.normalize("NFKD", text)
    # Collapse whitespace
    text = " ".join(text.split())
    return text.lower().strip()


def check_string_match(agent_answer: str, reference: dict) -> bool:
    """Check if the agent's answer matches reference criteria.

    WebArena reference_answers format:
      must_include: list of items, where each item is either:
        - a string: answer must contain it
        - a list of strings: answer must contain at least one (alternatives)
      exact_match: "N/A", a string, or a list of alternative strings
    """
    if agent_answer is None:
        agent_answer = ""
    answer = _normalize(agent_answer)

    must_include = reference.get("must_include", [])
    for item in must_include:
        if isinstance(item, str):
            # Single required string
            if _normalize(item) not in answer:
                return False
        elif isinstance(item, list):
            # List of alternatives — at least one must be present
            if not any(_normalize(alt) in answer
                      for alt in item if isinstance(alt, str)):
                return False

    exact = reference.get("exact_match", "N/A")
    if isinstance(exact, str) and exact != "N/A":
        if answer != _normalize(exact):
            return False
    elif isinstance(exact, list):
        # List of acceptable exact answers
        if not any(answer == _normalize(alt)
                  for alt in exact if isinstance(alt, str)):
            return False

    # If no criteria were specified, fail (nothing to match against)
    if not must_include:
        if isinstance(exact, str) and exact == "N/A":
            return False
        if not isinstance(exact, (str, list)):
            return False

    return True


# ── WebArena-Verified evaluation (Phase 3) ───────────────────────


def evaluate_task_verified(
    task_config: dict,
    agent_result: dict,
    output_dir: Path,
    wa_config: dict | None = None,
) -> dict[str, Any]:
    """Evaluate a single task via WebArena-Verified.

    Expects output_dir/{task_id}/network.har to already exist
    (written by the HAR proxy during task execution).  Builds
    agent_response.json from the agent result if not present.

    Returns a dict with: passed, score, status, details.
    """
    from verified import build_agent_response, evaluate_with_wa_verified

    task_id = task_config["task_id"]
    task_dir = output_dir / str(task_id)

    # Build agent_response.json
    resp_path = task_dir / "agent_response.json"
    if not resp_path.exists():
        build_agent_response(agent_result, task_config, output_dir)

    har_path = task_dir / "network.har"
    network_trace = None
    if har_path.exists():
        try:
            import json as _json
            har_data = _json.loads(har_path.read_text())
            entries = har_data.get("log", {}).get("entries", [])
            if entries:
                network_trace = har_path
            else:
                print(f"    ⚠ HAR has no entries for task {task_id} — "
                      f"evaluating agent response only")
        except Exception:
            pass

    return evaluate_with_wa_verified(
        task_id=task_id,
        agent_response=resp_path,
        network_trace=network_trace,
        wa_config=wa_config,
    )
