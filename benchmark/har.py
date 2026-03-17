"""Per-task HAR capture via ephemeral mitmproxy containers.

Each task gets its own mitmproxy container so HAR files are naturally
isolated.  The proxy container starts before the agent and is torn
down after, with the HAR file extracted from the container filesystem.

The agent's Chromium connects via CUA_HTTP_PROXY and uses
``--proxy-bypass-list=<-loopback>`` so that localhost traffic also
goes through the proxy.  A mitmproxy addon rewrites localhost:PORT
connections to the actual WebArena container hostnames (e.g.
``localhost:7770`` → ``shopping:80``), then restores the original
localhost URL in the response so the HAR records correct URLs.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
from pathlib import Path

import docker


PROXY_IMAGE = "mitmproxy/mitmproxy"
PROXY_PORT = 8080
HAR_DUMP_PATH = "/tmp/traffic.har"
ADDON_PATH = "/tmp/rewrite_addon.py"

EMPTY_HAR = {
    "log": {
        "version": "1.2",
        "creator": {"name": "cua-benchmark", "version": "0.1"},
        "entries": [],
    }
}

# Template for the mitmproxy addon that rewrites localhost → container
ADDON_TEMPLATE = """\
import mitmproxy.http

# Mapping: localhost port → (container_hostname, container_port)
MAPPING = {mapping}

class LocalhostRedirect:
    def request(self, flow: mitmproxy.http.HTTPFlow):
        if flow.request.pretty_host in ("localhost", "127.0.0.1"):
            port = flow.request.port
            if port in MAPPING:
                new_host, new_port = MAPPING[port]
                flow.metadata["original_host"] = flow.request.host
                flow.metadata["original_port"] = flow.request.port
                # Preserve the Host header so the site still sees
                # "localhost:PORT" — matches its configured base URL.
                # Without this, mitmproxy sends "Host: shopping_admin"
                # and Magento redirect-loops trying to fix the URL.
                original_host_header = flow.request.headers.get("Host", "")
                flow.request.host = new_host
                flow.request.port = new_port
                if original_host_header:
                    flow.request.headers["Host"] = original_host_header

    def response(self, flow: mitmproxy.http.HTTPFlow):
        if "original_host" in flow.metadata:
            flow.request.host = flow.metadata["original_host"]
            flow.request.port = flow.metadata["original_port"]

addons = [LocalhostRedirect()]
"""


class HarProxy:
    """Manages an ephemeral mitmproxy container for one benchmark task."""

    def __init__(
        self,
        client: docker.DockerClient,
        task_id: int,
        network: str,
        output_dir: Path,
        port_forwards: dict[int, tuple[str, int]] | None = None,
        host_target: str = "host-gateway",
    ):
        self.client = client
        self.task_id = task_id
        self.network = network
        self.output_dir = output_dir
        self.port_forwards = port_forwards or {}
        self.host_target = host_target
        self.container = None
        self._name = f"bench-proxy-{task_id}-{int(time.time())}"

    @property
    def proxy_url(self) -> str:
        """URL the agent should use as its HTTP proxy."""
        return f"http://{self._name}:{PROXY_PORT}"

    def start(self) -> None:
        """Start the mitmproxy container."""
        import base64

        # Build the addon script with the port mapping
        addon_code = ADDON_TEMPLATE.format(
            mapping=repr(self.port_forwards),
        )

        # Inject via base64 — avoids all quoting issues with echo/sh -c.
        # "exec" replaces sh with mitmdump as PID 1 so that SIGINT
        # (sent by stop_and_extract) reaches mitmdump directly —
        # without it, sh swallows the signal and mitmdump never
        # flushes the HAR file.
        b64 = base64.b64encode(addon_code.encode()).decode()
        shell_cmd = (
            f"echo {b64} | base64 -d > {ADDON_PATH} && "
            f"exec mitmdump"
            f" --mode regular"
            f" --listen-port {PROXY_PORT}"
            f" --set hardump={HAR_DUMP_PATH}"
            f" --ssl-insecure"
            f" -s {ADDON_PATH}"
        )

        self.container = self.client.containers.run(
            image=PROXY_IMAGE,
            command=["sh", "-c", shell_cmd],
            name=self._name,
            network=self.network,
            extra_hosts={"host.docker.internal": self.host_target},
            detach=True,
            mem_limit="512m",
        )
        # Give mitmproxy a moment to bind its listening port
        time.sleep(2)

    def stop_and_extract(self) -> Path:
        """Stop the proxy, extract the HAR file, remove the container.

        Always returns a path — writes an empty HAR as fallback if
        capture failed, so the caller never gets None.
        """
        if self.container is None:
            return self._write_empty_har()

        har_path = None
        try:
            # Send SIGINT — mitmdump handles this by flushing the HAR
            # file and exiting cleanly.
            self.container.kill(signal="SIGINT")
            self.container.wait(timeout=20)
        except Exception:
            try:
                self.container.stop(timeout=5)
            except Exception:
                pass

        try:
            har_path = self._extract_har()
        except Exception as e:
            print(f"    ⚠ HAR extraction failed for task {self.task_id}: {e}")
            # Dump proxy logs for debugging
            try:
                logs = self.container.logs(tail=20).decode(errors="replace")
                print(f"    ⚠ Proxy logs (last 20 lines):\n{logs}")
            except Exception:
                pass

        try:
            self.container.remove(force=True)
        except Exception:
            pass

        if har_path is None:
            har_path = self._write_empty_har()

        return har_path

    def cleanup(self) -> None:
        """Force-remove the proxy container (e.g. on error)."""
        if self.container:
            try:
                self.container.remove(force=True)
            except Exception:
                pass

    def _extract_har(self) -> Path | None:
        """Pull the HAR file out of the stopped container."""
        stream, _ = self.container.get_archive(HAR_DUMP_PATH)
        tar_bytes = b"".join(stream)
        tar = tarfile.open(fileobj=io.BytesIO(tar_bytes))
        member = tar.getmembers()[0]
        f = tar.extractfile(member)
        if not f:
            return None

        har_data = f.read()
        json.loads(har_data)

        task_dir = self.output_dir / str(self.task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        har_path = task_dir / "network.har"
        har_path.write_bytes(har_data)

        return har_path

    def _write_empty_har(self) -> Path:
        """Write a minimal valid HAR as fallback."""
        task_dir = self.output_dir / str(self.task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        har_path = task_dir / "network.har"
        har_path.write_text(json.dumps(EMPTY_HAR, indent=2))
        print(f"    ⚠ Wrote empty HAR fallback for task {self.task_id}")
        return har_path


def ensure_proxy_image(client: docker.DockerClient) -> None:
    """Pull the mitmproxy image if not already present."""
    try:
        client.images.get(PROXY_IMAGE)
    except docker.errors.ImageNotFound:
        print(f"Pulling {PROXY_IMAGE}...")
        client.images.pull(PROXY_IMAGE)
