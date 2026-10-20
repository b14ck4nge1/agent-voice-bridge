"""The brain through an API gateway: the real SDKs against a local stand-in server."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from agent_voice_bridge.assistant.brain import brain_test

SEEN: list[dict[str, Any]] = []


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization") or self.headers.get("x-api-key"),
                "body": body,
            }
        )
        first = len(body["messages"]) <= 2
        if self.path.endswith("/chat/completions"):
            msg: dict[str, Any] = (
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "get_time", "arguments": "{}"},
                        }
                    ],
                }
                if first
                else {"role": "assistant", "content": "It's 10:42."}
            )
            out: dict[str, Any] = {
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {"index": 0, "message": msg, "finish_reason": "tool_calls" if first else "stop"}
                ],
            }
        else:  # Anthropic-style /v1/messages
            content = (
                [{"type": "tool_use", "id": "tu1", "name": "get_time", "input": {}}]
                if first
                else [{"type": "text", "text": "It's 10:42."}]
            )
            out = {
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": body["model"],
                "content": content,
                "stop_reason": "tool_use" if first else "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def gateway() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    SEEN.clear()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_brain_via_gateway(
    gateway: str, provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip(provider)
    monkeypatch.setenv("AVB_BRAIN", provider)
    monkeypatch.setenv("AVB_BRAIN_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("AVB_BRAIN_BASE_URL", gateway + ("/v1" if provider == "openai" else ""))
    monkeypatch.setenv("AVB_BRAIN_API_KEY", "sk-gateway-key")
    assert await brain_test() == 0
    assert SEEN[0]["path"] == ("/v1/chat/completions" if provider == "openai" else "/v1/messages")
    assert "sk-gateway-key" in SEEN[0]["auth"]
    assert SEEN[0]["body"]["model"] == "claude-haiku-4-5"
