"""Unit tests for cualm preflight checks."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from cualm_preflight import (
    SystemInfo, run_checks, get_hint, _run,
)


class TestRunChecks:
    """Test run_checks against various SystemInfo states."""

    def test_all_good(self):
        info = SystemInfo(
            os_id="fedora", os_version="42", arch="x86_64",
            gpu_vendor="amd", gpu_model="RX 7900", gpu_driver="ROCm (gfx1100)",
            runtime="podman", runtime_version="5.3.1", runtime_rootless=True,
            socket_path="/tmp/fake.sock",
            agent_image=True,
            llm_url="http://localhost:8000/v1", llm_reachable=True,
        )
        # Mock socket existence check
        with patch("os.path.exists", return_value=True), \
             patch("os.access", return_value=True):
            results = run_checks(info)

        failed = [r for r in results if not r.ok and r.blocking]
        assert len(failed) == 0, f"Unexpected failures: {[r.name for r in failed]}"

    def test_no_runtime(self):
        info = SystemInfo(os_id="fedora", runtime="none")
        results = run_checks(info)
        rt = [r for r in results if r.name == "Container runtime"]
        assert len(rt) == 1
        assert not rt[0].ok
        assert rt[0].blocking
        assert "Podman" in rt[0].hint or "Docker" in rt[0].hint

    def test_no_image(self):
        info = SystemInfo(
            os_id="fedora",
            runtime="podman", runtime_version="5.0",
            socket_path="/tmp/fake.sock",
            agent_image=False,
        )
        with patch("os.path.exists", return_value=True), \
             patch("os.access", return_value=True):
            results = run_checks(info)
        img = [r for r in results if r.name == "Agent image"]
        assert len(img) == 1
        assert not img[0].ok
        assert "build" in img[0].hint.lower()

    def test_gpu_no_driver(self):
        info = SystemInfo(
            os_id="fedora",
            gpu_vendor="nvidia", gpu_model="RTX 4090", gpu_driver="",
            runtime="docker", runtime_version="24.0",
            socket_path="/var/run/docker.sock",
            agent_image=True,
            llm_url="http://localhost:8000/v1", llm_reachable=True,
        )
        with patch("os.path.exists", return_value=True), \
             patch("os.access", return_value=True):
            results = run_checks(info)
        gpu = [r for r in results if r.name == "GPU"]
        assert len(gpu) == 1
        assert not gpu[0].ok
        # GPU without driver is a warning, not blocking
        assert not gpu[0].blocking

    def test_llm_not_reachable(self):
        info = SystemInfo(
            os_id="ubuntu",
            runtime="docker", runtime_version="24.0",
            socket_path="/var/run/docker.sock",
            agent_image=True,
            llm_url="http://localhost:8000/v1", llm_reachable=False,
            gpu_vendor="none",
        )
        with patch("os.path.exists", return_value=True), \
             patch("os.access", return_value=True):
            results = run_checks(info)
        llm = [r for r in results if r.name == "LLM server"]
        assert len(llm) == 1
        assert not llm[0].ok


class TestGetHint:
    """Test install hint lookup."""

    def test_fedora_runtime(self):
        hint = get_hint("runtime", "fedora")
        assert "dnf" in hint
        assert "podman" in hint.lower()

    def test_ubuntu_runtime(self):
        hint = get_hint("runtime", "ubuntu")
        assert "docker" in hint.lower()

    def test_wildcard_fallback(self):
        hint = get_hint("llm_cpu", "some-unknown-distro")
        assert "llama" in hint.lower()

    def test_missing_hint(self):
        hint = get_hint("nonexistent_category", "fedora")
        assert hint == ""


class TestRun:
    """Test the _run helper."""

    def test_success(self):
        rc, out, err = _run(["echo", "hello"])
        assert rc == 0
        assert out == "hello"

    def test_not_found(self):
        rc, out, err = _run(["nonexistent_command_xyz"])
        assert rc == 127

    def test_timeout(self):
        rc, out, err = _run(["sleep", "10"], timeout=1)
        assert rc == 124
