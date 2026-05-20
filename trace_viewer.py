#!/usr/bin/env python3
"""Generate an interactive HTML trace viewer from audit session data.

Usage:
  # From a session directory
  python trace_viewer.py out/session_20260513_221326/

  # From an output directory (picks the latest session)
  python trace_viewer.py out/

  # Output to a specific file
  python trace_viewer.py out/session_20260513_221326/ -o trace.html

  # Process every session under a directory
  python trace_viewer.py out/ --all
"""

import argparse
import base64
import glob
import html
import json
import os
import re
import sys
from pathlib import Path


def find_session_dir(path: Path) -> Path | None:
    """Find the audit session directory from various input paths."""
    p = path.resolve()

    # Direct session dir (has trace.json)
    if (p / "trace.json").exists():
        return p

    # Task dir with audit/session_*
    audit = p / "audit"
    if audit.exists():
        sessions = sorted(audit.glob("session_*"))
        if sessions:
            return sessions[-1]  # latest session

    # Task dir with session_* directly inside audit extracted flat
    sessions = sorted(p.glob("session_*"))
    if sessions:
        return sessions[-1]

    return None


def load_session(session_dir: Path) -> dict:
    """Load trace + metadata + task info from a session directory."""
    trace_path = session_dir / "trace.json"
    meta_path = session_dir / "metadata.json"

    trace = []
    if trace_path.exists():
        with open(trace_path) as f:
            trace = json.load(f)

    meta = {}
    if meta_path.exists():
        with open(meta_path) as f:
            meta = json.load(f)

    # Load task_info.json from parent dirs (task dir has audit/session_*)
    task_info = {}
    for parent in [session_dir.parent, session_dir.parent.parent]:
        ti_path = parent / "task_info.json"
        if ti_path.exists():
            try:
                with open(ti_path) as f:
                    task_info = json.load(f)
                break
            except (json.JSONDecodeError, OSError):
                pass

    # Load screenshots as base64
    for step in trace:
        png_name = step.get("screenshot", "")
        if png_name:
            png_path = session_dir / png_name
            if png_path.exists():
                with open(png_path, "rb") as f:
                    step["_screenshot_b64"] = base64.b64encode(f.read()).decode()

    return {"trace": trace, "metadata": meta, "task_info": task_info,
            "session_dir": str(session_dir)}


def extract_think(reply: str) -> tuple:
    """Split reply into (thinking, action) parts."""
    m = re.search(r"<think>(.*?)</think>(.*)", reply, re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", reply.strip()


def extract_prompt_text(llm_input: list) -> str:
    """Extract the user prompt text from the redacted llm_input."""
    for msg in reversed(llm_input or []):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        return part.get("text", "")
            elif isinstance(content, str):
                return content
    return ""


def extract_section(prompt_text: str, start: str, ends: list) -> str:
    """Slice the block starting at `start` and ending at the first `end`."""
    if not prompt_text or start not in prompt_text:
        return ""
    section = prompt_text[prompt_text.index(start):]
    cut = len(section)
    for marker in ends:
        if marker in section:
            cut = min(cut, section.index(marker))
    return section[:cut].strip()


# Markers that delimit the dynamic blocks the agent appends to each prompt.
_BLOCK_MARKERS = [
    "\nCoverage status:", "\nAccessibility tree:", "\nCurrent plan:",
    "\nHere is the updated", "\n⚠ WARNING:", "\nCreate a plan",
]


def parse_coverage(coverage_text: str) -> dict:
    """Pull JS / CSS / Total percentages out of a coverage status block."""
    pcts = {}
    for key in ("JS", "CSS", "Total"):
        m = re.search(rf"{key}:\s*([\d.]+)%", coverage_text)
        if m:
            pcts[key.lower()] = float(m.group(1))
    return pcts


def next_plan_step(output: str) -> str:
    """Return the first pending (○) item from a cua-plan command's output."""
    for line in (output or "").splitlines():
        s = line.strip()
        if s.startswith("○"):
            t = s[1:].strip()
            t = re.sub(r"^\d+\.\s*", "", t)      # leading "3. "
            t = re.sub(r"^\[START\]\s*", "", t)  # "[START] "
            t = re.sub(r"^\d+\.\s*", "", t)      # duplicated "3. "
            return t
    return ""


def make_step_title(command: str, output: str) -> str:
    """A concise, useful step title — not the first slice of a giant plan."""
    cmd = (command or "").strip()
    if not cmd:
        return "(no command)"
    if cmd.startswith("cua-plan"):
        nxt = next_plan_step(output)
        return f"Plan → next: {nxt}" if nxt else "Plan → all steps done"
    return cmd.splitlines()[0]


def extract_system_prompt(llm_input: list) -> str:
    """Return the system prompt text from a step's (redacted) llm_input."""
    for msg in llm_input or []:
        if msg.get("role") == "system":
            c = msg.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "\n".join(
                    p.get("text", "") for p in c if isinstance(p, dict)
                )
    return ""


def coverage_class(pct: float) -> str:
    """entropy-style green/orange/red bucket for a coverage percentage."""
    if pct >= 60:
        return "cov-high"
    if pct >= 25:
        return "cov-mid"
    return "cov-low"


def coverage_sparkline(series: list) -> str:
    """Tiny inline SVG line chart of Total coverage % across steps."""
    pts = [p for p in series if p is not None]
    if len(pts) < 2:
        return ""
    w, h = 160, 28
    n = len(series)
    hi = max(pts) or 1.0
    coords = []
    for i, p in enumerate(series):
        if p is None:
            continue
        x = round(i / (n - 1) * w, 1) if n > 1 else 0
        y = round(h - (p / hi) * (h - 4) - 2, 1)
        coords.append(f"{x},{y}")
    return (
        f'<svg class="sparkline" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
        f'<polyline points="{" ".join(coords)}" fill="none" '
        f'stroke="currentColor" stroke-width="1.5"/></svg>'
    )


def _wall_time_from_trace(trace: list) -> float:
    """Best-effort: first→last step timestamp delta in seconds."""
    from datetime import datetime
    ts = [s.get("timestamp") for s in trace if s.get("timestamp")]
    if len(ts) < 2:
        return 0
    try:
        return (datetime.fromisoformat(ts[-1]) -
                datetime.fromisoformat(ts[0])).total_seconds()
    except (ValueError, TypeError):
        return 0


def generate_html(session: dict, *, model_override: str = "",
                  label: str = "") -> str:
    """Generate a self-contained HTML trace viewer."""
    trace = session["trace"]
    meta = session["metadata"]
    task_info = session.get("task_info", {})

    task = meta.get("task", "Unknown task")
    model = (
        model_override
        or meta.get("upstream_model")
        or meta.get("model", "Unknown model")
    )
    outcome = meta.get("outcome", "unknown")
    total_steps = meta.get("steps", len(trace)) or len(
        [s for s in trace if s.get("step")]
    )
    wall_time = meta.get("wall_time_sec") or _wall_time_from_trace(trace)

    # Build evaluation info panel
    eval_html = ""
    if task_info:
        ref = task_info.get("reference_answers", {})
        agent_result = task_info.get("agent_result", "")
        passed = task_info.get("passed", False)
        eval_types = task_info.get("eval_types", [])
        pass_class = "eval-pass" if passed else "eval-fail"
        pass_text = "PASS" if passed else "FAIL"

        ref_parts = []
        must_include = ref.get("must_include", [])
        if must_include:
            mi_strs = []
            for item in must_include:
                if isinstance(item, str):
                    mi_strs.append(html.escape(item))
                elif isinstance(item, list):
                    alts = [html.escape(str(a)) for a in item]
                    mi_strs.append(" or ".join(alts))
            ref_parts.append(f"<span class='label'>Must include:</span> "
                           f"{' · '.join(mi_strs)}")
        exact = ref.get("exact_match", "N/A")
        if isinstance(exact, str) and exact != "N/A":
            ref_parts.append(f"<span class='label'>Exact match:</span> "
                           f"{html.escape(exact)}")
        elif isinstance(exact, list):
            alts = [html.escape(str(a)) for a in exact]
            ref_parts.append(f"<span class='label'>Exact match (one of):</span> "
                           f"{' or '.join(alts)}")
        fuzzy = ref.get("fuzzy_match", [])
        if fuzzy:
            ref_parts.append(f"<span class='label'>Fuzzy match:</span> "
                           f"{html.escape(', '.join(str(x) for x in fuzzy))}")

        eval_html = f"""
        <div class="eval-panel {pass_class}">
          <div class="eval-header">
            <span class="badge {'pass' if passed else 'fail'}">{pass_text}</span>
            <span class="eval-type">{html.escape(', '.join(eval_types))}</span>
          </div>
          <div class="eval-body">
            <div class="eval-row">
              <span class="label">Expected:</span>
              <span class="eval-value">{' · '.join(ref_parts) if ref_parts else '<em>no reference</em>'}</span>
            </div>
            <div class="eval-row">
              <span class="label">Agent returned:</span>
              <span class="eval-value agent-result">{html.escape(str(agent_result or '(none)'))}</span>
            </div>
          </div>
        </div>"""

    steps_html = []
    total_prompt_tokens = 0
    total_completion_tokens = 0
    coverage_series = []  # Total coverage % per step (None where absent)

    for i, step in enumerate(trace):
        if step.get("event") == "task_complete":
            steps_html.append(f"""
            <div class="step completion">
              <div class="step-header">
                <span class="badge done">DONE</span>
                <span class="step-title">Task Complete</span>
              </div>
              <div class="step-body">
                <div class="field"><span class="label">Summary:</span> {html.escape(str(step.get('summary', '')))}</div>
                <div class="field"><span class="label">Result:</span> <code>{html.escape(str(step.get('result', '')))}</code></div>
              </div>
            </div>""")
            continue

        step_num = step.get("step", i + 1)
        screenshot_b64 = step.get("_screenshot_b64", "")
        reply = step.get("llm_reply", "")
        command = step.get("command", "")
        output = step.get("output", "")
        usage = step.get("usage", {})
        prompt_text = extract_prompt_text(step.get("llm_input", []))

        thinking, action = extract_think(reply)

        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        elapsed = usage.get("elapsed_sec", 0)
        total_prompt_tokens += pt
        total_completion_tokens += ct
        tok_s = f"{ct / elapsed:.1f}" if elapsed > 0 else "—"

        # Entropy stats
        entropy = usage.get("entropy", {})
        mean_h = entropy.get("mean_entropy", 0)
        max_h = entropy.get("max_entropy", 0)
        high_h_count = entropy.get("high_entropy_count", 0)
        low_conf = entropy.get("low_confidence_tokens", [])

        # Color-code entropy: green (<0.5), yellow (0.5-1.5), red (>1.5)
        if mean_h > 0:
            if mean_h < 0.5:
                h_class = "entropy-low"
            elif mean_h < 1.5:
                h_class = "entropy-mid"
            else:
                h_class = "entropy-high"
            entropy_badge = (f'<span class="entropy-badge {h_class}" '
                           f'title="mean={mean_h:.2f} max={max_h:.2f} '
                           f'high={high_h_count}">'
                           f'H={mean_h:.2f}</span>')
        else:
            entropy_badge = ""

        # Low-confidence tokens section
        low_conf_html = ""
        if low_conf:
            lc_items = " ".join(
                f'<span class="lc-token" title="entropy={t["entropy"]}, '
                f'p1={t["top1_prob"]}">{html.escape(t["token"])}'
                f'<sub>{t["entropy"]:.1f}</sub></span>'
                for t in low_conf
            )
            low_conf_html = (f'<details class="section">'
                           f'<summary>Uncertain tokens ({len(low_conf)})</summary>'
                           f'<div class="lc-tokens">{lc_items}</div></details>')

        # Extract the dynamic blocks the agent appends to each prompt.
        a11y = extract_section(prompt_text, "Accessibility tree:", _BLOCK_MARKERS)
        plan = extract_section(prompt_text, "Current plan:", _BLOCK_MARKERS)
        coverage = extract_section(prompt_text, "Coverage status:", _BLOCK_MARKERS)

        # Coverage badge — Total % from this step's coverage snapshot.
        cov_pcts = parse_coverage(coverage)
        cov_total = cov_pcts.get("total")
        coverage_series.append(cov_total)
        if cov_total is not None:
            cov_badge = (f'<span class="cov-badge {coverage_class(cov_total)}" '
                         f'title="JS {cov_pcts.get("js", 0)}% · '
                         f'CSS {cov_pcts.get("css", 0)}%">'
                         f'{cov_total:.1f}%</span>')
        else:
            cov_badge = ""

        system_prompt = extract_system_prompt(step.get("llm_input", []))
        step_title = make_step_title(command, output)

        img_tag = ""
        if screenshot_b64:
            img_tag = f'<img src="data:image/png;base64,{screenshot_b64}" class="screenshot" loading="lazy">'

        sys_html = ""
        if system_prompt and system_prompt != "[system prompt]":
            sys_html = ('<details class="section"><summary>System prompt</summary>'
                        '<pre class="sysprompt">' + html.escape(system_prompt)
                        + '</pre></details>')
        elif system_prompt == "[system prompt]":
            sys_html = ('<div class="section"><div class="label">System prompt</div>'
                        '<pre class="output">Not captured in this trace — re-run '
                        'with the updated audit plugin to record it.</pre></div>')

        user_html = ""
        if prompt_text:
            user_html = ('<details class="section"><summary>User prompt (full)</summary>'
                         '<pre class="userprompt">' + html.escape(prompt_text)
                         + '</pre></details>')

        steps_html.append(f"""
        <div class="step" id="step-{step_num}">
          <div class="step-header" onclick="this.parentElement.classList.toggle('collapsed')">
            <span class="badge step-num">Step {step_num}</span>
            <span class="step-title">{html.escape(step_title)}</span>
            {cov_badge}
            <span class="tokens">{pt:,}→{ct:,} ({tok_s} tok/s)</span>
            {entropy_badge}
            <span class="chevron">▾</span>
          </div>
          <div class="step-body">
            <div class="columns">
              <div class="col-left">
                {img_tag}
              </div>
              <div class="col-right">
                {'<details class="section" open><summary>Reasoning</summary><pre class="think">' + html.escape(thinking) + '</pre></details>' if thinking else ''}
                <div class="section">
                  <div class="label">Command</div>
                  <pre class="command">{html.escape(command)}</pre>
                </div>
                {'<details class="section"><summary>Output</summary><pre class="output">' + html.escape(output) + '</pre></details>' if output else ''}
                {'<details class="section" open><summary>Coverage</summary><pre class="coverage">' + html.escape(coverage) + '</pre></details>' if coverage else ''}
                {low_conf_html}
                {'<details class="section"><summary>Plan</summary><pre class="plan">' + html.escape(plan) + '</pre></details>' if plan else ''}
                {'<details class="section"><summary>Accessibility Tree</summary><pre class="a11y">' + html.escape(a11y) + '</pre></details>' if a11y else ''}
                {user_html}
                {sys_html}
              </div>
            </div>
          </div>
        </div>""")

    outcome_class = "pass" if outcome == "completed" else "fail"

    # Coverage summary for the header (final % + sparkline).
    # Coverage is cumulative per session, so peak == final by construction.
    cov_vals = [c for c in coverage_series if c is not None]
    cov_header = ""
    if cov_vals:
        cov_header = (
            f'<span>Coverage <span class="val">{cov_vals[-1]:.1f}%</span> '
            f'{coverage_sparkline(coverage_series)}</span>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trace: {html.escape(task[:60])}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=DM+Sans:wght@400;500;600&display=swap');

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

:root {{
  --bg: #0f1117;
  --surface: #181a20;
  --surface2: #1e2028;
  --border: #2a2d38;
  --text: #c9cdd6;
  --text-dim: #6b7080;
  --text-bright: #e8eaf0;
  --accent: #5b9cf5;
  --green: #4ade80;
  --red: #f87171;
  --orange: #fb923c;
  --mono: 'JetBrains Mono', monospace;
  --sans: 'DM Sans', system-ui, sans-serif;
}}

body {{
  font-family: var(--sans);
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  padding: 0;
}}

.header {{
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 24px 32px;
  position: sticky;
  top: 0;
  z-index: 100;
}}

.header h1 {{
  font-size: 15px;
  font-weight: 600;
  color: var(--text-bright);
  margin-bottom: 8px;
  font-family: var(--mono);
}}

.header .meta {{
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  font-size: 13px;
  color: var(--text-dim);
}}

.header .meta span {{
  display: flex;
  align-items: center;
  gap: 6px;
}}

.header .meta .val {{
  color: var(--text);
  font-family: var(--mono);
  font-size: 12px;
}}

.nav {{
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 8px 32px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  position: sticky;
  top: 80px;
  z-index: 99;
}}

.nav a {{
  display: inline-block;
  padding: 4px 10px;
  border-radius: 4px;
  font-size: 12px;
  font-family: var(--mono);
  color: var(--text-dim);
  text-decoration: none;
  background: var(--surface2);
  border: 1px solid var(--border);
  transition: all 0.15s;
}}

.nav a:hover {{
  color: var(--accent);
  border-color: var(--accent);
}}

.container {{
  max-width: 1400px;
  margin: 0 auto;
  padding: 16px 32px 64px;
}}

.step {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 12px;
  overflow: hidden;
}}

.step.collapsed .step-body {{ display: none; }}
.step.collapsed .chevron {{ transform: rotate(-90deg); }}

.step-header {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  cursor: pointer;
  user-select: none;
  transition: background 0.15s;
}}

.step-header:hover {{
  background: var(--surface2);
}}

.badge {{
  display: inline-block;
  padding: 2px 10px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  font-family: var(--mono);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  flex-shrink: 0;
}}

.badge.step-num {{
  background: #1e293b;
  color: var(--accent);
  border: 1px solid #2d3a4f;
}}

.badge.done {{
  background: #052e16;
  color: var(--green);
  border: 1px solid #166534;
}}

.badge.pass {{ background: #052e16; color: var(--green); }}
.badge.fail {{ background: #2c0b0e; color: var(--red); }}

.step-title {{
  flex: 1;
  font-size: 13px;
  font-family: var(--mono);
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}

.tokens {{
  font-size: 11px;
  font-family: var(--mono);
  color: var(--text-dim);
  flex-shrink: 0;
}}

.chevron {{
  color: var(--text-dim);
  font-size: 14px;
  transition: transform 0.15s;
  flex-shrink: 0;
}}

.entropy-badge {{
  font-size: 10px;
  font-family: var(--mono);
  padding: 1px 6px;
  border-radius: 3px;
  flex-shrink: 0;
}}

.entropy-low {{
  background: #052e16;
  color: var(--green);
  border: 1px solid #166534;
}}

.entropy-mid {{
  background: #1c1a05;
  color: var(--orange);
  border: 1px solid #854d0e;
}}

.entropy-high {{
  background: #2c0b0e;
  color: var(--red);
  border: 1px solid #7f1d1d;
}}

.lc-tokens {{
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 0;
}}

.lc-token {{
  display: inline-block;
  font-family: var(--mono);
  font-size: 12px;
  background: #1e0505;
  color: var(--red);
  padding: 2px 6px;
  border-radius: 3px;
  border: 1px solid #7f1d1d;
  cursor: help;
}}

.lc-token sub {{
  font-size: 9px;
  color: var(--text-dim);
  margin-left: 2px;
}}

.step-body {{
  padding: 0 16px 16px;
}}

.columns {{
  display: grid;
  grid-template-columns: minmax(300px, 480px) 1fr;
  gap: 16px;
}}

@media (max-width: 900px) {{
  .columns {{ grid-template-columns: 1fr; }}
}}

.screenshot {{
  width: 100%;
  border-radius: 6px;
  border: 1px solid var(--border);
  cursor: zoom-in;
}}

.screenshot.zoomed {{
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  width: 100vw;
  height: 100vh;
  object-fit: contain;
  z-index: 200;
  background: rgba(0,0,0,0.9);
  border: none;
  border-radius: 0;
  cursor: zoom-out;
}}

.section {{
  margin-top: 10px;
}}

.section summary {{
  font-size: 12px;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  cursor: pointer;
  padding: 4px 0;
}}

.section .label {{
  font-size: 12px;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}}

pre {{
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  padding: 10px 12px;
  border-radius: 6px;
  max-height: 400px;
  overflow-y: auto;
}}

pre.think {{
  background: #1a1c24;
  color: #9ca3af;
  border: 1px solid var(--border);
}}

pre.command {{
  background: #0c1425;
  color: var(--accent);
  border: 1px solid #1e3050;
}}

pre.output {{
  background: #141618;
  color: #a0a8b8;
  border: 1px solid var(--border);
}}

pre.plan {{
  background: #14180e;
  color: #a3c47a;
  border: 1px solid #2a3a1e;
}}

pre.a11y {{
  background: #18141e;
  color: #b0a0c8;
  border: 1px solid #2a2440;
}}

pre.coverage {{
  background: #0c1a14;
  color: #7cd8b0;
  border: 1px solid #1e3a2e;
}}

pre.sysprompt {{
  background: #16141c;
  color: #9b94b0;
  border: 1px solid #2a2638;
}}

pre.userprompt {{
  background: #141618;
  color: #a0a8b8;
  border: 1px solid var(--border);
}}

.cov-badge {{
  font-size: 10px;
  font-family: var(--mono);
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 3px;
  flex-shrink: 0;
}}

.cov-high {{ background: #052e16; color: var(--green); border: 1px solid #166534; }}
.cov-mid  {{ background: #1c1a05; color: var(--orange); border: 1px solid #854d0e; }}
.cov-low  {{ background: #2c0b0e; color: var(--red); border: 1px solid #7f1d1d; }}

.cov-peak {{
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-dim);
}}

.sparkline {{
  color: var(--accent);
  vertical-align: middle;
  margin-left: 4px;
}}

.completion {{
  border-color: #166534;
}}

.completion .step-header {{
  background: #052e16;
}}

.field {{
  font-size: 13px;
  padding: 6px 0;
}}

.field .label {{
  display: inline;
  font-weight: 600;
  color: var(--text-dim);
}}

.field code {{
  font-family: var(--mono);
  font-size: 12px;
  background: var(--surface2);
  padding: 2px 6px;
  border-radius: 3px;
}}

.eval-panel {{
  margin: 0 32px;
  padding: 12px 16px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--surface);
}}

.eval-panel.eval-pass {{
  border-color: #166534;
  background: #051e10;
}}

.eval-panel.eval-fail {{
  border-color: #7f1d1d;
  background: #1e0505;
}}

.eval-header {{
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}}

.eval-type {{
  font-size: 12px;
  font-family: var(--mono);
  color: var(--text-dim);
}}

.eval-body {{
  font-size: 13px;
}}

.eval-row {{
  padding: 3px 0;
}}

.eval-row .label {{
  font-weight: 600;
  color: var(--text-dim);
  margin-right: 6px;
}}

.eval-value {{
  color: var(--text);
  font-family: var(--mono);
  font-size: 12px;
}}

.eval-value.agent-result {{
  color: var(--accent);
}}

.keyboard-hint {{
  text-align: center;
  padding: 12px;
  font-size: 12px;
  color: var(--text-dim);
}}

kbd {{
  display: inline-block;
  padding: 1px 6px;
  border: 1px solid var(--border);
  border-radius: 3px;
  font-family: var(--mono);
  font-size: 11px;
  background: var(--surface2);
}}
</style>
</head>
<body>

<div class="header">
  <h1>{html.escape(task[:120])}</h1>
  <div class="meta">
    <span>Model <span class="val">{html.escape(model)}</span></span>
    {f'<span>Variant <span class="val">{html.escape(label)}</span></span>' if label else ''}
    <span>Steps <span class="val">{total_steps}</span></span>
    <span>Outcome <span class="val {outcome_class}">{outcome}</span></span>
    <span>Time <span class="val">{int(wall_time)//60}m {int(wall_time)%60:02d}s</span></span>
    <span>Tokens <span class="val">{total_prompt_tokens + total_completion_tokens:,} ({total_prompt_tokens:,} in / {total_completion_tokens:,} out)</span></span>
    {cov_header}
  </div>
</div>

{eval_html}

<div class="nav">
  {''.join(f'<a href="#step-{s.get("step", i+1)}">{s.get("step", i+1)}</a>' for i, s in enumerate(trace) if s.get("step"))}
</div>

<div class="container">
  <div class="keyboard-hint">
    Click step headers to collapse/expand. Click screenshots to zoom. <kbd>J</kbd>/<kbd>K</kbd> to navigate.
  </div>
  {''.join(steps_html)}
</div>

<script>
// Screenshot zoom
document.addEventListener('click', e => {{
  if (e.target.classList.contains('screenshot')) {{
    e.target.classList.toggle('zoomed');
  }}
}});

// Keyboard navigation
let currentStep = 0;
const steps = document.querySelectorAll('.step[id]');
document.addEventListener('keydown', e => {{
  if (e.key === 'j' || e.key === 'ArrowDown') {{
    e.preventDefault();
    currentStep = Math.min(currentStep + 1, steps.length - 1);
    steps[currentStep].scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  }} else if (e.key === 'k' || e.key === 'ArrowUp') {{
    e.preventDefault();
    currentStep = Math.max(currentStep - 1, 0);
    steps[currentStep].scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  }} else if (e.key === 'c') {{
    // Collapse/expand current
    steps[currentStep]?.classList.toggle('collapsed');
  }} else if (e.key === 'Escape') {{
    document.querySelector('.screenshot.zoomed')?.classList.remove('zoomed');
  }}
}});
</script>

</body>
</html>"""


def process_path(input_path: Path, output: str | None = None, *,
                 model_override: str = "", label: str = "") -> str | None:
    """Process a single path, return output filepath or None."""
    session = find_session_dir(input_path)
    if not session:
        print(f"  ⚠ No audit session found in {input_path}", file=sys.stderr)
        return None

    data = load_session(session)
    if not data["trace"]:
        print(f"  ⚠ Empty trace in {session}", file=sys.stderr)
        return None

    html_content = generate_html(
        data, model_override=model_override, label=label
    )

    if output:
        out_path = output
    else:
        out_path = str(session / "trace.html")

    with open(out_path, "w") as f:
        f.write(html_content)

    steps = len([s for s in data["trace"] if s.get("step")])
    print(f"  ✓ {out_path} ({steps} steps)")
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate HTML trace viewer from audit data"
    )
    parser.add_argument(
        "path",
        help="Session directory, task directory, or results directory (with --all)",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output HTML file path (default: trace.html in session dir)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all task directories under the given path",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Override the model name shown in the header "
             "(metadata.json usually only records the CUA_MODEL alias).",
    )
    parser.add_argument(
        "--label",
        default="",
        help="Short variant label shown next to the model, e.g. "
             "'with reasoning' / 'without reasoning'.",
    )
    args = parser.parse_args()

    input_path = Path(args.path)

    if args.all:
        # Find all task directories with audit data
        count = 0
        for task_dir in sorted(input_path.iterdir()):
            if task_dir.is_dir() and task_dir.name.isdigit():
                result = process_path(
                    task_dir,
                    model_override=args.model,
                    label=args.label,
                )
                if result:
                    count += 1
        if count:
            print(f"\nGenerated {count} trace viewer(s)")
        else:
            print("No audit sessions found", file=sys.stderr)
            sys.exit(1)
    else:
        result = process_path(
            input_path, args.output,
            model_override=args.model, label=args.label,
        )
        if not result:
            sys.exit(1)


if __name__ == "__main__":
    main()
