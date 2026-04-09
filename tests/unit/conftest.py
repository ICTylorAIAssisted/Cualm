"""Shared fixtures for unit tests."""

import sys
from pathlib import Path

import pytest

# Add project root and test scripts to path
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "scripts"))


@pytest.fixture
def mock_llm():
    """Start a mock LLM server, yield it, stop on cleanup."""
    from mock_llm import MockLLMServer
    server = MockLLMServer(port=0)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def mock_llm_with_script():
    """Factory fixture — create mock LLM with a specific script."""
    servers = []

    def _create(script, **kwargs):
        from mock_llm import MockLLMServer
        server = MockLLMServer(script=script, port=0, **kwargs)
        server.start()
        servers.append(server)
        return server

    yield _create

    for s in servers:
        s.stop()
