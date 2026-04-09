"""Unit tests for cualm CLI argument parsing."""

import sys
import subprocess
from pathlib import Path

import pytest

CUALM = str(Path(__file__).parent.parent.parent / "cualm")


class TestCLIHelp:
    """Test that CLI help works for all subcommands."""

    def test_main_help(self):
        r = subprocess.run([sys.executable, CUALM, "--help"],
                           capture_output=True, text=True, timeout=5)
        assert r.returncode == 0
        assert "cualm" in r.stdout.lower()
        assert "agent" in r.stdout
        assert "serve" in r.stdout
        assert "benchmark" in r.stdout
        assert "selftest" in r.stdout

    def test_agent_help(self):
        r = subprocess.run([sys.executable, CUALM, "agent", "--help"],
                           capture_output=True, text=True, timeout=5)
        assert r.returncode == 0
        assert "task" in r.stdout.lower()

    def test_selftest_help(self):
        r = subprocess.run([sys.executable, CUALM, "selftest", "--help"],
                           capture_output=True, text=True, timeout=5)
        assert r.returncode == 0
        assert "--full" in r.stdout

    def test_setup_help(self):
        r = subprocess.run([sys.executable, CUALM, "setup", "--help"],
                           capture_output=True, text=True, timeout=5)
        assert r.returncode == 0
        assert "--skip-test" in r.stdout

    def test_demo_help(self):
        r = subprocess.run([sys.executable, CUALM, "demo", "--help"],
                           capture_output=True, text=True, timeout=5)
        assert r.returncode == 0
        assert "webarena" in r.stdout
        assert "wikipedia" in r.stdout

    def test_no_args_shows_help(self):
        r = subprocess.run([sys.executable, CUALM],
                           capture_output=True, text=True, timeout=5)
        assert r.returncode == 0
        assert "usage" in r.stdout.lower() or "cualm" in r.stdout.lower()

    def test_unknown_command(self):
        r = subprocess.run([sys.executable, CUALM, "nonexistent"],
                           capture_output=True, text=True, timeout=5)
        # argparse returns error for unknown subcommand
        assert r.returncode != 0
