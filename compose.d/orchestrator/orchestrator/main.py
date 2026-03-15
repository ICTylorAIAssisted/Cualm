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
import json
import logging
import os
import re
import signal
import ssl
import time
from datetime import datetime, timezone

import docker
import slixmpp

log = logging.getLogger("orchestrator")

# ── Configuration ────────────────────────────────────────────

XMPP_JID = os.environ.get("ORCH_XMPP_JID", "orchestrator@cua.local")
XMPP_PASSWORD = os.environ.get("ORCH_XMPP_PASSWORD", "orch-secret")
XMPP_HOST = os.environ.get("ORCH_XMPP_HOST", "prosody")
XMPP_PORT = int(os.environ.get("ORCH_XMPP_PORT", "5222"))
XMPP_VERIFY_CERT = os.environ.get("ORCH_XMPP_VERIFY_CERT", "false").lower() == "true"

# Who is allowed to send tasks
ALLOWED_USER = os.environ.get("ORCH_ALLOWED_USER", "user@cua.local")

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

                    # Track last LLM reply
                    elif "LLM →" in line:
                        self.last_llm_reply = line.split("LLM →", 1)[1].strip()

                    # Detect completion
                    elif line.startswith("✅ Done:"):
                        outcome = "completed"
                        summary = line[len("✅ Done:"):].strip()

                    elif line.strip().startswith("Result:"):
                        task_result = line.split("Result:", 1)[1].strip()

                    elif "Reached max steps" in line:
                        outcome = "max_steps"
                        summary = "Reached maximum steps without completing"

        except Exception as e:
            log.warning("Log streaming error: %s", e)
            if self._cancel:
                outcome = "cancelled"
                summary = "Task cancelled by user"

        # Cleanup container
        elapsed = time.monotonic() - self.start_time
        try:
            self.container.wait(timeout=10)
        except Exception:
            pass
        try:
            self.container.remove(force=True)
        except Exception as e:
            log.warning("Container cleanup failed: %s", e)

        return {
            "outcome": outcome,
            "summary": summary,
            "result": task_result,
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

        self.add_event_handler("session_start", self.on_start)
        self.add_event_handler("message", self.on_message)
        self.add_event_handler("disconnected", self.on_disconnect)

        self.register_plugin("xep_0030")  # Service Discovery
        self.register_plugin("xep_0199")  # Ping

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

        # Check if a task is already running
        if self._runner.is_running:
            self.send_message(
                mto=sender,
                mbody=(
                    f"⏳ A task is already running (step {self._runner.step}).\n"
                    f"Task: {self._runner.task_text[:100]}\n"
                    f"Send /stop to cancel it."
                ),
                mtype="chat",
            )
            return

        # Slash commands
        if body.startswith("/"):
            await self._handle_command(sender, body)
            return

        # New task
        await self._run_task(sender, body)

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

        elif cmd == "/status":
            if self._runner.is_running:
                elapsed = time.monotonic() - self._runner.start_time
                self.send_message(
                    mto=sender,
                    mbody=(
                        f"🔄 Running task (step {self._runner.step}, "
                        f"{elapsed:.0f}s elapsed)\n"
                        f"Task: {self._runner.task_text[:100]}"
                    ),
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
                    "  /status  — Check if a task is running\n"
                    "  /stop    — Cancel the current task\n"
                    "  /help    — Show this message\n"
                    "\nSend any other text to start a new task."
                ),
                mtype="chat",
            )

        else:
            self.send_message(
                mto=sender,
                mbody=f"Unknown command: {cmd}\nSend /help for available commands.",
                mtype="chat",
            )

    async def _run_task(self, sender: str, task: str):
        """Spawn an agent container and relay the result."""
        # Acknowledge
        self.send_message(
            mto=sender,
            mbody=f"🚀 Starting task: {task[:100]}",
            mtype="chat",
        )

        # Set presence to busy
        self.send_presence(pshow="dnd", pstatus=f"Running: {task[:60]}")

        # Run in background
        try:
            result = await self._runner.run(task, sender)
        except Exception as e:
            log.exception("Task runner failed: %s", e)
            result = {
                "outcome": "error",
                "summary": f"Internal error: {e}",
                "result": None,
                "steps": 0,
                "elapsed": 0,
            }

        # Format result message
        if result["outcome"] == "completed":
            parts = [f"✅ {result['summary']}"]
            if result["result"]:
                parts.append(f"Result: {result['result']}")
            parts.append(f"({result['steps']} steps, {result['elapsed']}s)")
            reply = "\n".join(parts)
        elif result["outcome"] == "cancelled":
            reply = f"🛑 Task cancelled after {result['steps']} steps."
        elif result["outcome"] == "error":
            reply = f"❌ {result['summary']}"
        else:
            reply = (
                f"⚠️ Task did not complete ({result['outcome']}).\n"
                f"Ran for {result['steps']} steps in {result['elapsed']}s."
            )

        self.send_message(mto=sender, mbody=reply, mtype="chat")

        # Set presence back to available
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
