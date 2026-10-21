from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from agent_voice_bridge.agents.codex.app_server import CodexAppServer
from agent_voice_bridge.agents.codex.approvals import CodexApprovalRouter
from agent_voice_bridge.agents.hook import BridgeClient
from agent_voice_bridge.models import ApprovalState, CallEvent
from agent_voice_bridge.models import CallEventKind as E
from agent_voice_bridge.voice.conversation.engine import Conversation
from agent_voice_bridge.voice.conversation.ops import HttpOps
from tests.conftest import AGENT_TOKEN, VOICE_TOKEN, LiveBridge

FAKE = Path(__file__).with_name("fake_codex_app_server.py")


async def test_app_server_approvals_round_trip(live_bridge: LiveBridge, tmp_path: Path) -> None:
    completed: dict[str, Any] = {}
    done = asyncio.Event()

    def on_note(method: str, params: dict[str, Any]) -> None:
        if method == "turn/completed":
            completed.update(params["turn"])
            done.set()

    router = CodexApprovalRouter(
        BridgeClient(live_bridge.url, AGENT_TOKEN), str(tmp_path), wait_seconds=20
    )
    server = CodexAppServer([sys.executable, str(FAKE)], on_request=router, on_notification=on_note)
    await server.start()
    thread = await server.request(
        "thread/start",
        {"cwd": str(tmp_path), "approvalPolicy": "on-request", "sandbox": "workspace-write"},
    )
    await server.request(
        "turn/start",
        {"threadId": thread["thread"]["id"], "input": [{"type": "text", "text": "hi"}]},
    )

    ops = HttpOps(live_bridge.url, VOICE_TOKEN)
    for _ in range(200):
        pending = [p for p in await ops.pending() if p.state == ApprovalState.PENDING_CALL]
        if len(pending) == 2:
            break
        await asyncio.sleep(0.05)
    by_tool = {p.tool_name: p for p in pending}
    assert set(by_tool) == {"fileChange", "commandExecution"}
    assert by_tool["fileChange"].summary == "change 1 file: src/app.py"
    assert "rm -rf build" in by_tool["commandExecution"].summary
    assert by_tool["commandExecution"].session_id == "thr_1"
    assert by_tool["commandExecution"].reason == "clean build output"

    order = [by_tool["fileChange"].id, by_tool["commandExecution"].id]
    for rid in order:
        await ops.call_event(rid, CallEvent(event=E.DIALING, call_id="c"))
        await ops.call_event(rid, CallEvent(event=E.RINGING, call_id="c"))
    convo = Conversation(ops, order, "c")
    await convo.start()
    for line in ("approve", "yes", "reject", "yes"):
        await convo.hear(line)

    await asyncio.wait_for(done.wait(), timeout=20)
    await server.close()
    await ops.aclose()
    assert completed["answers"] == {"0": "accept", "srv-cmd": "decline"}
    assert completed["sawJsonrpcField"] is False
