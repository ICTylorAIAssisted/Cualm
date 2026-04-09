#!/usr/bin/env python3
"""Preflight environment checks for cualm.

Detects system capabilities, validates the environment, and provides
actionable guidance when dependencies are missing.

Usage:
  from cualm_preflight import preflight
  ok = preflight()  # True if ready, False if blocking issues
"""

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── Terminal colors ──
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str = ""
    hint: str = ""  # actionable fix
    blocking: bool = True  # if False, just a warning


@dataclass
class SystemInfo:
    os_id: str = ""  # fedora, ubuntu, debian, arch, etc.
    os_version: str = ""
    arch: str = ""
    gpu_vendor: str = ""  # amd, nvidia, intel, none
    gpu_model: str = ""
    gpu_driver: str = ""  # rocm version, cuda version, etc.
    runtime: str = ""  # docker, podman, none
    runtime_version: str = ""
    runtime_rootless: bool = False
    socket_path: str = ""
    agent_image: bool = False
    llm_url: str = ""
    llm_reachable: bool = False


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def detect_os() -> tuple[str, str]:
    """Detect OS from /etc/os-release."""
    try:
        with open("/etc/os-release") as f:
            data = {}
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    data[k] = v.strip('"')
        return data.get("ID", "unknown"), data.get("VERSION_ID", "")
    except FileNotFoundError:
        return platform.system().lower(), platform.release()


def detect_gpu() -> tuple[str, str, str]:
    """Detect GPU vendor, model, and driver status."""
    vendor, model, driver = "none", "", ""

    # Check lspci
    rc, out, _ = _run(["lspci"])
    if rc == 0:
        for line in out.split("\n"):
            lower = line.lower()
            if "vga" in lower or "3d controller" in lower or "display" in lower:
                if "amd" in lower or "radeon" in lower or "advanced micro" in lower:
                    vendor = "amd"
                    model = line.split(":")[-1].strip()
                elif "nvidia" in lower:
                    vendor = "nvidia"
                    model = line.split(":")[-1].strip()
                elif "intel" in lower:
                    vendor = "intel"
                    model = line.split(":")[-1].strip()

    # Check driver
    if vendor == "amd":
        rc, out, _ = _run(["rocminfo"])
        if rc == 0:
            for line in out.split("\n"):
                if "gfx" in line.lower():
                    m = re.search(r"(gfx\w+)", line)
                    if m:
                        driver = f"ROCm ({m.group(1)})"
                        break
    elif vendor == "nvidia":
        rc, out, _ = _run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
        if rc == 0:
            driver = f"CUDA (driver {out.strip()})"

    return vendor, model, driver


def detect_runtime() -> tuple[str, str, bool, str]:
    """Detect container runtime and socket."""
    # Check podman first (preferred on Fedora)
    rc, out, _ = _run(["podman", "version", "--format", "{{.Client.Version}}"])
    if rc == 0:
        version = out.strip()
        rootless = os.getuid() != 0
        # Find socket
        socket_path = ""
        if os.environ.get("DOCKER_HOST"):
            socket_path = os.environ["DOCKER_HOST"].replace("unix://", "")
        else:
            uid = os.getuid()
            candidates = [
                f"/run/user/{uid}/podman/podman.sock",
                "/run/podman/podman.sock",
                "/var/run/docker.sock",
            ]
            for s in candidates:
                if os.path.exists(s):
                    socket_path = s
                    break
        return "podman", version, rootless, socket_path

    # Check docker
    rc, out, _ = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    if rc == 0:
        version = out.strip()
        socket_path = os.environ.get("DOCKER_HOST", "").replace("unix://", "") \
            or "/var/run/docker.sock"
        return "docker", version, False, socket_path

    return "none", "", False, ""


def check_agent_image(runtime: str) -> bool:
    """Check if the agent image is built."""
    cmd = [runtime, "image", "inspect", "cua-agent"]
    rc, _, _ = _run(cmd, timeout=5)
    return rc == 0


def check_llm(url: str) -> bool:
    """Check if the LLM server is reachable."""
    if not url:
        return False
    try:
        import urllib.request
        req = urllib.request.urlopen(f"{url}/models", timeout=5)
        return req.status == 200
    except Exception:
        return False


# ── Per-distro install instructions ──

INSTALL_HINTS = {
    ("runtime", "fedora"): (
        "Install Podman:\n"
        "  sudo dnf install podman podman-compose\n"
        "  systemctl --user enable --now podman.socket\n"
        "  https://podman.io/docs/installation"
    ),
    ("runtime", "ubuntu"): (
        "Install Docker:\n"
        "  curl -fsSL https://get.docker.com | sh\n"
        "  sudo usermod -aG docker $USER && newgrp docker\n"
        "  https://docs.docker.com/engine/install/ubuntu/"
    ),
    ("runtime", "debian"): (
        "Install Docker:\n"
        "  curl -fsSL https://get.docker.com | sh\n"
        "  sudo usermod -aG docker $USER && newgrp docker\n"
        "  https://docs.docker.com/engine/install/debian/"
    ),
    ("runtime", "arch"): (
        "Install Podman:\n"
        "  sudo pacman -S podman podman-compose\n"
        "  systemctl --user enable --now podman.socket"
    ),
    ("gpu_amd", "fedora"): (
        "Install ROCm for AMD GPU:\n"
        "  sudo dnf install rocm-hip-runtime rocm-hip-sdk\n"
        "  https://rocm.docs.amd.com/projects/install-on-linux/"
    ),
    ("gpu_amd", "ubuntu"): (
        "Install ROCm for AMD GPU:\n"
        "  https://rocm.docs.amd.com/projects/install-on-linux/\n"
        "  Verify: rocminfo | grep gfx"
    ),
    ("gpu_nvidia", "fedora"): (
        "Install Nvidia drivers:\n"
        "  https://rpmfusion.org/Howto/NVIDIA\n"
        "  Verify: nvidia-smi"
    ),
    ("gpu_nvidia", "ubuntu"): (
        "Install Nvidia drivers:\n"
        "  sudo apt install nvidia-driver-560 nvidia-cuda-toolkit\n"
        "  Verify: nvidia-smi"
    ),
    ("llm_amd", "*"): (
        "Recommended LLM server for AMD GPU:\n"
        "  Lemonade Server: https://github.com/onnx/turnkeyml\n"
        "  Or llama.cpp with ROCm: https://github.com/ggml-org/llama.cpp"
    ),
    ("llm_nvidia", "*"): (
        "Recommended LLM server for Nvidia GPU:\n"
        "  LM Studio: https://lmstudio.ai\n"
        "  Or llama.cpp with CUDA: https://github.com/ggml-org/llama.cpp"
    ),
    ("llm_cpu", "*"): (
        "Recommended LLM server (CPU-only):\n"
        "  llama.cpp: https://github.com/ggml-org/llama.cpp\n"
        "  Ollama: https://ollama.ai"
    ),
}


def get_hint(category: str, os_id: str) -> str:
    """Get install hint for a category, falling back to wildcard."""
    return (INSTALL_HINTS.get((category, os_id))
            or INSTALL_HINTS.get((category, "*"))
            or "")


def detect_system() -> SystemInfo:
    """Full system detection."""
    info = SystemInfo()
    info.os_id, info.os_version = detect_os()
    info.arch = platform.machine()
    info.gpu_vendor, info.gpu_model, info.gpu_driver = detect_gpu()
    info.runtime, info.runtime_version, info.runtime_rootless, info.socket_path = detect_runtime()
    if info.runtime != "none":
        info.agent_image = check_agent_image(
            "podman" if info.runtime == "podman" else "docker"
        )
    info.llm_url = os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1")
    info.llm_reachable = check_llm(info.llm_url)
    return info


def run_checks(info: SystemInfo) -> list[CheckResult]:
    """Run all preflight checks against detected system info."""
    results = []

    # 1. Container runtime
    if info.runtime == "none":
        results.append(CheckResult(
            name="Container runtime",
            ok=False,
            message="No Docker or Podman found",
            hint=get_hint("runtime", info.os_id),
        ))
    else:
        label = f"{info.runtime} {info.runtime_version}"
        if info.runtime_rootless:
            label += " (rootless)"
        results.append(CheckResult(
            name="Container runtime",
            ok=True,
            message=label,
        ))

    # 2. Socket access
    if info.runtime != "none":
        if info.socket_path and os.path.exists(info.socket_path):
            # Check we can actually access it
            if os.access(info.socket_path, os.R_OK | os.W_OK):
                results.append(CheckResult(
                    name="Runtime socket",
                    ok=True,
                    message=info.socket_path,
                ))
            else:
                results.append(CheckResult(
                    name="Runtime socket",
                    ok=False,
                    message=f"Permission denied: {info.socket_path}",
                    hint=(
                        "For rootless Podman:\n"
                        "  systemctl --user enable --now podman.socket\n"
                        "For Docker:\n"
                        "  sudo usermod -aG docker $USER && newgrp docker"
                    ),
                ))
        else:
            results.append(CheckResult(
                name="Runtime socket",
                ok=False,
                message="Socket not found",
                hint=(
                    f"Expected: {info.socket_path or '/var/run/docker.sock'}\n"
                    "Start the socket service:\n"
                    "  systemctl --user start podman.socket  (Podman)\n"
                    "  sudo systemctl start docker           (Docker)"
                ),
            ))

    # 3. Agent image
    if info.runtime != "none":
        if info.agent_image:
            results.append(CheckResult(
                name="Agent image",
                ok=True,
                message="cua-agent:latest",
            ))
        else:
            results.append(CheckResult(
                name="Agent image",
                ok=False,
                message="cua-agent image not found",
                hint="Build with: ./run.sh build",
            ))

    # 4. GPU
    if info.gpu_vendor != "none":
        if info.gpu_driver:
            results.append(CheckResult(
                name="GPU",
                ok=True,
                message=f"{info.gpu_model} — {info.gpu_driver}",
            ))
        else:
            hint_key = f"gpu_{info.gpu_vendor}"
            results.append(CheckResult(
                name="GPU",
                ok=False,
                message=f"{info.gpu_vendor.upper()} GPU detected but no driver",
                hint=get_hint(hint_key, info.os_id),
                blocking=False,
            ))
    else:
        results.append(CheckResult(
            name="GPU",
            ok=True,
            message="None detected (CPU-only mode)",
            blocking=False,
        ))

    # 5. LLM server
    if info.llm_reachable:
        results.append(CheckResult(
            name="LLM server",
            ok=True,
            message=info.llm_url,
        ))
    else:
        llm_key = f"llm_{info.gpu_vendor}" if info.gpu_vendor != "none" else "llm_cpu"
        results.append(CheckResult(
            name="LLM server",
            ok=False,
            message=f"Not reachable at {info.llm_url}",
            hint=get_hint(llm_key, info.os_id),
        ))

    return results


def print_checks(results: list[CheckResult]) -> bool:
    """Print check results. Returns True if all blocking checks pass."""
    all_ok = True
    for r in results:
        if r.ok:
            icon = f"{GREEN}✓{RESET}"
        elif r.blocking:
            icon = f"{RED}✗{RESET}"
            all_ok = False
        else:
            icon = f"{YELLOW}⚠{RESET}"

        print(f"  {icon} {r.name}: {r.message}")
        if not r.ok and r.hint:
            for line in r.hint.split("\n"):
                print(f"    {DIM}{line}{RESET}")
            print()

    return all_ok


def preflight(require_llm: bool = True, require_image: bool = True) -> bool:
    """Run preflight checks and print results.

    Returns True if the environment is ready.
    """
    print(f"\n{BOLD}Checking environment...{RESET}\n")
    info = detect_system()
    results = run_checks(info)

    # Optionally relax checks
    if not require_llm:
        for r in results:
            if r.name == "LLM server":
                r.blocking = False
    if not require_image:
        for r in results:
            if r.name == "Agent image":
                r.blocking = False

    ok = print_checks(results)
    if ok:
        print(f"\n  {GREEN}Environment ready.{RESET}\n")
    else:
        print(f"\n  {RED}Fix the issues above, then re-run.{RESET}\n")
    return ok


if __name__ == "__main__":
    ok = preflight()
    sys.exit(0 if ok else 1)
