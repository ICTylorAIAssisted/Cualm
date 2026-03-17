"""LLM proxy sidecar for benchmark network isolation.

When webarena-net is internal (no external routing), the agent can't
reach the host LLM directly.  This module manages a lightweight socat
container that bridges traffic:

    agent (webarena-net, internal)
        → llm-proxy (webarena-net + webarena-llm-bridge)
            → host LLM (via webarena-llm-bridge, not internal)

The proxy is transparent — it just forwards TCP on the LLM port.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import docker


SOCAT_IMAGE = "alpine/socat"

# Name of the non-internal bridge network used by the LLM proxy
# to reach the host.  Created once per benchmark run.
LLM_BRIDGE_NETWORK = "webarena-llm-bridge"


class LlmProxy:
    """Manages a socat container that forwards LLM traffic to the host."""

    def __init__(
        self,
        client: docker.DockerClient,
        task_id: int,
        llm_base_url: str,
        internal_network: str,
    ):
        self.client = client
        self.task_id = task_id
        self.internal_network = internal_network
        self.container = None
        self._name = f"bench-llm-proxy-{task_id}-{int(time.time())}"

        # Parse LLM URL to extract host and port
        parsed = urlparse(llm_base_url)
        self._llm_host = parsed.hostname or "host.docker.internal"
        self._llm_port = parsed.port or 8000
        self._llm_path = parsed.path  # e.g. "/v1"

    @property
    def base_url(self) -> str:
        """The OPENAI_BASE_URL the agent should use to reach the LLM
        through this proxy."""
        return f"http://{self._name}:{self._llm_port}{self._llm_path}"

    def start(self) -> None:
        """Start the socat forwarding container."""
        # Ensure the non-internal bridge network exists
        _ensure_bridge_network(self.client)

        # Start socat on the internal network first
        self.container = self.client.containers.run(
            image=SOCAT_IMAGE,
            command=[
                f"TCP-LISTEN:{self._llm_port},fork,reuseaddr",
                f"TCP:host.docker.internal:{self._llm_port}",
            ],
            name=self._name,
            network=self.internal_network,
            detach=True,
            mem_limit="64m",
        )

        # Connect to the non-internal bridge so socat can reach the host
        bridge = self.client.networks.get(LLM_BRIDGE_NETWORK)
        bridge.connect(
            self.container,
            aliases=[self._name],
        )

        # Map host.docker.internal inside the socat container so it can
        # resolve the LLM host.  The bridge network's gateway IS the host.
        try:
            bridge_net = self.client.networks.get(LLM_BRIDGE_NETWORK)
            gateway = None
            for cfg in bridge_net.attrs.get("IPAM", {}).get("Config", []):
                gateway = cfg.get("Gateway")
                if gateway:
                    break
            if gateway:
                self.container.exec_run(
                    f"sh -c 'echo \"{gateway} host.docker.internal\" >> /etc/hosts'",
                )
        except Exception:
            pass

        # Give socat a moment to bind
        time.sleep(1)

    def stop(self) -> None:
        """Stop and remove the proxy container."""
        if self.container:
            try:
                self.container.remove(force=True)
            except Exception:
                pass


def _ensure_bridge_network(client: docker.DockerClient) -> None:
    """Create the non-internal bridge network if it doesn't exist."""
    try:
        client.networks.get(LLM_BRIDGE_NETWORK)
    except docker.errors.NotFound:
        client.networks.create(
            LLM_BRIDGE_NETWORK,
            driver="bridge",
            internal=False,
        )


def ensure_socat_image(client: docker.DockerClient) -> None:
    """Pull the socat image if not already present."""
    try:
        client.images.get(SOCAT_IMAGE)
    except docker.errors.ImageNotFound:
        print(f"Pulling {SOCAT_IMAGE}...")
        client.images.pull(SOCAT_IMAGE)


def cleanup_bridge_network(client: docker.DockerClient) -> None:
    """Remove the LLM bridge network (call after benchmark run)."""
    try:
        net = client.networks.get(LLM_BRIDGE_NETWORK)
        net.remove()
    except Exception:
        pass
