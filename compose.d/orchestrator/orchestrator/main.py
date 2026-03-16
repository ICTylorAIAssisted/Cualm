#!/usr/bin/env python3
"""CUA Orchestrator — XMPP bot that spawns agent containers per task.

Connects to Prosody and waits for messages from the phone user.
Each task gets a fresh agent container (clean Chromium, clean
calibration).  The result is sent back when the agent finishes.

Presence indicates state:
  - "chat"  (green)  → idle, ready for a task
  - "dnd"   (red)    → running a task
  - "away"  (orange) → waiting for user input (Phase 3)
"""

import asyncio
import base64
import json
import logging
import os
import queue
import re
import signal
import ssl
import time
from datetime import datetime, timezone

import docker
import slixmpp
from slixmpp.exceptions import IqError, IqTimeout

log = logging.getLogger("orchestrator")

# ── Configuration ────────────────────────────────────────────

XMPP_JID = os.environ.get("ORCH_XMPP_JID", "orchestrator@cua.local")
XMPP_PASSWORD = os.environ.get("ORCH_XMPP_PASSWORD", "orch-secret")
XMPP_HOST = os.environ.get("ORCH_XMPP_HOST", "prosody")
XMPP_PORT = int(os.environ.get("ORCH_XMPP_PORT", "5222"))
XMPP_VERIFY_CERT = os.environ.get("ORCH_XMPP_VERIFY_CERT", "false").lower() == "true"

# MUC (Multi-User Chat) domain for task rooms
MUC_DOMAIN = os.environ.get("ORCH_MUC_DOMAIN", "conference.cua.local")

# Who is allowed to send tasks
ALLOWED_USER = os.environ.get("ORCH_ALLOWED_USER", "user@cua.local")

# External host:port for rewriting file upload URLs so phones can reach them.
# Prosody generates internal URLs like http://cua.local:5280/file_share/...
# which aren't reachable from outside. This rewrites them to use the host IP.
# e.g. "192.168.50.198:5280" (host:port only, no http://)
_raw_host = os.environ.get("ORCH_HTTP_HOST", "")
# Strip scheme if accidentally included
HTTP_EXTERNAL_HOST = _raw_host.replace("http://", "").replace("https://", "").rstrip("/")

# Docker / agent settings
AGENT_IMAGE = os.environ.get("CUA_AGENT_IMAGE", "cua-agent")
DOCKER_NETWORK = os.environ.get("CUA_DOCKER_NETWORK", "")  # auto-detected if empty
AGENT_TIMEOUT = int(os.environ.get("CUA_AGENT_TIMEOUT", "600"))
CALIBRATION_VOLUME = os.environ.get("CUA_CALIBRATION_VOLUME", "")  # 10 min default

# LLM settings to pass through to spawned agents
LLM_ENV = {
    "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", "http://host.docker.internal:8000/v1"),
    "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "not-needed"),
    "CUA_MODEL": os.environ.get("CUA_MODEL", "your-model-name"),
}

# Done markers from agent.py
DONE_MARKER = "@@CUA_TASK_COMPLETE@@"


# ── Docker helpers ───────────────────────────────────────────

_docker_client = None


def get_docker() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        docker_host = os.environ.get("DOCKER_HOST", "")
        if docker_host:
            _docker_client = docker.DockerClient(base_url=docker_host)
        else:
            # The compose file always mounts the host socket to
            # /var/run/docker.sock regardless of whether the host
            # uses Docker or Podman.
            _docker_client = docker.DockerClient(
                base_url="unix:///var/run/docker.sock"
            )
    return _docker_client


def detect_image() -> str:
    """Verify the agent image exists and return its name."""
    client = get_docker()
    try:
        client.images.get(AGENT_IMAGE)
        return AGENT_IMAGE
    except docker.errors.ImageNotFound:
        raise RuntimeError(
            f"Agent image '{AGENT_IMAGE}' not found. "
            f"Run './run.sh build' first, or set CUA_AGENT_IMAGE."
        )


def detect_network() -> str:
    """Find the compose network to attach agent containers to."""
    if DOCKER_NETWORK:
        return DOCKER_NETWORK

    # Inspect our own container to find the network
    client = get_docker()
    hostname = os.environ.get("HOSTNAME", "")
    if hostname:
        try:
            own = client.containers.get(hostname)
            networks = list(own.attrs["NetworkSettings"]["Networks"].keys())
            if networks:
                net = networks[0]
                log.info("Auto-detected network: %s", net)
                return net
        except Exception as e:
            log.warning("Could not auto-detect network: %s", e)

    return "bridge"


def detect_host_ip() -> str:
    """Resolve the host IP that spawned containers should use.

    Podman's 'host-gateway' alias works in compose but not when
    spawning containers via the API.  We resolve the actual IP
    from our own container's view of host.docker.internal.
    """
    import socket
    try:
        ip = socket.gethostbyname("host.docker.internal")
        log.info("Host IP (from DNS): %s", ip)
        return ip
    except socket.gaierror:
        pass

    # Fallback: check the default gateway
    try:
        with open("/proc/net/route") as f:
            for line in f:
                fields = line.strip().split()
                if fields[1] == "00000000":  # default route
                    # Gateway is in hex, little-endian
                    gw_hex = fields[2]
                    gw = ".".join(
                        str(int(gw_hex[i : i + 2], 16))
                        for i in range(6, -1, -2)
                    )
                    log.info("Host IP (from gateway): %s", gw)
                    return gw
    except Exception:
        pass

    log.warning("Could not detect host IP — using 'host-gateway' (may not work)")
    return "host-gateway"


# ── Screenshot & upload helpers ──────────────────────────────


def take_container_screenshot(container) -> bytes | None:
    """Run cua-screenshot inside a container, return PNG bytes."""
    try:
        exit_code, output = container.exec_run(
            "cua-screenshot", demux=True,
        )
        if exit_code == 0 and output[0]:
            b64 = output[0].decode().strip()
            return base64.b64decode(b64)
    except Exception as e:
        log.warning("Screenshot exec failed: %s", e)
    return None


async def upload_and_send_image(
    bot: slixmpp.ClientXMPP,
    to_jid: str,
    png_bytes: bytes,
    caption: str = "",
    filename: str = "screenshot.png",
    msg_type: str = "chat",
) -> bool:
    """Upload an image via XEP-0363 and send the URL to the user.

    Uses slixmpp's upload_file() helper which handles discovery,
    slot request, and the HTTP PUT internally.  The PUT uses the
    internal URL (reachable inside Docker).  The GET URL sent to
    the phone is rewritten to use the external host if configured.
    """
    import tempfile
    import os

    # Write to a temp file since upload_file expects a path
    fd, tmp_path = tempfile.mkstemp(suffix=".png")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png_bytes)

        # upload_file handles: discover service → request slot → PUT file
        # Returns the GET URL (internal, e.g. http://cua.local:5280/...)
        get_url = await bot["xep_0363"].upload_file(
            tmp_path,
            size=len(png_bytes),
            content_type="image/png",
        )

        # Rewrite the URL for external access if configured
        if HTTP_EXTERNAL_HOST:
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(get_url)
            get_url = urlunparse(parsed._replace(netloc=HTTP_EXTERNAL_HOST))
            log.info("Rewrote URL for external access: %s", get_url)

        # Send caption as a separate message if present
        if caption:
            bot.send_message(mto=to_jid, mbody=caption, mtype=msg_type)

        # Body must be ONLY the URL for Conversations to render inline
        msg = bot.make_message(mto=to_jid, mtype=msg_type)
        msg["body"] = get_url
        msg["oob"]["url"] = get_url
        msg.send()

        log.info("Sent image via XEP-0363: %s", get_url)
        return True

    except Exception as e:
        log.warning("Image upload failed: %s", e)
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Task runner ──────────────────────────────────────────────


class TaskRunner:
    """Manages one agent container at a time."""

    def __init__(self):
        self.container = None
        self.task_text = ""
        self.requester_jid = ""
        self.start_time = 0.0
        self.step = 0
        self.last_llm_reply = ""
        self._cancel = False
        self._update_queue = queue.Queue()  # thread → async bridge

    @property
    def is_running(self) -> bool:
        return self.container is not None

    def cancel(self):
        """Signal the running task to stop."""
        self._cancel = True
        if self.container:
            try:
                self.container.kill()
                log.info("Killed container %s", self.container.short_id)
            except Exception as e:
                log.warning("Kill failed: %s", e)

    def _emit_update(self, kind: str, **kwargs):
        """Enqueue an update from the sync thread for async dispatch."""
        self._update_queue.put({"kind": kind, **kwargs})

    async def run(self, task: str, requester: str) -> dict:
        """Spawn an agent container and stream its output.

        Returns a dict with keys: outcome, summary, result, steps, elapsed.
        Runs Docker operations in a thread executor to avoid blocking asyncio.
        """
        self.task_text = task
        self.requester_jid = requester
        self.start_time = time.monotonic()
        self.step = 0
        self.last_llm_reply = ""
        self._cancel = False
        # Drain any stale updates
        while not self._update_queue.empty():
            try:
                self._update_queue.get_nowait()
            except queue.Empty:
                break

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._run_sync, task)

        # Cleanup
        self.container = None
        self.task_text = ""
        self.requester_jid = ""

        return result

    def _run_sync(self, task: str) -> dict:
        """Synchronous container lifecycle — runs in a thread."""
        client = get_docker()
        image = detect_image()
        network = detect_network()
        host_ip = detect_host_ip()
        name = f"cua-task-{int(time.time())}"

        env = {
            **LLM_ENV,
            "PYTHONUNBUFFERED": "1",
        }

        log.info("Spawning container %s for task: %s", name, task[:80])

        # Volumes to mount into the agent container
        volumes = {}
        if CALIBRATION_VOLUME:
            volumes[CALIBRATION_VOLUME] = {
                "bind": "/var/cua/calibration",
                "mode": "rw",
            }

        try:
            self.container = client.containers.run(
                image=image,
                command=["agent", task],
                name=name,
                environment=env,
                network=network,
                extra_hosts={"host.docker.internal": host_ip},
                volumes=volumes,
                detach=True,
                # Resource limits
                mem_limit="2g",
                shm_size="256m",  # for Chromium
            )
        except Exception as e:
            log.error("Failed to spawn container: %s", e)
            return {
                "outcome": "error",
                "summary": f"Failed to start agent: {e}",
                "result": None,
                "steps": 0,
                "elapsed": 0,
            }

        log.info("Container %s created — streaming output", self.container.short_id)

        # Stream stdout
        outcome = "max_steps"
        summary = "Task completed"
        task_result = None
        last_command = ""

        try:
            for chunk in self.container.logs(stream=True, follow=True):
                if self._cancel:
                    break

                for line in chunk.decode(errors="replace").splitlines():
                    line = line.rstrip()
                    if not line:
                        continue

                    # Track step number
                    if line.startswith("── Step"):
                        m = re.search(r"Step (\d+)", line)
                        if m:
                            self.step = int(m.group(1))
                            self._emit_update("step", step=self.step)

                    # Track last LLM reply (strip <think> tags)
                    elif "LLM →" in line:
                        raw = line.split("LLM →", 1)[1].strip()
                        # Remove <think>...</think> and show only the action part
                        clean = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL)
                        clean = re.sub(r"<think>.*", "", clean, flags=re.DOTALL).strip()
                        self.last_llm_reply = clean or raw[:200]

                    # Track commands
                    elif line.strip().startswith("Command:"):
                        last_command = line.split("Command:", 1)[1].strip()
                        # Emit a room update with the command
                        self._emit_update(
                            "command",
                            step=self.step,
                            command=last_command,
                        )

                    # Track errors
                    elif "⚠" in line or "error" in line.lower()[:20]:
                        self._emit_update("error", step=self.step, message=line.strip())

                    # Detect completion
                    elif line.startswith("✅ Done:"):
                        outcome = "completed"
                        summary = line[len("✅ Done:"):].strip()

                    elif line.strip().startswith("Result:"):
                        task_result = line.split("Result:", 1)[1].strip()

                    elif "Reached max steps" in line:
                        outcome = "max_steps"
                        summary = "Reached maximum steps without completing"

                    elif "📸 Screenshot included" in line:
                        self._emit_update("screenshot_included", step=self.step)

        except Exception as e:
            log.warning("Log streaming error: %s", e)
            if self._cancel:
                outcome = "cancelled"
                summary = "Task cancelled by user"

        # Read full done payload (may contain screenshot_b64)
        screenshot_b64 = None
        elapsed = time.monotonic() - self.start_time
        try:
            self.container.wait(timeout=10)
        except Exception:
            pass

        if outcome == "completed":
            try:
                # Use get_archive — works on stopped containers
                # (exec_run doesn't work after container exits)
                import io, tarfile
                stream, _ = self.container.get_archive("/tmp/cua_done.json")
                tar_bytes = b"".join(stream)
                tar = tarfile.open(fileobj=io.BytesIO(tar_bytes))
                member = tar.getmembers()[0]
                f = tar.extractfile(member)
                if f:
                    payload = json.loads(f.read().decode())
                    screenshot_b64 = payload.get("screenshot_b64")
                    summary = payload.get("summary", summary)
                    task_result = payload.get("result", task_result)
                    if screenshot_b64:
                        log.info("Done payload includes screenshot (%d chars b64)",
                                 len(screenshot_b64))
            except Exception as e:
                log.warning("Could not read done payload: %s", e)

        # Cleanup container
        try:
            self.container.remove(force=True)
        except Exception as e:
            log.warning("Container cleanup failed: %s", e)

        return {
            "outcome": outcome,
            "summary": summary,
            "result": task_result,
            "screenshot_b64": screenshot_b64,
            "steps": self.step,
            "elapsed": round(elapsed, 1),
        }


# ── XMPP Bot ────────────────────────────────────────────────


class OrchestratorBot(slixmpp.ClientXMPP):
    """XMPP bot that dispatches tasks to agent containers."""

    def __init__(self, jid, password, server_addr):
        super().__init__(jid, password)
        self._server_addr = server_addr
        self._runner = TaskRunner()
        self._queue = []          # list of {"id": int, "task": str, "sender": str}
        self._task_counter = 0    # monotonic task ID
        self._active_room = ""    # JID of the room for the current task
        self._task_rooms = {}     # task_id → room JID

        self.add_event_handler("session_start", self.on_start)
        self.add_event_handler("message", self.on_message)
        self.add_event_handler("groupchat_message", self.on_groupchat_message)
        self.add_event_handler("disconnected", self.on_disconnect)

        self.register_plugin("xep_0030")  # Service Discovery
        self.register_plugin("xep_0045")  # Multi-User Chat
        self.register_plugin("xep_0066")  # Out-of-Band Data (images)
        self.register_plugin("xep_0199")  # Ping
        self.register_plugin("xep_0363")  # HTTP File Upload

    async def on_start(self, _event):
        self.send_presence(pshow="chat", pstatus="Ready for tasks")
        await self.get_roster()
        log.info("Orchestrator online as %s", self.boundjid.bare)

    def on_disconnect(self, _event):
        log.warning("Disconnected — reconnecting in 5s")
        asyncio.ensure_future(self._reconnect())

    async def _reconnect(self):
        await asyncio.sleep(5)
        self.connect(self._server_addr)

    # ── MUC Room Management ──────────────────────────────────

    def _room_slug(self, task_id: int, task_text: str) -> str:
        """Generate a room JID slug from task ID and description."""
        # Sanitize task text into a short slug
        slug = re.sub(r"[^a-z0-9]+", "-", task_text.lower()[:40]).strip("-")
        slug = slug[:30]  # keep it short
        return f"task-{task_id}-{slug}" if slug else f"task-{task_id}"

    async def _create_task_room(self, task_id: int, task_text: str) -> str:
        """Create a MUC room for a task, join it, invite the user.

        Returns the room JID.
        """
        slug = self._room_slug(task_id, task_text)
        room_jid = f"{slug}@{MUC_DOMAIN}"

        try:
            # Join the room (creates it since it doesn't exist)
            await self["xep_0045"].join_muc(room_jid, "orchestrator")
            log.info("Joined room %s", room_jid)

            # Configure the room (send an empty form to accept defaults)
            try:
                form = await self["xep_0045"].get_room_config(room_jid)
                # Set room name to the task description
                form["values"]["muc#roomconfig_roomname"] = f"Task #{task_id}"
                form["values"]["muc#roomconfig_persistentroom"] = True
                form["values"]["muc#roomconfig_membersonly"] = True
                form["values"]["muc#roomconfig_whois"] = "anyone"
                await self["xep_0045"].set_room_config(room_jid, form)
            except Exception as e:
                log.debug("Room config failed (may be pre-configured): %s", e)

            # Set the room subject
            subject = task_text[:200]
            self.send_message(mto=room_jid, mbody="", msubject=subject, mtype="groupchat")

            # Invite the user
            self["xep_0045"].invite(room_jid, ALLOWED_USER,
                                    reason=f"Task #{task_id}: {task_text[:100]}")
            log.info("Invited %s to %s", ALLOWED_USER, room_jid)

        except Exception as e:
            log.warning("Room creation failed: %s", e)
            return ""

        self._task_rooms[task_id] = room_jid
        return room_jid

    def _post_to_room(self, room_jid: str, text: str):
        """Send a groupchat message to a room."""
        if room_jid:
            self.send_message(mto=room_jid, mbody=text, mtype="groupchat")

    async def _post_screenshot_to_room(self, room_jid: str, png_bytes: bytes, caption: str = ""):
        """Upload a screenshot and post it to a room."""
        if not room_jid:
            return
        ok = await upload_and_send_image(self, room_jid, png_bytes, caption,
                                          msg_type="groupchat")
        if not ok:
            self._post_to_room(room_jid, f"📸 {caption} (upload failed)")

    async def on_groupchat_message(self, msg):
        """Handle messages inside task rooms."""
        body = (msg["body"] or "").strip()
        if not body:
            return
        # Ignore our own messages
        if msg["mucnick"] == "orchestrator":
            return

        room_jid = str(msg["from"].bare)
        sender_nick = msg["mucnick"]

        # Only handle commands
        if not body.startswith("/"):
            return

        cmd = body.split()[0].lower()

        if cmd == "/screenshot":
            if not self._runner.is_running or not self._runner.container:
                self._post_to_room(room_jid, "No task is running.")
                return

            self._post_to_room(room_jid, "📸 Capturing...")
            loop = asyncio.get_running_loop()
            png_bytes = await loop.run_in_executor(
                None, take_container_screenshot, self._runner.container
            )
            if png_bytes:
                await self._post_screenshot_to_room(room_jid, png_bytes, "Current screen")
            else:
                self._post_to_room(room_jid, "⚠️ Screenshot failed.")

        elif cmd == "/stop":
            if self._runner.is_running:
                self._runner.cancel()
                self._post_to_room(room_jid, "🛑 Cancelling task...")
            else:
                self._post_to_room(room_jid, "No task is running.")

        elif cmd == "/status":
            if self._runner.is_running:
                elapsed = time.monotonic() - self._runner.start_time
                self._post_to_room(
                    room_jid,
                    f"🔄 Step {self._runner.step}, {elapsed:.0f}s elapsed"
                )
            else:
                self._post_to_room(room_jid, "💤 Task finished.")

    # ── Update Worker ────────────────────────────────────────

    async def _room_update_worker(self, room_jid: str):
        """Poll the TaskRunner's update queue and post to the room.

        Runs as an async task alongside _run_task. Exits when the
        runner finishes (update_queue gets a 'done' sentinel).
        """
        while True:
            try:
                update = self._runner._update_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.5)
                # Check if runner finished
                if not self._runner.is_running:
                    break
                continue

            kind = update.get("kind")

            if kind == "step":
                step = update.get("step", "?")
                self._post_to_room(room_jid, f"── Step {step} ──")

            elif kind == "command":
                cmd = update.get("command", "")
                step = update.get("step", "?")
                # Format the command nicely
                if cmd.startswith("cua-click"):
                    self._post_to_room(room_jid, f"🖱️ {cmd}")
                elif cmd.startswith("cua-type"):
                    self._post_to_room(room_jid, f"⌨️ {cmd}")
                elif cmd.startswith("cua-scroll"):
                    self._post_to_room(room_jid, f"📜 {cmd}")
                elif cmd.startswith("cua-key"):
                    self._post_to_room(room_jid, f"⌨️ {cmd}")
                elif cmd.startswith("cua-done"):
                    self._post_to_room(room_jid, f"✅ {cmd}")
                else:
                    self._post_to_room(room_jid, f"▶️ {cmd}")

            elif kind == "error":
                msg = update.get("message", "Unknown error")
                self._post_to_room(room_jid, f"⚠️ {msg}")

            elif kind == "screenshot_included":
                self._post_to_room(room_jid, "📸 Screenshot will be attached to result")

    # ── Message Handling ─────────────────────────────────────

    async def on_message(self, msg):
        if msg["type"] not in ("chat", "normal"):
            return
        body = (msg["body"] or "").strip()
        if not body:
            return

        sender = str(msg["from"].bare)
        if sender != ALLOWED_USER:
            log.warning("Ignored message from unauthorized user: %s", sender)
            return

        log.info("Received from %s: %s", sender, body[:100])

        # Slash commands work even while a task is running
        if body.startswith("/"):
            await self._handle_command(sender, body)
            return

        # Enqueue the task
        self._task_counter += 1
        task_id = self._task_counter
        entry = {"id": task_id, "task": body, "sender": sender}

        if self._runner.is_running:
            self._queue.append(entry)
            position = len(self._queue)
            self.send_message(
                mto=sender,
                mbody=(
                    f"📋 Queued as #{task_id} ({position} task{'s' if position > 1 else ''} ahead).\n"
                    f"Task: {body[:100]}\n"
                    f"Send /queue to see the queue, /cancel {task_id} to remove."
                ),
                mtype="chat",
            )
        else:
            # Run immediately
            await self._run_task(entry)

    async def _handle_command(self, sender: str, body: str):
        """Handle slash commands."""
        cmd = body.split()[0].lower()

        if cmd == "/stop":
            if self._runner.is_running:
                self._runner.cancel()
                self.send_message(
                    mto=sender,
                    mbody="🛑 Cancelling task...",
                    mtype="chat",
                )
            else:
                self.send_message(
                    mto=sender,
                    mbody="No task is running.",
                    mtype="chat",
                )

        elif cmd == "/queue":
            if not self._queue:
                self.send_message(
                    mto=sender,
                    mbody="📋 Queue is empty.",
                    mtype="chat",
                )
            else:
                lines = ["📋 Queued tasks:"]
                for i, entry in enumerate(self._queue, 1):
                    lines.append(f"  {i}. #{entry['id']} — {entry['task'][:80]}")
                self.send_message(
                    mto=sender,
                    mbody="\n".join(lines),
                    mtype="chat",
                )

        elif cmd == "/cancel":
            parts = body.split()
            if len(parts) < 2 or not parts[1].lstrip("#").isdigit():
                self.send_message(
                    mto=sender,
                    mbody="Usage: /cancel <task_id>\nSend /queue to see task IDs.",
                    mtype="chat",
                )
                return

            cancel_id = int(parts[1].lstrip("#"))
            before = len(self._queue)
            self._queue = [e for e in self._queue if e["id"] != cancel_id]

            if len(self._queue) < before:
                self.send_message(
                    mto=sender,
                    mbody=f"✓ Cancelled task #{cancel_id}.",
                    mtype="chat",
                )
            else:
                self.send_message(
                    mto=sender,
                    mbody=f"Task #{cancel_id} not found in queue.\nSend /queue to see queued tasks.",
                    mtype="chat",
                )

        elif cmd == "/clear":
            count = len(self._queue)
            self._queue.clear()
            self.send_message(
                mto=sender,
                mbody=f"✓ Cleared {count} queued task{'s' if count != 1 else ''}.",
                mtype="chat",
            )

        elif cmd == "/screenshot":
            if not self._runner.is_running or not self._runner.container:
                self.send_message(
                    mto=sender,
                    mbody="No task is running — nothing to screenshot.",
                    mtype="chat",
                )
                return

            self.send_message(mto=sender, mbody="📸 Capturing...", mtype="chat")

            # Run in executor to avoid blocking
            loop = asyncio.get_running_loop()
            png_bytes = await loop.run_in_executor(
                None, take_container_screenshot, self._runner.container
            )

            if png_bytes is None:
                self.send_message(
                    mto=sender,
                    mbody="⚠️ Screenshot failed.",
                    mtype="chat",
                )
                return

            await self._send_screenshot(sender, png_bytes, "Current screen")

        elif cmd == "/last":
            if self._runner.is_running and self._runner.last_llm_reply:
                self.send_message(
                    mto=sender,
                    mbody=f"🧠 Last LLM reply:\n{self._runner.last_llm_reply[:1000]}",
                    mtype="chat",
                )
            elif self._runner.is_running:
                self.send_message(
                    mto=sender,
                    mbody="No LLM reply yet.",
                    mtype="chat",
                )
            else:
                self.send_message(
                    mto=sender,
                    mbody="No task is running.",
                    mtype="chat",
                )

        elif cmd == "/status":
            if self._runner.is_running:
                elapsed = time.monotonic() - self._runner.start_time
                parts = [
                    f"🔄 Running task (step {self._runner.step}, "
                    f"{elapsed:.0f}s elapsed)",
                    f"Task: {self._runner.task_text[:100]}",
                ]
                if self._queue:
                    parts.append(f"📋 {len(self._queue)} task{'s' if len(self._queue) > 1 else ''} queued")
                self.send_message(
                    mto=sender,
                    mbody="\n".join(parts),
                    mtype="chat",
                )
            else:
                self.send_message(
                    mto=sender,
                    mbody="💤 Idle — send a message to start a task.",
                    mtype="chat",
                )

        elif cmd == "/help":
            self.send_message(
                mto=sender,
                mbody=(
                    "Available commands:\n"
                    "  /screenshot — Capture the agent's screen\n"
                    "  /last       — Show the last LLM reply\n"
                    "  /status     — Check task and queue status\n"
                    "  /queue      — Show queued tasks\n"
                    "  /cancel N   — Remove task #N from queue\n"
                    "  /clear      — Clear the entire queue\n"
                    "  /stop       — Cancel the running task\n"
                    "  /help       — Show this message\n"
                    "\nSend any other text to start or queue a task."
                ),
                mtype="chat",
            )

        else:
            self.send_message(
                mto=sender,
                mbody=f"Unknown command: {cmd}\nSend /help for available commands.",
                mtype="chat",
            )

    async def _send_screenshot(self, to_jid: str, png_bytes: bytes, caption: str = ""):
        """Upload a screenshot and send it to the user."""
        ok = await upload_and_send_image(self, to_jid, png_bytes, caption)
        if not ok:
            # Fallback: send as text note
            self.send_message(
                mto=to_jid,
                mbody=f"📸 {caption}\n(Image upload not available — "
                      f"check Prosody HTTP File Share config)",
                mtype="chat",
            )

    async def _run_task(self, entry: dict):
        """Spawn an agent container, create a task room, and relay results."""
        task = entry["task"]
        sender = entry["sender"]
        task_id = entry["id"]

        # Create a MUC room for this task
        room_jid = await self._create_task_room(task_id, task)
        self._active_room = room_jid

        # Acknowledge in direct chat
        if room_jid:
            self.send_message(
                mto=sender,
                mbody=f"🚀 Starting task #{task_id}: {task[:100]}\n"
                      f"Follow progress in: {room_jid}",
                mtype="chat",
            )
            # Post to room
            self._post_to_room(room_jid, f"🚀 Task #{task_id}: {task}")
        else:
            self.send_message(
                mto=sender,
                mbody=f"🚀 Starting task #{task_id}: {task[:100]}",
                mtype="chat",
            )

        # Set presence to busy
        queue_note = f" ({len(self._queue)} queued)" if self._queue else ""
        self.send_presence(pshow="dnd", pstatus=f"Running #{task_id}{queue_note}")

        # Start the room update worker (polls TaskRunner for updates)
        update_task = None
        if room_jid:
            update_task = asyncio.ensure_future(self._room_update_worker(room_jid))

        # Run the agent
        try:
            result = await self._runner.run(task, sender)
        except Exception as e:
            log.exception("Task runner failed: %s", e)
            result = {
                "outcome": "error",
                "summary": f"Internal error: {e}",
                "result": None,
                "screenshot_b64": None,
                "steps": 0,
                "elapsed": 0,
            }

        # Stop the update worker
        if update_task:
            update_task.cancel()
            try:
                await update_task
            except asyncio.CancelledError:
                pass

        # Format result message
        if result["outcome"] == "completed":
            parts = [f"✅ #{task_id}: {result['summary']}"]
            if result["result"]:
                parts.append(f"Result: {result['result']}")
            parts.append(f"({result['steps']} steps, {result['elapsed']}s)")
            reply = "\n".join(parts)
        elif result["outcome"] == "cancelled":
            reply = f"🛑 #{task_id}: Cancelled after {result['steps']} steps."
        elif result["outcome"] == "error":
            reply = f"❌ #{task_id}: {result['summary']}"
        else:
            reply = (
                f"⚠️ #{task_id}: Task did not complete ({result['outcome']}).\n"
                f"Ran for {result['steps']} steps in {result['elapsed']}s."
            )

        # Post result to direct chat
        self.send_message(mto=sender, mbody=reply, mtype="chat")

        # Post result to room
        if room_jid:
            self._post_to_room(room_jid, reply)

        # Send screenshot if included in result
        if result.get("screenshot_b64"):
            try:
                png_bytes = base64.b64decode(result["screenshot_b64"])
                # Send to direct chat
                await self._send_screenshot(sender, png_bytes, "Final screen state")
                # Send to room
                if room_jid:
                    await self._post_screenshot_to_room(
                        room_jid, png_bytes, "Final screen state"
                    )
            except Exception as e:
                log.warning("Failed to send result screenshot: %s", e)

        # Leave the room (orchestrator's work is done)
        if room_jid:
            self._post_to_room(room_jid, "── Task complete ──")
            self._active_room = ""

        # Process next task in queue
        await self._process_queue()

    async def _process_queue(self):
        """Pop and run the next queued task, or go idle."""
        if self._queue:
            entry = self._queue.pop(0)
            log.info("Dequeued task #%d (%d remaining)", entry["id"], len(self._queue))
            await self._run_task(entry)
        else:
            self.send_presence(pshow="chat", pstatus="Ready for tasks")


# ── Main ─────────────────────────────────────────────────────


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Verify Docker access
    try:
        client = get_docker()
        client.ping()
        log.info("Docker connection OK")
    except Exception as e:
        log.error("Cannot connect to Docker: %s", e)
        log.error("Is /var/run/docker.sock mounted?")
        raise SystemExit(1)

    # Detect image early so we fail fast
    try:
        image = detect_image()
        log.info("Agent image: %s", image)
    except RuntimeError as e:
        log.error(str(e))
        raise SystemExit(1)

    net = detect_network()
    log.info("Docker network: %s", net)

    host_ip = detect_host_ip()
    log.info("Host IP for spawned containers: %s", host_ip)

    # XMPP setup
    server_addr = (XMPP_HOST, XMPP_PORT)
    bot = OrchestratorBot(XMPP_JID, XMPP_PASSWORD, server_addr)

    if not XMPP_VERIFY_CERT:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        bot.ssl_context = ctx

    bot.connect(server_addr)

    log.info("Starting orchestrator — connecting to %s:%s", *server_addr)

    loop = asyncio.get_event_loop()

    def _shutdown(signum, frame):
        log.info("Shutting down (signal %s)", signum)
        bot.disconnect()
        loop.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    loop.run_forever()


if __name__ == "__main__":
    main()
