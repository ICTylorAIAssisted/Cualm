"""WebArena-Verified integration.

Handles:
  - Generating the WA-Verified config JSON from our URL templates
  - Translating the agent's freeform cua-done result into the
    structured agent_response.json schema WA-Verified expects
  - Calling the WA-Verified Python API for deterministic evaluation
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from config import SITE_CREDENTIALS, URL_TEMPLATES_HOST


# ── WA-Verified config generation ─────────────────────────────────


def generate_wa_config(output_path: Path | None = None) -> dict:
    """Build a WebArena-Verified configuration dict.

    Maps our URL_TEMPLATES / SITE_CREDENTIALS to the format expected
    by ``WebArenaVerifiedConfig``.
    """
    # Map template keys to WA-Verified environment keys
    env_map = {
        "__SHOPPING__": {
            "urls": [URL_TEMPLATES_HOST["__SHOPPING__"]],
            "credentials": {
                "username": SITE_CREDENTIALS["shopping"]["username"],
                "password": SITE_CREDENTIALS["shopping"]["password"],
            },
        },
        "__SHOPPING_ADMIN__": {
            "urls": [URL_TEMPLATES_HOST["__SHOPPING_ADMIN__"]],
            "credentials": {
                "username": SITE_CREDENTIALS["shopping_admin"]["username"],
                "password": SITE_CREDENTIALS["shopping_admin"]["password"],
            },
        },
        "__REDDIT__": {
            "urls": [URL_TEMPLATES_HOST["__REDDIT__"]],
            "credentials": {
                "username": SITE_CREDENTIALS["reddit"]["username"],
                "password": SITE_CREDENTIALS["reddit"]["password"],
            },
        },
        "__GITLAB__": {
            "urls": [URL_TEMPLATES_HOST["__GITLAB__"]],
            "credentials": {
                "username": SITE_CREDENTIALS["gitlab"]["username"],
                "password": SITE_CREDENTIALS["gitlab"]["password"],
            },
        },
    }

    config = {"environments": env_map}

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(config, indent=2))

    return config


# ── Task type inference ───────────────────────────────────────────

# Keywords that strongly suggest each task type
_NAVIGATE_PATTERNS = [
    r"\bnavigate\s+to\b", r"\bgo\s+to\b", r"\bopen\b.*\bpage\b",
    r"\bshow\s+me\b", r"\bbrowse\b", r"\bvisit\b",
]
_MUTATE_PATTERNS = [
    r"\bcreate\b", r"\bdelete\b", r"\bremove\b", r"\bedit\b",
    r"\bmodify\b", r"\bchange\b", r"\bupdate\b", r"\badd\b",
    r"\bset\b.*\bto\b", r"\bpost\b", r"\bsubmit\b", r"\bwrite\b",
    r"\bsubscribe\b", r"\bunsubscribe\b", r"\bstar\b", r"\bfork\b",
    r"\bmerge\b", r"\bclose\b.*\bissue\b", r"\bcomment\b",
]


def infer_task_type(intent: str, eval_types: list[str] | None = None) -> str:
    """Infer RETRIEVE / NAVIGATE / MUTATE from the task intent.

    Heuristic order:
      1. url_match eval type → NAVIGATE
      2. Mutation keyword in intent → MUTATE
      3. Navigation keyword in intent → NAVIGATE
      4. Default (information retrieval) → RETRIEVE
    """
    if eval_types and "url_match" in eval_types:
        return "NAVIGATE"

    intent_lower = intent.lower()

    for pattern in _MUTATE_PATTERNS:
        if re.search(pattern, intent_lower):
            return "MUTATE"

    for pattern in _NAVIGATE_PATTERNS:
        if re.search(pattern, intent_lower):
            return "NAVIGATE"

    return "RETRIEVE"


# ── Agent response builder ────────────────────────────────────────

# Status codes defined by WA-Verified
VALID_STATUSES = {
    "SUCCESS",
    "NOT_FOUND_ERROR",
    "PERMISSION_DENIED_ERROR",
    "DATA_VALIDATION_ERROR",
    "ACTION_NOT_ALLOWED_ERROR",
    "UNKNOWN_ERROR",
}


def build_agent_response(
    agent_result: dict,
    task_config: dict,
    output_dir: Path | None = None,
) -> dict:
    """Convert the agent's freeform cua-done output to a WA-Verified
    structured agent_response.json.

    Args:
        agent_result: The dict returned by spawn_agent (outcome, result, …)
        task_config:  The raw task JSON with intent, eval, sites, etc.
        output_dir:   If given, writes agent_response.json here.

    Returns:
        The structured agent response dict.
    """
    intent = task_config.get("intent", "")
    eval_types = task_config.get("eval", {}).get("eval_types", [])
    task_type = infer_task_type(intent, eval_types)

    outcome = agent_result.get("outcome", "error")
    raw_result = agent_result.get("result")

    # Determine status
    if outcome == "completed":
        status = "SUCCESS"
    elif outcome == "max_steps":
        status = "UNKNOWN_ERROR"
    elif outcome == "timeout":
        status = "UNKNOWN_ERROR"
    else:
        status = "UNKNOWN_ERROR"

    # Build retrieved_data for RETRIEVE tasks
    retrieved_data = None
    if task_type == "RETRIEVE" and status == "SUCCESS" and raw_result:
        # WA-Verified expects a list of strings
        retrieved_data = [str(raw_result)]

    # Build error_details
    error_details = None
    if status != "SUCCESS":
        error_details = agent_result.get("summary", f"Agent {outcome}")

    response = {
        "task_type": task_type,
        "status": status,
        "retrieved_data": retrieved_data,
        "error_details": error_details,
    }

    if output_dir:
        task_id = task_config.get("task_id", 0)
        task_dir = output_dir / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        resp_path = task_dir / "agent_response.json"
        resp_path.write_text(json.dumps(response, indent=2))

    return response


# ── WA-Verified evaluation ────────────────────────────────────────


def evaluate_with_wa_verified(
    task_id: int,
    agent_response: dict | Path,
    network_trace: dict | Path | None,
    wa_config: dict | None = None,
) -> dict[str, Any]:
    """Evaluate a single task via the WA-Verified Python API.

    Returns a dict with at least:
      - passed: bool
      - score: float (0.0 or 1.0)
      - status: str
      - details: full evaluator result or error message
    """
    try:
        from webarena_verified.api import WebArenaVerified
        from webarena_verified.types.config import WebArenaVerifiedConfig
    except ImportError:
        return {
            "passed": False,
            "score": 0.0,
            "status": "error",
            "details": (
                "webarena-verified package not installed. "
                "Run: pip install webarena-verified"
            ),
        }

    if network_trace is None:
        return {
            "passed": False,
            "score": 0.0,
            "status": "error",
            "details": "No HAR trace available — cannot evaluate network events",
        }

    try:
        if wa_config is None:
            wa_config = generate_wa_config()

        config = WebArenaVerifiedConfig(**wa_config)
        wa = WebArenaVerified(config=config)

        result = wa.evaluate_task(
            task_id=task_id,
            agent_response=agent_response,
            network_trace=network_trace,
        )

        return {
            "passed": result.score == 1.0,
            "score": result.score,
            "status": result.status,
            "details": (
                result.model_dump()
                if hasattr(result, "model_dump")
                else str(result)
            ),
        }

    except Exception as e:
        return {
            "passed": False,
            "score": 0.0,
            "status": "error",
            "details": f"WA-Verified evaluation error: {e}",
        }
