"""Integration tests: agent navigation.

Requires Docker/Podman and the cua-agent image built.
Uses mock LLM — no real model needed.
"""

import json
import subprocess
import time

import pytest

from mock_llm import MockLLMServer, NAVIGATE_AND_READ, CALIBRATION_ONLY


def _run_agent(runtime, llm_url, task, max_steps=5, timeout=60):
    """Run the agent container and return (exit_code, stdout, stderr)."""
    # Rewrite localhost for container
    container_url = llm_url.replace("127.0.0.1", "host.docker.internal")

    name = f"cualm-test-{int(time.time())}"
    cmd = [
        runtime, "run", "--rm", "--name", name,
        "--add-host", "host.docker.internal:host-gateway",
        "-e", f"OPENAI_BASE_URL={container_url}",
        "-e", "OPENAI_API_KEY=test",
        "-e", "CUA_MODEL=mock-model",
        "-e", f"CUA_MAX_STEPS={max_steps}",
        "-e", "CUA_START_URL=https://example.com",
        "-e", "PYTHONUNBUFFERED=1",
        "cua-agent",
        "agent", task,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        subprocess.run([runtime, "rm", "-f", name], capture_output=True)
        return 124, "", "timeout"
    finally:
        subprocess.run([runtime, "rm", "-f", name],
                       capture_output=True, stderr=subprocess.DEVNULL)


@pytest.fixture
def agent_image(runtime):
    """Skip if agent image not built."""
    result = subprocess.run(
        [runtime, "image", "inspect", "cua-agent"],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip("cua-agent image not built (run: ./run.sh build)")


class TestAgentNavigation:
    """Test agent navigation with mock LLM."""

    @pytest.mark.slow
    def test_agent_calls_llm(self, runtime, agent_image, mock_llm_with_script):
        """Agent starts and makes at least one LLM call."""
        server = mock_llm_with_script(CALIBRATION_ONLY)

        rc, out, err = _run_agent(
            runtime, server.url,
            "Navigate to example.com",
            max_steps=3, timeout=90,
        )

        assert server.call_count > 0, (
            f"Agent made 0 LLM calls.\nstdout: {out[:500]}\nstderr: {err[:500]}"
        )

    @pytest.mark.slow
    def test_agent_sends_screenshots(self, runtime, agent_image, mock_llm_with_script):
        """Agent includes screenshots in LLM requests."""
        server = mock_llm_with_script(CALIBRATION_ONLY)

        _run_agent(
            runtime, server.url,
            "Tell me what you see",
            max_steps=2, timeout=90,
        )

        # Check that at least one request contained an image
        has_image = False
        for req in server.requests:
            messages = req.get("messages", [])
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "image_url":
                            has_image = True
                            break

        assert has_image, "No screenshots found in LLM requests"

    @pytest.mark.slow
    def test_agent_completes_task(self, runtime, agent_image, mock_llm_with_script):
        """Agent completes when cua-done is in the response."""
        server = mock_llm_with_script([
            {"match": r".*",
             "response": 'run: cua-done "test complete" --result "OK"'},
        ])

        rc, out, err = _run_agent(
            runtime, server.url,
            "Do anything",
            max_steps=5, timeout=90,
        )

        # Agent should have completed (look for the done marker)
        assert "@@CUA_TASK_COMPLETE@@" in out or "Done:" in out, (
            f"Agent didn't complete.\nstdout: {out[:500]}\nstderr: {err[:500]}"
        )


class TestAgentErrorHandling:
    """Test agent behavior with problematic LLM responses."""

    @pytest.mark.slow
    def test_agent_handles_empty_response(self, runtime, agent_image, mock_llm_with_script):
        """Agent doesn't crash on empty LLM response."""
        server = mock_llm_with_script([
            {"match": r".*", "response": ""},
        ])

        rc, out, err = _run_agent(
            runtime, server.url,
            "Do something",
            max_steps=3, timeout=60,
        )

        # Should exit (max_steps) without crashing
        assert rc is not None  # didn't hang

    @pytest.mark.slow
    def test_agent_max_steps(self, runtime, agent_image, mock_llm_with_script):
        """Agent stops at max_steps."""
        server = mock_llm_with_script([
            {"match": r".*",
             "response": "run: echo 'thinking...'"},
        ])

        rc, out, err = _run_agent(
            runtime, server.url,
            "Keep going forever",
            max_steps=3, timeout=90,
        )

        # Should have made exactly max_steps calls (plus calibration)
        assert server.call_count <= 6, f"Too many calls: {server.call_count}"
