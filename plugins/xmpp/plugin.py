"""XMPP messaging plugin for CUA agents.

Enables the agent to send and receive XMPP messages via a set of
CLI tools that bridge to a background daemon.  The daemon maintains
a persistent connection to a Prosody (or any XMPP) server.

Requires:
  - slixmpp (pip install slixmpp)
  - A running XMPP server (see compose.d/xmpp/ for a Docker setup)

Environment variables:
  XMPP_JID             Agent's JID           (e.g. agent@cua.local)
  XMPP_PASSWORD         Agent's password
  XMPP_PEER_JID        Human user's JID      (e.g. user@cua.local)
  XMPP_HOST            Server hostname        (default: derived from JID)
  XMPP_PORT            Server port            (default: 5222)
  XMPP_VERIFY_CERT     Verify TLS cert        (default: false)
  XMPP_SPOOL_DIR       Inbox/outbox path      (default: /var/xmpp)
  XMPP_AUTO_START      Start daemon on load   (default: true)
"""

import os
import subprocess
import sys
from pathlib import Path

# Directory of this plugin (plugins/xmpp/)
_PLUGIN_DIR = Path(__file__).resolve().parent
_TOOLS_DIR = _PLUGIN_DIR / "tools"


def on_startup(ctx):
    """Add XMPP tools to PATH, start daemon, enrich system prompt."""

    # ── Make xmpp_tools importable for CLI tool scripts ──
    plugin_dir = str(_PLUGIN_DIR)
    os.environ["PYTHONPATH"] = (
        plugin_dir + os.pathsep + os.environ.get("PYTHONPATH", "")
    )

    # ── Add tool scripts to PATH ──
    tools_dir = str(_TOOLS_DIR)
    if tools_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = tools_dir + os.pathsep + os.environ.get("PATH", "")

    # ── Check slixmpp is installed ──
    try:
        import slixmpp  # noqa: F401
    except ImportError:
        print("   ⚠ XMPP plugin: slixmpp not installed. Installing...")
        req_file = _PLUGIN_DIR / "requirements.txt"
        subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "--break-system-packages", "-q", "-r", str(req_file)],
            check=True,
        )

    # ── Start the daemon (unless disabled) ──
    auto_start = os.environ.get("XMPP_AUTO_START", "true").lower()
    jid = os.environ.get("XMPP_JID", "")

    if auto_start == "true" and jid:
        print(f"   XMPP: starting daemon as {jid}")
        try:
            result = subprocess.run(
                [str(_TOOLS_DIR / "cua-xmpp-daemon"), "--bg"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                print(f"   XMPP: {result.stdout.strip()}")
            else:
                print(f"   ⚠ XMPP daemon start failed: {result.stderr.strip()}")
        except Exception as e:
            print(f"   ⚠ XMPP daemon start failed: {e}")
    elif not jid:
        print("   XMPP: no XMPP_JID set — daemon not started")

    # ── Tool descriptions are auto-discovered from PATH ──
    # (tool_discovery.py finds cua-xmpp-* and runs --help on each)

    # ── Add XMPP instructions to the system prompt ──
    peer = os.environ.get("XMPP_PEER_JID", "the phone user")
    ctx["extra_prompt"].append(
        f"You have XMPP messaging capabilities. A human user ({peer}) "
        f"can send you messages via their phone. Use cua-xmpp-recv or "
        f"cua-xmpp-wait to check for incoming messages. Use cua-xmpp-send "
        f"to report progress or results back to them. When waiting for "
        f"human input, prefer cua-xmpp-wait --timeout 120 over polling."
    )


def on_shutdown(ctx):
    """Stop the XMPP daemon."""
    try:
        subprocess.run(
            [str(_TOOLS_DIR / "cua-xmpp-stop")],
            capture_output=True, timeout=10,
        )
        print("   XMPP: daemon stopped")
    except Exception:
        pass  # best effort
