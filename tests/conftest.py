from __future__ import annotations

import asyncio
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn

from agent_voice_bridge.api.app import create_app
from agent_voice_bridge.approvals.service import ApprovalService
from agent_voice_bridge.config import Settings
from agent_voice_bridge.database.store import Store
from agent_voice_bridge.models import AgentKind, ApprovalCreate, ApprovalView

AGENT_TOKEN = "agent-" + "a" * 40
VOICE_TOKEN = "voice-" + "v" * 40
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


class RecordingDispatcher:
    def __init__(self) -> None:
        self.requested: list[ApprovalView] = []
        self.cancelled: list[str] = []

    async def request_call(self, request: ApprovalView) -> None:
        self.requested.append(request)

    async def cancel_call(self, request_id: str, call_id: str | None) -> None:
        self.cancelled.append(request_id)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "bridge.sqlite3",
        agent_token=AGENT_TOKEN,
        voice_token=VOICE_TOKEN,
        totp_secret=TOTP_SECRET,
        redial_delay_seconds=0.0,
        sweep_interval_seconds=0.2,
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
def service(settings: Settings, dispatcher: RecordingDispatcher, clock: Clock) -> ApprovalService:
    return ApprovalService(Store(settings.db_path), settings, dispatcher, clock=clock)


def make_create(**overrides: object) -> ApprovalCreate:
    data: dict[str, object] = {
        "agent": AgentKind.CLAUDE_CODE,
        "session_id": "sess-1",
        "workspace": "C:\\Users\\dev\\frontend",
        "tool_name": "Bash",
        "tool_input": {"command": "npm install", "description": "Install dependencies"},
        "idempotency_key": secrets.token_hex(8),
        "request_secret": secrets.token_urlsafe(32),
    }
    data.update(overrides)
    return ApprovalCreate.model_validate(data)


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@dataclass
class LiveBridge:
    url: str
    settings: Settings
    dispatcher: RecordingDispatcher


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live_bridge(settings: Settings) -> Iterator[LiveBridge]:
    port = _free_port()
    live_settings = replace(settings, port=port)
    disp = RecordingDispatcher()
    app = create_app(live_settings, dispatcher=disp)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        raise RuntimeError("bridge did not start")
    yield LiveBridge(url, live_settings, disp)
    server.should_exit = True
    thread.join(timeout=10)
