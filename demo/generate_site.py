#!/usr/bin/env python3
"""Generate a GitHub Pages demo showcase from demo output.

Processes demo results (videos, screenshots, logs, traces) into
a polished static site suitable for docs/ in a GitHub repo.

Usage:
  python3 demo/generate_site.py ./demo_out --output docs/
  python3 demo/generate_site.py ./demo_out -o docs/ --title "cualm demos"
"""

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from datetime import datetime


def find_demo_dirs(root: Path) -> dict:
    """Find demo output directories by type."""
    demos = {"webarena": None, "coverage": None, "wikipedia": None}
    for ts_dir in sorted(root.iterdir()):
        if not ts_dir.is_dir():
            continue
        for demo_type in demos:
            d = ts_dir / demo_type
            if d.is_dir():
                demos[demo_type] = d
    return demos


def strip_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def parse_xmpp_log(path: Path) -> list[dict]:
    """Parse XMPP chat log into structured messages."""
    text = strip_ansi(path.read_text())
    messages = []
    current = None

    for line in text.split('\n'):
        # Timestamp + sender
        m = re.match(r'^(\d{2}:\d{2}:\d{2})\s+(You|Agent):', line)
        if m:
            if current:
                messages.append(current)
            current = {
                "time": m.group(1),
                "sender": m.group(2),
                "text": "",
            }
            continue

        # Status line
        m = re.match(r'^\s*\[(.+)\]$', line)
        if m and not current:
            messages.append({"time": "", "sender": "system", "text": m.group(1)})
            continue
        if m and current:
            # Status after a message
            messages.append(current)
            current = None
            messages.append({"time": "", "sender": "system", "text": m.group(1)})
            continue

        # Content line
        if current and line.strip():
            if current["text"]:
                current["text"] += "\n"
            current["text"] += line.strip()

    if current:
        messages.append(current)
    return messages


def parse_coverage_trace(path: Path) -> list[dict]:
    """Parse coverage trace.json into step summaries."""
    with open(path) as f:
        trace = json.load(f)
    steps = []
    for s in trace:
        steps.append({
            "step": s.get("step", 0),
            "command": s.get("command", ""),
            "output": s.get("output", "")[:200],
            "screenshot": s.get("screenshot_file", ""),
        })
    return steps


def select_screenshots(screenshot_dir: Path, max_count: int = 12) -> list[Path]:
    """Select evenly-spaced screenshots from a directory."""
    pngs = sorted(screenshot_dir.glob("*.png"))
    if len(pngs) <= max_count:
        return pngs
    # Evenly space, always include first and last
    indices = [0]
    step = (len(pngs) - 1) / (max_count - 1)
    for i in range(1, max_count - 1):
        indices.append(round(i * step))
    indices.append(len(pngs) - 1)
    return [pngs[i] for i in indices]


def generate_html(demos: dict, title: str) -> str:
    """Generate the main showcase HTML."""

    # Parse data
    xmpp_messages = []
    if demos["wikipedia"]:
        log_path = demos["wikipedia"] / "xmpp_chat.log"
        if log_path.exists():
            xmpp_messages = parse_xmpp_log(log_path)

    coverage_meta = {}
    coverage_steps = []
    if demos["coverage"]:
        audit_dirs = sorted((demos["coverage"] / "audit").glob("session_*"))
        if audit_dirs:
            meta_path = audit_dirs[-1] / "metadata.json"
            trace_path = audit_dirs[-1] / "trace.json"
            if meta_path.exists():
                coverage_meta = json.loads(meta_path.read_text())
            if trace_path.exists():
                coverage_steps = parse_coverage_trace(trace_path)

    # Find last coverage step result from terminal.log
    coverage_result = ""
    if demos["coverage"]:
        log_path = demos["coverage"] / "terminal.log"
        if log_path.exists():
            log_text = strip_ansi(log_path.read_text())
            for line in log_text.split('\n'):
                if 'JS Coverage:' in line or 'Total:' in line:
                    coverage_result = line.strip()

    # Wikipedia result
    wiki_result = ""
    for msg in xmpp_messages:
        if msg["sender"] == "Agent" and "✅" in msg["text"]:
            wiki_result = msg["text"]
            break

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #0a0a0f;
  --surface: #12121a;
  --surface-2: #1a1a26;
  --border: #2a2a3a;
  --text: #e0e0ec;
  --text-dim: #8888a0;
  --accent: #6ee7b7;
  --accent-2: #38bdf8;
  --accent-3: #f472b6;
  --user-color: #38bdf8;
  --agent-color: #6ee7b7;
  --system-color: #666680;
  --font-body: 'DM Sans', system-ui, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  line-height: 1.6;
}}
.container {{ max-width: 1100px; margin: 0 auto; padding: 0 24px; }}

/* Header */
header {{
  padding: 80px 0 60px;
  text-align: center;
  border-bottom: 1px solid var(--border);
}}
header h1 {{
  font-size: 3rem;
  font-weight: 700;
  letter-spacing: -0.03em;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  margin-bottom: 12px;
}}
header p {{
  color: var(--text-dim);
  font-size: 1.15rem;
  max-width: 600px;
  margin: 0 auto;
}}

/* Nav */
nav {{
  display: flex;
  justify-content: center;
  gap: 8px;
  padding: 24px 0;
  position: sticky;
  top: 0;
  z-index: 100;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
}}
nav a {{
  color: var(--text-dim);
  text-decoration: none;
  font-family: var(--font-mono);
  font-size: 0.85rem;
  padding: 8px 20px;
  border-radius: 6px;
  border: 1px solid var(--border);
  transition: all 0.2s;
}}
nav a:hover, nav a.active {{
  color: var(--accent);
  border-color: var(--accent);
  background: rgba(110, 231, 183, 0.05);
}}

/* Sections */
section {{
  padding: 60px 0;
  border-bottom: 1px solid var(--border);
}}
section:last-child {{ border-bottom: none; }}
h2 {{
  font-size: 1.8rem;
  font-weight: 700;
  margin-bottom: 8px;
  letter-spacing: -0.02em;
}}
h2 .emoji {{ font-size: 1.5rem; margin-right: 8px; }}
.section-desc {{
  color: var(--text-dim);
  margin-bottom: 32px;
  font-size: 1.05rem;
}}
h3 {{
  font-size: 1.1rem;
  font-weight: 600;
  color: var(--accent);
  margin: 32px 0 12px;
  font-family: var(--font-mono);
}}

/* Result badge */
.result-badge {{
  display: inline-block;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 20px;
  margin: 16px 0 24px;
  font-family: var(--font-mono);
  font-size: 0.9rem;
}}
.result-badge.pass {{ border-color: var(--accent); background: rgba(110,231,183,0.08); }}
.result-badge.info {{ border-color: var(--accent-2); background: rgba(56,189,248,0.08); }}

/* Video */
.video-wrap {{
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid var(--border);
  background: #000;
  margin: 16px 0;
}}
.video-wrap video {{
  width: 100%;
  display: block;
}}
.video-label {{
  padding: 8px 16px;
  font-family: var(--font-mono);
  font-size: 0.8rem;
  color: var(--text-dim);
  background: var(--surface);
}}

/* Screenshot gallery */
.gallery {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
  margin: 16px 0;
}}
.gallery img {{
  width: 100%;
  border-radius: 8px;
  border: 1px solid var(--border);
  cursor: pointer;
  transition: transform 0.2s, border-color 0.2s;
}}
.gallery img:hover {{
  transform: scale(1.02);
  border-color: var(--accent);
}}
.gallery .caption {{
  font-family: var(--font-mono);
  font-size: 0.75rem;
  color: var(--text-dim);
  text-align: center;
  padding: 4px 0;
}}

/* Chat */
.chat {{
  background: var(--surface);
  border-radius: 12px;
  border: 1px solid var(--border);
  padding: 20px;
  max-height: 500px;
  overflow-y: auto;
  font-size: 0.9rem;
}}
.chat-msg {{ margin-bottom: 12px; }}
.chat-msg .sender {{
  font-family: var(--font-mono);
  font-size: 0.8rem;
  font-weight: 600;
  margin-bottom: 2px;
}}
.chat-msg .sender.user {{ color: var(--user-color); }}
.chat-msg .sender.agent {{ color: var(--agent-color); }}
.chat-msg .sender.system {{ color: var(--system-color); font-weight: 400; font-style: italic; }}
.chat-msg .body {{
  padding-left: 12px;
  border-left: 2px solid var(--border);
  white-space: pre-wrap;
  word-break: break-word;
}}
.chat-msg.system .body {{
  border-left: none;
  padding-left: 0;
  color: var(--system-color);
  font-style: italic;
}}

/* Steps */
.step-list {{
  background: var(--surface);
  border-radius: 12px;
  border: 1px solid var(--border);
  overflow: hidden;
}}
.step {{
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  font-family: var(--font-mono);
  font-size: 0.85rem;
}}
.step:last-child {{ border-bottom: none; }}
.step .step-num {{
  color: var(--accent);
  font-weight: 600;
  margin-right: 12px;
}}
.step .cmd {{ color: var(--text); }}

/* Lightbox */
.lightbox {{
  display: none;
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(0,0,0,0.92);
  align-items: center;
  justify-content: center;
  cursor: zoom-out;
}}
.lightbox.active {{ display: flex; }}
.lightbox img {{
  max-width: 95vw;
  max-height: 95vh;
  border-radius: 8px;
}}

/* Footer */
footer {{
  padding: 40px 0;
  text-align: center;
  color: var(--text-dim);
  font-size: 0.85rem;
}}
footer a {{ color: var(--accent); text-decoration: none; }}
</style>
</head>
<body>

<header>
  <div class="container">
    <h1>{title}</h1>
    <p>A model-agnostic Computer Use Agent controlling a Linux desktop via screenshot → LLM → command loop.</p>
  </div>
</header>

<nav>
  <div class="container" style="display:flex;gap:8px;justify-content:center;">
    {"<a href='#webarena'>WebArena</a>" if demos["webarena"] else ""}
    {"<a href='#coverage'>Coverage</a>" if demos["coverage"] else ""}
    {"<a href='#wikipedia'>Wikipedia</a>" if demos["wikipedia"] else ""}
    <a href='#architecture'>Architecture</a>
  </div>
</nav>

{"_WEBARENA_SECTION_" if demos["webarena"] else ""}
{"_COVERAGE_SECTION_" if demos["coverage"] else ""}
{"_WIKIPEDIA_SECTION_" if demos["wikipedia"] else ""}

<section id="architecture">
  <div class="container">
    <h2><span class="emoji">🏗️</span>Architecture</h2>
    <p class="section-desc">The agent runs inside Docker — Xvfb + Chromium + xdotool. Each step: take screenshot → send to LLM → parse command → execute → repeat.</p>
    <div class="step-list">
      <div class="step"><span class="step-num">1</span><span class="cmd">Screenshot via scrot → base64 PNG</span></div>
      <div class="step"><span class="step-num">2</span><span class="cmd">Accessibility tree via CDP → ref IDs for elements</span></div>
      <div class="step"><span class="step-num">3</span><span class="cmd">LLM sees screenshot + a11y tree → outputs shell command</span></div>
      <div class="step"><span class="step-num">4</span><span class="cmd">Agent executes: cua-click, cua-type, cua-pw, cua-cdp-js, etc.</span></div>
      <div class="step"><span class="step-num">5</span><span class="cmd">Loop until cua-done or max steps reached</span></div>
    </div>
  </div>
</section>

<footer>
  <div class="container">
    <p>Generated from demo output &mdash; <a href="https://github.com/user/cualm">cualm</a></p>
  </div>
</footer>

<div class="lightbox" id="lightbox" onclick="this.classList.remove('active')">
  <img id="lightbox-img" src="" alt="Screenshot">
</div>

<script>
function showLightbox(src) {{
  const lb = document.getElementById('lightbox');
  document.getElementById('lightbox-img').src = src;
  lb.classList.add('active');
}}
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') document.getElementById('lightbox').classList.remove('active');
}});
</script>

</body>
</html>"""


def build_webarena_section(demo_dir: Path) -> str:
    has_video = (demo_dir / "steps.mp4").exists()
    has_trace = (demo_dir / "trace.html").exists()
    has_trace_video = (demo_dir / "trace_scroll.mp4").exists()

    parts = ["""
<section id="webarena">
  <div class="container">
    <h2><span class="emoji">🏪</span>WebArena Benchmark</h2>
    <p class="section-desc">The agent solves real web tasks on self-hosted e-commerce, forum, and git platforms. 812 tasks across 5 sites.</p>
"""]

    if has_video:
        parts.append("""
    <h3>Agent Steps</h3>
    <div class="video-wrap">
      <video controls loop muted playsinline>
        <source src="webarena/steps.mp4" type="video/mp4">
      </video>
      <div class="video-label">Screenshot timelapse — each frame is one agent step</div>
    </div>
""")

    if has_trace_video:
        parts.append("""
    <h3>Trace Viewer</h3>
    <div class="video-wrap">
      <video controls loop muted playsinline>
        <source src="webarena/trace_scroll.mp4" type="video/mp4">
      </video>
      <div class="video-label">Interactive trace viewer walkthrough — step-by-step reasoning, commands, and outputs</div>
    </div>
""")

    if has_trace:
        parts.append("""
    <p style="margin-top:12px;"><a href="webarena/trace.html" style="color:var(--accent);font-family:var(--font-mono);font-size:0.9rem;">→ Open full interactive trace viewer</a></p>
""")

    parts.append("  </div>\n</section>")
    return "\n".join(parts)


def build_coverage_section(demo_dir: Path, coverage_steps: list, coverage_meta: dict) -> str:
    has_video = (demo_dir / "steps.mp4").exists()
    audit_dirs = sorted((demo_dir / "audit").glob("session_*")) if (demo_dir / "audit").exists() else []
    screenshots = []
    if audit_dirs:
        screenshots = sorted(audit_dirs[-1].glob("step_*.png"))

    model = coverage_meta.get("model", "unknown")

    parts = [f"""
<section id="coverage">
  <div class="container">
    <h2><span class="emoji">📊</span>Coverage Benchmark</h2>
    <p class="section-desc">The agent explores a web app to maximize JavaScript code coverage — clicking buttons, filling forms, navigating menus.</p>
    <div class="result-badge info">Model: {model} · {len(coverage_steps)} steps</div>
"""]

    if has_video:
        parts.append("""
    <h3>Agent Exploration</h3>
    <div class="video-wrap">
      <video controls loop muted playsinline>
        <source src="coverage/steps.mp4" type="video/mp4">
      </video>
      <div class="video-label">Timelapse of the agent exploring the web application</div>
    </div>
""")

    if coverage_steps:
        parts.append("    <h3>Steps</h3>\n    <div class='step-list'>")
        for s in coverage_steps:
            cmd = s["command"][:120].replace("<", "&lt;").replace(">", "&gt;")
            parts.append(f'      <div class="step"><span class="step-num">{s["step"]}</span><span class="cmd">{cmd}</span></div>')
        parts.append("    </div>")

    if screenshots:
        selected = select_screenshots_from_list(screenshots, 6)
        parts.append("    <h3>Screenshots</h3>\n    <div class='gallery'>")
        for png in selected:
            rel = f"coverage/audit/{png.parent.name}/{png.name}"
            parts.append(f'      <div><img src="{rel}" alt="Step" onclick="showLightbox(this.src)" loading="lazy"><div class="caption">{png.stem}</div></div>')
        parts.append("    </div>")

    parts.append("  </div>\n</section>")
    return "\n".join(parts)


def build_wikipedia_section(demo_dir: Path, xmpp_messages: list, wiki_result: str) -> str:
    has_video = (demo_dir / "steps.mp4").exists()
    screenshots_dir = demo_dir / "screenshots"
    screenshots = sorted(screenshots_dir.glob("*.png")) if screenshots_dir.exists() else []

    parts = ["""
<section id="wikipedia">
  <div class="container">
    <h2><span class="emoji">📱</span>Wikipedia via XMPP</h2>
    <p class="section-desc">Send a task from your phone via XMPP → orchestrator spawns an agent → result sent back as a chat message.</p>
"""]

    if wiki_result:
        safe = wiki_result.replace("<", "&lt;").replace(">", "&gt;")
        parts.append(f'    <div class="result-badge pass">{safe}</div>')

    if has_video:
        parts.append("""
    <h3>Agent Screen</h3>
    <div class="video-wrap">
      <video controls loop muted playsinline>
        <source src="wikipedia/steps.mp4" type="video/mp4">
      </video>
      <div class="video-label">What the agent sees while completing the task</div>
    </div>
""")

    if xmpp_messages:
        parts.append("    <h3>XMPP Conversation</h3>\n    <div class='chat'>")
        for msg in xmpp_messages:
            if msg["sender"] == "system":
                parts.append(f'      <div class="chat-msg system"><div class="body">{msg["text"]}</div></div>')
            else:
                cls = "user" if msg["sender"] == "You" else "agent"
                time_str = f'<span style="color:var(--text-dim);font-size:0.75rem;margin-left:8px;">{msg["time"]}</span>' if msg["time"] else ""
                body = msg["text"].replace("<", "&lt;").replace(">", "&gt;").replace("&amp;", "&")
                # Unescape XML entities that came through
                body = body.replace("&apos;", "'").replace("&quot;", '"')
                parts.append(f'      <div class="chat-msg"><div class="sender {cls}">{msg["sender"]}{time_str}</div><div class="body">{body}</div></div>')
        parts.append("    </div>")

    if screenshots:
        selected = select_screenshots_from_list(screenshots, 8)
        parts.append("    <h3>Agent Screenshots</h3>\n    <div class='gallery'>")
        for png in selected:
            rel = f"wikipedia/screenshots/{png.name}"
            parts.append(f'      <div><img src="{rel}" alt="Step" onclick="showLightbox(this.src)" loading="lazy"><div class="caption">{png.stem}</div></div>')
        parts.append("    </div>")

    parts.append("  </div>\n</section>")
    return "\n".join(parts)


def select_screenshots_from_list(pngs: list, max_count: int) -> list:
    if len(pngs) <= max_count:
        return pngs
    indices = [0]
    step = (len(pngs) - 1) / (max_count - 1)
    for i in range(1, max_count - 1):
        indices.append(round(i * step))
    indices.append(len(pngs) - 1)
    return [pngs[i] for i in sorted(set(indices))]


def main():
    parser = argparse.ArgumentParser(description="Generate demo showcase site")
    parser.add_argument("demo_dir", help="Demo output directory")
    parser.add_argument("-o", "--output", default="docs",
                        help="Output directory (default: docs/)")
    parser.add_argument("--title", default="cualm — Demo Showcase",
                        help="Page title")
    args = parser.parse_args()

    demo_root = Path(args.demo_dir)
    out_dir = Path(args.output)

    if not demo_root.exists():
        print(f"Error: {demo_root} not found")
        return 1

    demos = find_demo_dirs(demo_root)
    print(f"Found demos: {', '.join(k for k, v in demos.items() if v)}")

    # Parse data for HTML generation
    xmpp_messages = []
    wiki_result = ""
    if demos["wikipedia"]:
        log_path = demos["wikipedia"] / "xmpp_chat.log"
        if log_path.exists():
            xmpp_messages = parse_xmpp_log(log_path)
            for msg in xmpp_messages:
                if msg["sender"] == "Agent" and "✅" in msg["text"]:
                    wiki_result = msg["text"]
                    break

    coverage_meta = {}
    coverage_steps = []
    if demos["coverage"]:
        audit_dirs = sorted((demos["coverage"] / "audit").glob("session_*"))
        if audit_dirs:
            meta_path = audit_dirs[-1] / "metadata.json"
            trace_path = audit_dirs[-1] / "trace.json"
            if meta_path.exists():
                coverage_meta = json.loads(meta_path.read_text())
            if trace_path.exists():
                coverage_steps = parse_coverage_trace(trace_path)

    # Generate HTML
    html = generate_html(demos, args.title)

    # Build sections
    if demos["webarena"]:
        html = html.replace("_WEBARENA_SECTION_", build_webarena_section(demos["webarena"]))
    if demos["coverage"]:
        html = html.replace("_COVERAGE_SECTION_", build_coverage_section(
            demos["coverage"], coverage_steps, coverage_meta))
    if demos["wikipedia"]:
        html = html.replace("_WIKIPEDIA_SECTION_", build_wikipedia_section(
            demos["wikipedia"], xmpp_messages, wiki_result))

    # Create output directory
    out_dir.mkdir(parents=True, exist_ok=True)

    # Copy assets
    for demo_type, demo_dir in demos.items():
        if not demo_dir:
            continue
        dest = out_dir / demo_type
        dest.mkdir(parents=True, exist_ok=True)

        # Videos
        for vid in demo_dir.glob("*.mp4"):
            shutil.copy2(vid, dest / vid.name)
            print(f"  Copied {demo_type}/{vid.name}")

        # Trace HTML
        if (demo_dir / "trace.html").exists():
            shutil.copy2(demo_dir / "trace.html", dest / "trace.html")
            print(f"  Copied {demo_type}/trace.html")

        # Screenshots — only copy selected ones used in the gallery
        if demo_type == "wikipedia" and (demo_dir / "screenshots").exists():
            ss_dest = dest / "screenshots"
            ss_dest.mkdir(exist_ok=True)
            selected = select_screenshots_from_list(
                sorted((demo_dir / "screenshots").glob("*.png")), 8)
            for png in selected:
                shutil.copy2(png, ss_dest / png.name)
            print(f"  Copied {demo_type}/screenshots/ ({len(selected)} of {len(list((demo_dir / 'screenshots').glob('*.png')))} selected)")

        if demo_type == "coverage" and (demo_dir / "audit").exists():
            for session_dir in (demo_dir / "audit").glob("session_*"):
                audit_dest = dest / "audit" / session_dir.name
                audit_dest.mkdir(parents=True, exist_ok=True)
                selected = select_screenshots_from_list(
                    sorted(session_dir.glob("step_*.png")), 6)
                for png in selected:
                    shutil.copy2(png, audit_dest / png.name)
                print(f"  Copied {demo_type}/audit/{session_dir.name}/ ({len(selected)} of {len(list(session_dir.glob('step_*.png')))} selected)")

    # Write HTML
    (out_dir / "index.html").write_text(html)
    print(f"\n✓ Site generated: {out_dir}/index.html")
    print(f"  Enable GitHub Pages on docs/ to publish.")

    return 0


if __name__ == "__main__":
    exit(main() or 0)
