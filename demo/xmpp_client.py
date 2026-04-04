#!/usr/bin/env python3
"""Minimal XMPP chat client for demo recordings.

Uses raw sockets + STARTTLS instead of slixmpp to avoid
library compatibility issues with self-signed certificates.

Usage:
  python3 demo/xmpp_client.py \
    --jid user@cua.local \
    --password user-secret \
    --to agent@cua.local \
    --host 127.0.0.1 \
    --message "your task" \
    --wait 120
"""

import argparse
import base64
import hashlib
import os
import re
import select
import signal
import socket
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime

# ── Terminal colors ──
BLUE = "\033[1;34m"
GREEN = "\033[1;32m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"


def timestamp():
    return datetime.now().strftime("%H:%M:%S")


def print_sent(msg):
    print(f"\n{DIM}{timestamp()}{RESET} {BLUE}You:{RESET}")
    for line in msg.split("\n"):
        print(f"  {line}")
    print()
    sys.stdout.flush()


def print_received(msg):
    print(f"\n{DIM}{timestamp()}{RESET} {GREEN}Agent:{RESET}")
    for line in msg.split("\n"):
        print(f"  {line}")
    print()
    sys.stdout.flush()


def print_status(msg):
    print(f"  {DIM}[{msg}]{RESET}")
    sys.stdout.flush()


def recv_all(sock, timeout=5):
    """Read all available data from socket."""
    data = b""
    sock.settimeout(timeout)
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            # Check if there's more data ready
            if not select.select([sock], [], [], 0.1)[0]:
                break
    except (socket.timeout, ssl.SSLError):
        pass
    return data.decode("utf-8", errors="replace")


def send_xml(sock, xml_str):
    """Send XML string."""
    sock.sendall(xml_str.encode("utf-8"))


def extract_bodies(xml_data):
    """Extract message bodies from XML data (handles stream fragments)."""
    bodies = []
    # Wrap in a root element to parse fragments
    wrapped = f"<root xmlns='jabber:client' xmlns:stream='http://etherx.jabber.org/streams'>{xml_data}</root>"
    try:
        root = ET.fromstring(wrapped)
        for msg in root.iter("{jabber:client}message"):
            body = msg.find("{jabber:client}body")
            if body is not None and body.text:
                bodies.append(body.text)
    except ET.ParseError:
        # Try regex fallback for malformed XML
        for match in re.finditer(r"<body>(.*?)</body>", xml_data, re.DOTALL):
            bodies.append(match.group(1))
    return bodies


def main():
    parser = argparse.ArgumentParser(description="XMPP demo client")
    parser.add_argument("--jid", required=True, help="User JID (user@domain)")
    parser.add_argument("--password", required=True)
    parser.add_argument("--to", required=True, help="Agent JID")
    parser.add_argument("--message", required=True, help="Task message")
    parser.add_argument("--wait", type=int, default=180, help="Wait time in seconds")
    parser.add_argument("--host", default=None, help="XMPP server host")
    parser.add_argument("--port", type=int, default=5222)
    args = parser.parse_args()

    user, domain = args.jid.split("@", 1)
    host = args.host or domain

    print(f"\n{BOLD}╭─────────────────────────────────────────╮{RESET}")
    print(f"{BOLD}│         CUA Agent — XMPP Demo           │{RESET}")
    print(f"{BOLD}╰─────────────────────────────────────────╯{RESET}\n")

    # ── Connect ──
    print_status(f"Connecting to {host}:{args.port}...")
    try:
        sock = socket.create_connection((host, args.port), timeout=10)
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        print_status(f"Connection failed: {e}")
        sys.exit(1)

    # ── Stream + STARTTLS ──
    stream_header = (
        f'<stream:stream xmlns="jabber:client" '
        f'xmlns:stream="http://etherx.jabber.org/streams" '
        f'to="{domain}" version="1.0">'
    )
    send_xml(sock, stream_header)
    features = recv_all(sock, timeout=3)

    if "starttls" not in features:
        print_status("Server doesn't support STARTTLS")
        sock.close()
        sys.exit(1)

    send_xml(sock, '<starttls xmlns="urn:ietf:params:xml:ns:xmpp-tls"/>')
    proceed = recv_all(sock, timeout=3)
    if "proceed" not in proceed:
        print_status(f"STARTTLS rejected: {proceed[:200]}")
        sock.close()
        sys.exit(1)

    # ── TLS upgrade ──
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock = ctx.wrap_socket(sock, server_hostname=domain)
    print_status("TLS established")

    # ── Re-open stream after TLS ──
    send_xml(sock, stream_header)
    features = recv_all(sock, timeout=3)

    # ── SASL PLAIN auth ──
    if "PLAIN" not in features:
        print_status("Server doesn't support PLAIN auth")
        sock.close()
        sys.exit(1)

    auth_str = base64.b64encode(
        f"\x00{user}\x00{args.password}".encode("utf-8")
    ).decode("ascii")
    send_xml(sock, f'<auth xmlns="urn:ietf:params:xml:ns:xmpp-sasl" mechanism="PLAIN">{auth_str}</auth>')
    auth_resp = recv_all(sock, timeout=5)

    if "success" not in auth_resp:
        print_status(f"Authentication failed: {auth_resp[:200]}")
        sock.close()
        sys.exit(1)
    print_status(f"Authenticated as {args.jid}")

    # ── Bind resource ──
    send_xml(sock, stream_header)
    recv_all(sock, timeout=3)  # features

    resource = f"demo-{os.getpid()}"
    send_xml(sock, (
        f'<iq type="set" id="bind1">'
        f'<bind xmlns="urn:ietf:params:xml:ns:xmpp-bind">'
        f'<resource>{resource}</resource>'
        f'</bind></iq>'
    ))
    bind_resp = recv_all(sock, timeout=3)
    print_status(f"Bound resource: {resource}")

    # ── Send presence ──
    send_xml(sock, '<presence/>')
    recv_all(sock, timeout=2)

    # ── Send message ──
    msg_id = hashlib.md5(args.message[:20].encode()).hexdigest()[:8]
    send_xml(sock, (
        f'<message to="{args.to}" type="chat" id="{msg_id}">'
        f'<body>{args.message}</body>'
        f'</message>'
    ))
    print_sent(args.message)

    # ── Wait for responses ──
    print_status(f"Waiting up to {args.wait}s for response...")
    start = time.monotonic()
    got_response = False
    task_complete = False
    joined_rooms = set()

    # Handle Ctrl+C gracefully
    interrupted = False
    def handle_sigint(sig, frame):
        nonlocal interrupted
        interrupted = True
    signal.signal(signal.SIGINT, handle_sigint)

    def join_muc(room_jid):
        """Join a MUC room to receive progress updates (no history)."""
        if room_jid in joined_rooms:
            return
        nick = user
        print_status(f"Joining room: {room_jid}")
        send_xml(sock, (
            f'<presence to="{room_jid}/{nick}">'
            f'<x xmlns="http://jabber.org/protocol/muc">'
            f'<history maxstanzas="0"/>'
            f'</x>'
            f'</presence>'
        ))
        joined_rooms.add(room_jid)

    def process_data(data):
        """Process received XML data — extract messages, handle MUC invitations."""
        nonlocal got_response, task_complete

        # Check for MUC invitations (direct or mediated)
        for match in re.finditer(
            r'<x[^>]*xmlns=["\']jabber:x:conference["\'][^>]*jid=["\']([^"\']+)["\']',
            data
        ):
            join_muc(match.group(1))

        # Also check for mediated invitations
        for match in re.finditer(
            r'<invite[^>]*from=["\']([^"\']+)["\'].*?</invite>',
            data, re.DOTALL
        ):
            # The room JID is in the 'from' of the parent message
            room_match = re.search(
                r'<message[^>]*from=["\']([^"\']+)["\'].*?' + re.escape(match.group(0)),
                data, re.DOTALL
            )
            if room_match:
                room_jid = room_match.group(1).split("/")[0]
                join_muc(room_jid)

        # Extract message bodies
        bodies = extract_bodies(data)
        for body in bodies:
            # Skip MUC join/leave messages and subject messages
            if not body.strip():
                continue

            elapsed = time.monotonic() - start
            print_received(body)
            print_status(f"Response received in {elapsed:.1f}s")
            got_response = True

            # Check for task completion markers from the orchestrator.
            # Orchestrator format: "✅ #1: ...", "⚠️ #1: ...", "❌ #1: ...", "🛑 #1: ..."
            # Don't match step output like "⚠️ Output: JS Error..."
            if "── Task complete ──" in body or \
               re.match(r'^(?:✅|⚠️|❌|🛑)\s*#\d+:', body):
                task_complete = True

    while time.monotonic() - start < args.wait and not interrupted and not task_complete:
        try:
            ready = select.select([sock], [], [], 2.0)
            if ready[0]:
                data = recv_all(sock, timeout=1)
                if data:
                    process_data(data)
        except (ssl.SSLError, socket.error) as e:
            # Don't break on transient SSL errors
            if "WANT_READ" in str(e) or "timed out" in str(e):
                continue
            print_status(f"Connection error: {e}")
            break

    if task_complete:
        elapsed = time.monotonic() - start
        print_status(f"Task completed in {elapsed:.1f}s")
    elif not got_response and not interrupted:
        print_status(f"Timeout after {args.wait}s — no response")
    elif interrupted:
        print_status("Interrupted")

    # ── Cleanup ──
    try:
        send_xml(sock, '</stream:stream>')
        sock.close()
    except Exception:
        pass

    print_status("Done")
    print()


if __name__ == "__main__":
    main()
