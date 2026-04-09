"""Unit tests for the mock LLM server."""

import json
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tests" / "scripts"))
from mock_llm import MockLLMServer, NAVIGATE_AND_READ, FORM_FILL


class TestMockLLMServer:
    """Test the mock LLM server itself."""

    def test_models_endpoint(self, mock_llm):
        resp = urllib.request.urlopen(f"{mock_llm.url}/models")
        data = json.loads(resp.read())
        assert data["object"] == "list"
        assert len(data["data"]) > 0

    def test_health_endpoint(self, mock_llm):
        resp = urllib.request.urlopen(f"{mock_llm.url.replace('/v1', '')}/health")
        data = json.loads(resp.read())
        assert data["status"] == "ok"

    def test_completion_basic(self, mock_llm):
        body = json.dumps({
            "model": "test",
            "messages": [{"role": "user", "content": "hello"}],
        }).encode()
        req = urllib.request.Request(
            f"{mock_llm.url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        assert "choices" in data
        assert data["choices"][0]["message"]["role"] == "assistant"

    def test_default_response(self, mock_llm):
        """Unmatched prompts get the default response."""
        body = json.dumps({
            "model": "test",
            "messages": [{"role": "user", "content": "random gibberish xyz"}],
        }).encode()
        req = urllib.request.Request(
            f"{mock_llm.url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"]
        assert "cua-done" in text

    def test_scripted_matching(self, mock_llm_with_script):
        server = mock_llm_with_script([
            {"match": r"navigate", "response": "I will navigate"},
            {"match": r"click", "response": "I will click"},
        ])

        def complete(prompt):
            body = json.dumps({
                "model": "test",
                "messages": [{"role": "user", "content": prompt}],
            }).encode()
            req = urllib.request.Request(
                f"{server.url}/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req)
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]

        assert complete("Please navigate to google.com") == "I will navigate"
        assert complete("Now click the button") == "I will click"
        assert server.call_count == 2

    def test_request_logging(self, mock_llm):
        body = json.dumps({
            "model": "test",
            "messages": [{"role": "user", "content": "test logging"}],
        }).encode()
        req = urllib.request.Request(
            f"{mock_llm.url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req)
        assert len(mock_llm.requests) >= 1
        assert mock_llm.requests[-1]["messages"][-1]["content"] == "test logging"

    def test_callable_response(self, mock_llm_with_script):
        server = mock_llm_with_script([
            {"match": r".*", "response": lambda p, n: f"call {n}: {len(p)} chars"},
        ])

        body = json.dumps({
            "model": "test",
            "messages": [{"role": "user", "content": "hello world"}],
        }).encode()
        req = urllib.request.Request(
            f"{server.url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"]
        assert text.startswith("call 1:")

    def test_multimodal_prompt(self, mock_llm_with_script):
        """Text extracted from multimodal messages."""
        server = mock_llm_with_script([
            {"match": r"what do you see", "response": "I see an image"},
        ])

        body = json.dumps({
            "model": "test",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    {"type": "text", "text": "what do you see in this screenshot?"},
                ],
            }],
        }).encode()
        req = urllib.request.Request(
            f"{server.url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        assert data["choices"][0]["message"]["content"] == "I see an image"

    def test_reset(self, mock_llm):
        body = json.dumps({
            "model": "test",
            "messages": [{"role": "user", "content": "test"}],
        }).encode()
        req = urllib.request.Request(
            f"{mock_llm.url}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req)
        assert mock_llm.call_count >= 1
        mock_llm.reset()
        assert mock_llm.call_count == 0
        assert len(mock_llm.requests) == 0
