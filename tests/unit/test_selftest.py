"""Unit tests for cualm selftest module."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from cualm_selftest import _run, _runtime, LEVELS, LEVEL_NAMES


class TestRunHelper:

    def test_successful_command(self):
        ok, out, err = _run(["echo", "hello"])
        assert ok is True
        assert out == "hello"

    def test_failed_command(self):
        ok, out, err = _run(["false"])
        assert ok is False

    def test_missing_command(self):
        ok, out, err = _run(["nonexistent_cmd_xyz"])
        assert ok is False
        assert err  # should have error message

    def test_timeout(self):
        ok, out, err = _run(["sleep", "10"], timeout=1)
        assert ok is False
        assert "timed out" in err.lower() or "timeout" in err.lower()


class TestLevelStructure:

    def test_all_levels_have_names(self):
        for level in LEVELS:
            assert level in LEVEL_NAMES

    def test_levels_have_tests(self):
        for level, tests in LEVELS.items():
            assert len(tests) > 0, f"Level {level} has no tests"
            for name, fn in tests:
                assert callable(fn), f"Level {level} test '{name}' is not callable"

    def test_level_names_are_descriptive(self):
        assert "Infrastructure" in LEVEL_NAMES[1]
        assert "Image" in LEVEL_NAMES[2]
        assert "LLM" in LEVEL_NAMES[3]
        assert "End" in LEVEL_NAMES[4]


class TestRuntimeDetection:

    def test_returns_string_or_none(self):
        result = _runtime()
        assert result is None or isinstance(result, str)

    @patch("cualm_selftest._run")
    def test_prefers_podman(self, mock_run):
        """If both podman and docker exist, podman is found first."""
        mock_run.return_value = (True, "5.0", "")
        result = _runtime()
        # _runtime iterates ["podman", "docker"], returns first match
        assert result == "podman"

    @patch("cualm_selftest._run")
    def test_falls_back_to_docker(self, mock_run):
        def side_effect(cmd, **kwargs):
            if cmd[0] == "podman":
                return (False, "", "not found")
            return (True, "24.0", "")
        mock_run.side_effect = side_effect
        result = _runtime()
        assert result == "docker"

    @patch("cualm_selftest._run")
    def test_returns_none_if_nothing(self, mock_run):
        mock_run.return_value = (False, "", "not found")
        result = _runtime()
        assert result is None
