"""Unit tests for XMPP client helpers."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "demo"))
from xmpp_client import extract_bodies


class TestExtractBodies:
    """Test XML body extraction from XMPP message fragments."""

    def test_simple_message(self):
        xml = '<message from="agent@cua.local" type="chat"><body>hello world</body></message>'
        bodies = extract_bodies(xml)
        assert bodies == ["hello world"]

    def test_multiple_messages(self):
        xml = (
            '<message from="a@b" type="chat"><body>first</body></message>'
            '<message from="a@b" type="chat"><body>second</body></message>'
        )
        bodies = extract_bodies(xml)
        assert bodies == ["first", "second"]

    def test_empty_body(self):
        xml = '<message from="a@b" type="chat"><body></body></message>'
        bodies = extract_bodies(xml)
        assert bodies == []

    def test_no_body(self):
        xml = '<message from="a@b" type="chat"><subject>test</subject></message>'
        bodies = extract_bodies(xml)
        assert bodies == []

    def test_groupchat(self):
        xml = '<message from="room@conf/nick" type="groupchat"><body>room msg</body></message>'
        bodies = extract_bodies(xml)
        assert bodies == ["room msg"]

    def test_html_entities(self):
        """XML entities should be decoded."""
        xml = '<message from="a@b" type="chat"><body>today&apos;s article</body></message>'
        bodies = extract_bodies(xml)
        assert bodies == ["today's article"]

    def test_malformed_xml_fallback(self):
        """Falls back to regex for truly malformed XML."""
        xml = '<broken<<< <body>found it</body> >>>'
        bodies = extract_bodies(xml)
        assert bodies == ["found it"]

    def test_multiline_body(self):
        xml = '<message from="a@b" type="chat"><body>line 1\nline 2\nline 3</body></message>'
        bodies = extract_bodies(xml)
        assert len(bodies) == 1
        assert "line 1" in bodies[0]
        assert "line 3" in bodies[0]

    def test_emoji_in_body(self):
        xml = '<message from="a@b" type="chat"><body>✅ #1: Task done</body></message>'
        bodies = extract_bodies(xml)
        assert bodies == ["✅ #1: Task done"]


class TestCompletionDetection:
    """Test the regex patterns used for task completion detection."""

    import re

    COMPLETION_RE = re.compile(r'^(?:✅|⚠️|❌|🛑)\s*#\d+:')

    def test_completed(self):
        assert self.COMPLETION_RE.match("✅ #1: Found heading")

    def test_warning(self):
        assert self.COMPLETION_RE.match("⚠️ #1: Task did not complete")

    def test_error(self):
        assert self.COMPLETION_RE.match("❌ #1: Internal error")

    def test_cancelled(self):
        assert self.COMPLETION_RE.match("🛑 #1: Cancelled after 5 steps.")

    def test_step_output_not_matched(self):
        """Agent step output with emoji should NOT match."""
        assert not self.COMPLETION_RE.match("⚠️ Output: JS Error: TypeError")

    def test_plan_done_not_matched(self):
        """Plan [DONE] markers should NOT match."""
        assert not self.COMPLETION_RE.match("[DONE] Step A: Navigate")

    def test_task_complete_marker(self):
        """The room close marker should be detected separately."""
        assert "── Task complete ──" in "── Task complete ──"

    def test_multi_digit_task(self):
        assert self.COMPLETION_RE.match("✅ #123: Done")
