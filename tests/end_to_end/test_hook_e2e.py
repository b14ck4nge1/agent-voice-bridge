"""Real avb-hook processes against a live bridge, answered by the conversation engine.

This is the full software path Claude Code / Codex would exercise, with the phone
call replaced by text. It does not involve SIP, audio or a real agent.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from agent_voice_bridge.models import ApprovalState, CallEvent
from agent_voice_bridge.models import CallEventKind as E
from agent_voice_bridge.voice.conversation.engine import Conversation
from agent_voice_bridge.voice.conversation.ops import HttpOps
from tests.conftest import AGENT_TOKEN, VOICE_TOKEN, LiveBridge


def claude_input(
    session: str, command: str, cwd: str = "C:\\Users\\dev\\frontend"
) -> dict[str, Any]:
    # Shape documented for Claude Code's PermissionRequest hook.
    return {
        "session_id": session,
        "transcript_path": f"C:\\Users\\dev\\.claude\\projects\\x\\{session}.jsonl",
        "cwd": cwd,
        "permission_mode": "default",
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "test"},
        "permission_suggestions": [
            {
                "type": "addRules",
                "rules": [{"toolName": "Bash"}],
                "behavior": "allow",
                "destination": "localSettings",
            }
        ],
    }


def codex_input(session: str, command: str) -> dict[str, Any]:
    # Shape from codex-rs/hooks/schema/generated/permission-request.command.input.schema.json
    return {
        "session_id": session,
        "turn_id": "turn-1",
        "cwd": "/home/dev/api",
        "hook_event_name": "PermissionRequest",
        "model": "gpt-x",
        "permission_mode": "default",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "transcript_path": None,
    }


async def run_hook(
    bridge_url: str,
    hook_input: dict[str, Any],
    *,
    agent: str = "claude-code",
    extra: list[str] | None = None,
    token: str = AGENT_TOKEN,
    tmp: Path,
) -> asyncio.subprocess.Process:
    env = {
        **os.environ,
        "AVB_AGENT_TOKEN": token,
        "AVB_BRIDGE_URL": bridge_url,
        "AVB_HOOK_ENV": str(tmp / "missing.env"),
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "agent_voice_bridge.agents.hook",
        "--agent",
        agent,
        *(extra or []),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(hook_input).encode())
    await proc.stdin.drain()
    proc.stdin.close()
    return proc


async def finish(proc: asyncio.subprocess.Process) -> tuple[dict[str, Any] | None, str]:
    out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    assert proc.returncode == 0
    text = out.decode().strip()
    return (json.loads(text) if text else None), err.decode()


async def answer_call(ops: HttpOps, n: int, script: list[str]) -> Conversation:
    for _ in range(200):
        pending = [p for p in await ops.pending() if p.state == ApprovalState.PENDING_CALL]
        if len(pending) >= n:
            break
        await asyncio.sleep(0.05)
    ids = [p.id for p in pending]
    for rid in ids:
        await ops.call_event(rid, CallEvent(event=E.DIALING, call_id="call-e2e"))
        await ops.call_event(rid, CallEvent(event=E.RINGING, call_id="call-e2e"))
    convo = Conversation(ops, ids, "call-e2e")
    await convo.start()
    for line in script:
        await convo.hear(line)
    return convo


def decision(out: dict[str, Any] | None) -> dict[str, Any]:
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PermissionRequest"
    # Never widen permissions or rewrite the call.
    assert set(hso["decision"]) <= {"behavior", "message"}
    return hso["decision"]  # type: ignore[no-any-return]


async def test_claude_hook_approved_by_phone(live_bridge: LiveBridge, tmp_path: Path) -> None:
    ops = HttpOps(live_bridge.url, VOICE_TOKEN)
    proc = await run_hook(live_bridge.url, claude_input("s1", "npm install"), tmp=tmp_path)
    await answer_call(ops, 1, ["What command?", "approve", "yes"])
    out, _ = await finish(proc)
    assert decision(out) == {"behavior": "allow"}
    await ops.aclose()


async def test_codex_hook_rejected_with_feedback(live_bridge: LiveBridge, tmp_path: Path) -> None:
    ops = HttpOps(live_bridge.url, VOICE_TOKEN)
    proc = await run_hook(
        live_bridge.url,
        codex_input("thread-9", "pip install requests"),
        agent="codex",
        tmp=tmp_path,
    )
    await answer_call(ops, 1, ["reject it, use uv add instead", "yes"])
    out, _ = await finish(proc)
    d = decision(out)
    assert d["behavior"] == "deny" and "use uv add instead" in d["message"]
    await ops.aclose()


async def test_two_concurrent_sessions_get_their_own_decisions(
    live_bridge: LiveBridge, tmp_path: Path
) -> None:
    ops = HttpOps(live_bridge.url, VOICE_TOKEN)
    p1 = await run_hook(
        live_bridge.url, claude_input("sess-A", "npm test", cwd="/home/dev/alpha"), tmp=tmp_path
    )
    await asyncio.sleep(0.5)  # make the order deterministic: A is presented first
    p2 = await run_hook(
        live_bridge.url, claude_input("sess-B", "npm run build", cwd="/home/dev/beta"), tmp=tmp_path
    )
    await answer_call(ops, 2, ["approve", "yes", "reject", "yes"])
    out1, _ = await finish(p1)
    out2, _ = await finish(p2)
    assert decision(out1)["behavior"] == "allow"
    assert decision(out2)["behavior"] == "deny"
    await ops.aclose()


async def test_timeout_denies_and_cancels(live_bridge: LiveBridge, tmp_path: Path) -> None:
    ops = HttpOps(live_bridge.url, VOICE_TOKEN)
    proc = await run_hook(
        live_bridge.url,
        claude_input("s-timeout", "ls"),
        extra=["--wait-seconds", "2"],
        tmp=tmp_path,
    )
    out, err = await finish(proc)
    d = decision(out)
    assert d["behavior"] == "deny" and "denied by default" in d["message"]
    assert "no decision" in err or "EXPIRED" in err
    assert [p for p in await ops.pending()] == []
    await ops.aclose()


async def test_bridge_down_denies(tmp_path: Path) -> None:
    proc = await run_hook("http://127.0.0.1:9", claude_input("s", "ls"), tmp=tmp_path)
    out, err = await finish(proc)
    assert decision(out)["behavior"] == "deny"
    assert "unreachable" in err


async def test_bridge_down_ask_mode_defers_to_local_prompt(tmp_path: Path) -> None:
    proc = await run_hook(
        "http://127.0.0.1:9", claude_input("s", "ls"), extra=["--on-failure", "ask"], tmp=tmp_path
    )
    out, _ = await finish(proc)
    assert out is None  # no decision: the agent shows its normal permission prompt


async def test_invalid_token_denies(live_bridge: LiveBridge, tmp_path: Path) -> None:
    proc = await run_hook(
        live_bridge.url, claude_input("s", "ls"), token="wrong-" + "x" * 40, tmp=tmp_path
    )
    out, err = await finish(proc)
    assert decision(out)["behavior"] == "deny" and "401" in err


async def test_remote_plain_http_is_refused(tmp_path: Path) -> None:
    proc = await run_hook("http://bridge.example.com:8765", claude_input("s", "ls"), tmp=tmp_path)
    out, err = await finish(proc)
    assert decision(out)["behavior"] == "deny" and "https" in err


async def test_non_permission_event_is_denied_not_crashed(tmp_path: Path) -> None:
    proc = await run_hook("http://127.0.0.1:9", {"hook_event_name": "PreToolUse"}, tmp=tmp_path)
    out, err = await finish(proc)
    assert decision(out)["behavior"] == "deny" and "unsupported hook event" in err
