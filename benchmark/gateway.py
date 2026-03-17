"""Nginx gateway for benchmark network isolation.

When webarena-net is ``internal: true``, containers on it have no
route to the outside.  This gateway sits on both webarena-net
(internal) and webarena-ext (normal), forwarding traffic:

    Agent → gateway:7770 → shopping:80      (within webarena-net)
    Agent → gateway:LLM_PORT → host:LLM_PORT (via webarena-ext)

Agent containers map ``host.docker.internal`` to the gateway's IP
on webarena-net, so ``http://host.docker.internal:7770`` transparently
reaches the shopping site.

One gateway per benchmark run, shared across all tasks.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import docker

NGINX_IMAGE = "nginx:alpine"
GATEWAY_NAME = "bench-gateway"
EXT_NETWORK = "webarena-ext"

# Port → internal upstream for WebArena sites.
# These match the compose port mappings (host:container).
SITE_UPSTREAMS = {
    7770: "shopping:80",
    7780: "shopping_admin:80",
    9999: "reddit:80",
    8023: "gitlab:8023",
    4399: "homepage:80",
    # Phase 2:
    # 8888: "wikipedia:80",
    # 3000: "map:3000",
}

NGINX_CONF_TEMPLATE = """\
worker_processes auto;
error_log /dev/stderr warn;
pid /tmp/nginx.pid;

events {{
    worker_connections 1024;
}}

stream {{
{upstreams}
}}
"""

UPSTREAM_BLOCK = """\
    server {{
        listen {port};
        proxy_pass {upstream};
        proxy_connect_timeout 5s;
        proxy_timeout 300s;
    }}
"""


class Gateway:
    """Manages the nginx gateway container for a benchmark run."""

    def __init__(
        self,
        client: docker.DockerClient,
        network: str,
        llm_base_url: str,
    ):
        self.client = client
        self.network = network
        self.container = None
        self._ip: str | None = None

        # Parse LLM URL to extract port
        parsed = urlparse(llm_base_url)
        self._llm_port = parsed.port or 8000

    @property
    def ip(self) -> str | None:
        """Gateway's IP on webarena-net.  Available after start()."""
        return self._ip

    def start(self) -> None:
        """Create the ext network, generate config, start nginx."""
        # Ensure the non-internal bridge network exists
        _ensure_ext_network(self.client)

        # Generate nginx config
        conf = self._build_config()

        # Start nginx with custom config piped via sh -c
        # (avoids needing a volume mount for the config file)
        self.container = self.client.containers.run(
            image=NGINX_IMAGE,
            name=GATEWAY_NAME,
            command=[
                "sh", "-c",
                f"echo '{conf}' > /tmp/nginx.conf && "
                f"nginx -c /tmp/nginx.conf -g 'daemon off;'"
            ],
            network=self.network,
            # Map host.docker.internal so nginx can forward LLM
            # traffic to the real host via webarena-ext
            extra_hosts={"host.docker.internal": "host-gateway"},
            detach=True,
            mem_limit="128m",
        )

        # Connect to the non-internal network for LLM forwarding
        ext_net = self.client.networks.get(EXT_NETWORK)
        ext_net.connect(self.container)

        # Wait for nginx to start, then get its IP on webarena-net
        time.sleep(2)
        self.container.reload()
        networks = self.container.attrs["NetworkSettings"]["Networks"]
        self._ip = networks[self.network]["IPAddress"]

        print(f"  Gateway: {self._ip} (ports: "
              f"{', '.join(str(p) for p in sorted(SITE_UPSTREAMS))} + "
              f"LLM:{self._llm_port})")

    def stop(self) -> None:
        """Stop and remove the gateway container and ext network."""
        if self.container:
            try:
                self.container.remove(force=True)
            except Exception:
                pass
        _cleanup_ext_network(self.client)

    def _build_config(self) -> str:
        """Generate the nginx stream config."""
        blocks = []

        # Site upstreams (within webarena-net)
        for port, upstream in sorted(SITE_UPSTREAMS.items()):
            blocks.append(UPSTREAM_BLOCK.format(
                port=port, upstream=upstream,
            ))

        # LLM upstream (via webarena-ext → real host)
        # nginx resolves host.docker.internal from /etc/hosts
        if self._llm_port not in SITE_UPSTREAMS:
            blocks.append(UPSTREAM_BLOCK.format(
                port=self._llm_port,
                upstream=f"host.docker.internal:{self._llm_port}",
            ))

        return NGINX_CONF_TEMPLATE.format(
            upstreams="".join(blocks),
        )


def _ensure_ext_network(client: docker.DockerClient) -> None:
    """Create the non-internal bridge network if it doesn't exist."""
    try:
        client.networks.get(EXT_NETWORK)
    except docker.errors.NotFound:
        client.networks.create(EXT_NETWORK, driver="bridge", internal=False)


def _cleanup_ext_network(client: docker.DockerClient) -> None:
    """Remove the ext network."""
    try:
        net = client.networks.get(EXT_NETWORK)
        net.remove()
    except Exception:
        pass


def ensure_nginx_image(client: docker.DockerClient) -> None:
    """Pull the nginx image if not already present."""
    try:
        client.images.get(NGINX_IMAGE)
    except docker.errors.ImageNotFound:
        print(f"Pulling {NGINX_IMAGE}...")
        client.images.pull(NGINX_IMAGE)
