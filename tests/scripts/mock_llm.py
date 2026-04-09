#!/usr/bin/env python3
"""Mock LLM server for integration tests.

Returns pre-scripted responses based on regex matching against
the prompt content. Records all requests for assertions.

Usage:
  # As a standalone server:
  python3 tests/scripts/mock_llm.py --port 8111

  # Programmatically:
  from mock_llm import MockLLMServer
  server = MockLLMServer(script=[...])
  server.start()
  # ... run tests ...
  server.stop()
"""

import argparse
import json
import re
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional


class MockLLMHandler(BaseHTTPRequestHandler):
    """Handles /v1/chat/completions and /v1/models."""

    def log_message(self, fmt, *args):
        """Suppress default logging unless verbose."""
        if self.server.verbose:
            super().log_message(fmt, *args)

    def do_GET(self):
        if self.path == "/v1/models":
            self._json_response({
                "object": "list",
                "data": [{"id": "mock-model", "object": "model"}],
            })
        elif self.path == "/health":
            self._json_response({"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        self.server.request_log.append(body)

        # Extract the last user message text
        prompt = ""
        messages = body.get("messages", [])
        if messages:
            last = messages[-1]
            content = last.get("content", "")
            if isinstance(content, str):
                prompt = content
            elif isinstance(content, list):
                # Multimodal: extract text parts
                prompt = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )

        # Match against script
        self.server.call_count += 1
        response_text = self._match_script(prompt)

        # Build response
        response = {
            "id": f"mock-{self.server.call_count}",
            "object": "chat.completion",
            "model": body.get("model", "mock-model"),
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text,
                },
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": len(prompt) // 4,
                "completion_tokens": len(response_text) // 4,
                "total_tokens": (len(prompt) + len(response_text)) // 4,
            },
        }

        self._json_response(response)

    def _match_script(self, prompt: str) -> str:
        """Find the first matching script entry."""
        for entry in self.server.script:
            pattern = entry.get("match", "")
            if re.search(pattern, prompt, re.IGNORECASE | re.DOTALL):
                # Support callable responses
                if callable(entry.get("response")):
                    return entry["response"](prompt, self.server.call_count)
                return entry["response"]

        # Default: agent gives up
        return self.server.default_response

    def _json_response(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MockLLMServer:
    """Scripted LLM server for testing.

    Args:
        script: List of dicts with 'match' (regex) and 'response' (str or callable).
                Matched in order; first match wins.
        port: Port to listen on (0 = auto-assign).
        default_response: Response when nothing matches.
        verbose: Log HTTP requests.

    Example:
        server = MockLLMServer(script=[
            {"match": r"navigate|go to",
             "response": 'run: cua-type "example.com" && cua-key Return'},
            {"match": r"example.com|Example Domain",
             "response": 'run: cua-done "Found it" --result "Example Domain"'},
        ])
        server.start()
        print(f"Mock LLM at: {server.url}")
        # ... run agent ...
        assert server.call_count == 2
        server.stop()
    """

    def __init__(
        self,
        script: list[dict] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        default_response: str = 'run: cua-done "stuck" --result "N/A"',
        verbose: bool = False,
    ):
        self.script = script or []
        self.host = host
        self.port = port
        self.default_response = default_response
        self.verbose = verbose

        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

        # Accessible after start()
        self.url: str = ""
        self._call_count: int = 0
        self._request_log: list[dict] = []

    def start(self):
        """Start server in background thread."""
        self._httpd = HTTPServer((self.host, self.port), MockLLMHandler)
        self._httpd.script = self.script
        self._httpd.default_response = self.default_response
        self._httpd.verbose = self.verbose
        self._httpd.call_count = 0
        self._httpd.request_log = []

        actual_port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{actual_port}/v1"

        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

        # Wait for server to be ready
        for _ in range(50):
            try:
                import urllib.request
                urllib.request.urlopen(f"{self.url}/models", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

        return self

    def stop(self):
        """Stop server."""
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def call_count(self) -> int:
        if self._httpd:
            return self._httpd.call_count
        return self._call_count

    @call_count.setter
    def call_count(self, val):
        self._call_count = val
        if self._httpd:
            self._httpd.call_count = val

    @property
    def requests(self) -> list[dict]:
        """All recorded requests."""
        if self._httpd:
            return self._httpd.request_log
        return self._request_log

    def reset(self):
        """Clear request log and call count."""
        if self._httpd:
            self._httpd.call_count = 0
            self._httpd.request_log = []
        self._call_count = 0
        self._request_log = []

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.stop()


# ── Pre-built test scripts ──────────────────────────────────

NAVIGATE_AND_READ = [
    {"match": r"Step 1|first",
     "response": (
         'run: cua-plan "Step A: Navigate to site | '
         'Step B: Read the heading" && '
         'cua-type "example.com" && cua-key Return'
     )},
    {"match": r"Step 2|Example Domain|example",
     "response": 'run: cua-done "Found heading" --result "Example Domain"'},
]

FORM_FILL = [
    {"match": r"Step 1|first|form",
     "response": (
         'run: cua-plan "Step A: Fill name | Step B: Fill email | '
         'Step C: Submit" && cua-pw fill e1 "John Doe"'
     )},
    {"match": r"Step 2|John",
     "response": 'run: cua-pw fill e2 "john@example.com"'},
    {"match": r"Step 3|john@",
     "response": 'run: cua-pw click e3'},
    {"match": r"Step 4|submitted|success",
     "response": 'run: cua-done "Form submitted" --result "success"'},
]

CALIBRATION_ONLY = [
    # Always give up — used to test that calibration works
    {"match": r".*",
     "response": 'run: cua-done "calibration test" --result "OK"'},
]


def main():
    parser = argparse.ArgumentParser(description="Mock LLM server")
    parser.add_argument("--port", type=int, default=8111)
    parser.add_argument("--script", default="navigate",
                        choices=["navigate", "form", "calibration", "echo"],
                        help="Pre-built script to use")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    scripts = {
        "navigate": NAVIGATE_AND_READ,
        "form": FORM_FILL,
        "calibration": CALIBRATION_ONLY,
        "echo": [{"match": r".*",
                  "response": lambda p, n: f"Echo #{n}: received {len(p)} chars"}],
    }

    server = MockLLMServer(
        script=scripts[args.script],
        port=args.port,
        verbose=args.verbose,
    )

    with server:
        print(f"Mock LLM server running at {server.url}")
        print(f"Script: {args.script} ({len(server.script)} entries)")
        print("Press Ctrl+C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        print(f"\nTotal calls: {server.call_count}")


if __name__ == "__main__":
    main()
