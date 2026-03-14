"""Shared configuration for XMPP CUA tools."""

import os
import tempfile
from pathlib import Path

# ── Directories ──────────────────────────────────────────────
SPOOL_DIR = Path(os.environ.get("XMPP_SPOOL_DIR", "/var/xmpp"))
INBOX_DIR = SPOOL_DIR / "inbox"
OUTBOX_DIR = SPOOL_DIR / "outbox"
PID_FILE = SPOOL_DIR / "daemon.pid"
STATUS_FILE = SPOOL_DIR / "daemon.status"

# ── Connection ───────────────────────────────────────────────
XMPP_JID = os.environ.get("XMPP_JID", "agent@localhost")
XMPP_PASSWORD = os.environ.get("XMPP_PASSWORD", "")
XMPP_HOST = os.environ.get("XMPP_HOST", "")  # empty = derive from JID
XMPP_PORT = int(os.environ.get("XMPP_PORT", "5222"))
XMPP_VERIFY_CERT = os.environ.get("XMPP_VERIFY_CERT", "false").lower() == "true"

# ── Peer (the human on the phone) ────────────────────────────
XMPP_PEER_JID = os.environ.get("XMPP_PEER_JID", "user@localhost")

# ── Markers (scanned by the agent loop) ──────────────────────
MSG_RECEIVED_MARKER = "@@XMPP_MSG_RECEIVED@@"
MSG_SENT_MARKER = "@@XMPP_MSG_SENT@@"
MSG_EMPTY_MARKER = "@@XMPP_INBOX_EMPTY@@"
DAEMON_STATUS_MARKER = "@@XMPP_DAEMON_STATUS@@"
WAITING_MARKER = "@@XMPP_WAITING@@"


def ensure_dirs() -> None:
    """Create spool directories if they don't exist."""
    for d in (SPOOL_DIR, INBOX_DIR, OUTBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)


def atomic_write(target: Path, data: str) -> None:
    """Write *data* to *target* atomically.

    Writes to a temporary file in the same directory, flushes + fsyncs,
    then renames into place.  os.rename() is atomic on the same
    filesystem on Linux, so readers either see the complete file or
    don't see it at all.  This is the classic Maildir / spool pattern.
    """
    dir_ = target.parent
    fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, target)
    except BaseException:
        # Clean up the temp file on any failure
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
