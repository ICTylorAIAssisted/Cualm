"""Discover CUA tools on PATH and collect their descriptions.

Each tool is expected to support ``--help`` (via Typer).  The Usage
line and first description line are extracted and formatted for the
system prompt.

Typer --help output looks like::

    Usage: cua-click [OPTIONS] X Y

      Click at screen coordinate (X, Y).
      ...

We extract: ``cua-click X Y [OPTIONS]`` + ``Click at screen coordinate``
"""

import os
import subprocess

TOOL_PREFIX = "cua-"


def _parse_help(name: str, help_text: str) -> str:
    """Extract a one-liner from --help output."""
    lines = help_text.strip().splitlines()

    # Find usage line
    usage = ""
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("usage:"):
            # "Usage: cua-click [OPTIONS] X Y" → "cua-click X Y [OPTIONS]"
            parts = stripped.split(":", 1)[1].strip()
            # Remove the tool path/name prefix, keep args
            tokens = parts.split()
            # tokens[0] is the tool name, rest are args
            args = [t for t in tokens[1:] if t != "[OPTIONS]"]
            opts = " [OPTIONS]" if "[OPTIONS]" in parts else ""
            usage = " ".join(args) + opts
            break

    # Find first description line (indented, after usage + blank line)
    desc = ""
    past_usage = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("usage:"):
            past_usage = True
            continue
        if past_usage and stripped and not stripped.startswith("╭") and not stripped.startswith("│"):
            desc = stripped.rstrip(".")
            break

    if usage or desc:
        return f"  {name} {usage:<42s} {desc}"
    return f"  {name:<44s} (run '{name} --help' for details)"


def discover_tools() -> list[str]:
    """Return one-line description strings for every cua-* tool on PATH."""
    seen: set[str] = set()
    descriptions: list[str] = []

    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.startswith(TOOL_PREFIX):
                continue
            if name in seen:
                continue
            full = os.path.join(directory, name)
            if not os.access(full, os.X_OK):
                continue
            seen.add(name)

            try:
                result = subprocess.run(
                    [full, "--help"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    descriptions.append(_parse_help(name, result.stdout))
                else:
                    descriptions.append(f"  {name:<44s} (run '{name} --help' for details)")
            except Exception:
                descriptions.append(f"  {name:<44s} (run '{name} --help' for details)")

    return descriptions
