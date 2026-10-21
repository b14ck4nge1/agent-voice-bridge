"""Route Codex app-server approval requests through the bridge.

An approval maps to ``accept`` only when the bridge reports APPROVED. Everything
else (rejection, expiry, missed call, bridge failure) maps to ``decline``.
``acceptForSession`` and policy amendments are never sent: one phone approval
covers one operation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_voice_bridge.agents.codex.app_server import APPROVAL_METHODS, CodexAppServer
from agent_voice_bridge.agents.hook import BridgeClient, BridgeError, Outcome
from agent_voice_bridge.agents.hook import decide as hook_decide

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 4000


def to_hook_input(
    method: str, params: dict[str, Any], server: CodexAppServer, workspace: str
) -> dict[str, Any]:
    """Express an app-server approval in the PermissionRequest shape the bridge client uses."""
    thread_id = str(params.get("threadId", ""))
    if method == "item/commandExecution/requestApproval":
        tool_name = "commandExecution"
        tool_input: dict[str, Any] = {
            "command": params.get("command") or "",
            "cwd": params.get("cwd") or workspace,
        }
        if params.get("networkApprovalContext"):
            tool_input["network"] = params["networkApprovalContext"]
    elif method == "item/fileChange/requestApproval":
        tool_name = "fileChange"
        changes = server.file_changes.get(str(params.get("itemId")), [])
        tool_input = {
            "changes": [
                {
                    "path": c.get("path"),
                    "kind": (c.get("kind") or {}).get("type"),
                    "diff": str(c.get("diff", ""))[:MAX_DIFF_CHARS],
                }
                for c in changes
                if isinstance(c, dict)
            ],
        }
        if params.get("grantRoot"):
            tool_input["grant_root"] = params["grantRoot"]
    else:
        raise BridgeError(f"unsupported approval method {method}")
    return {
        "hook_event_name": "PermissionRequest",
        "session_id": thread_id or "codex-unknown-thread",
        "turn_id": params.get("turnId"),
        "cwd": workspace,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "reason": params.get("reason"),
    }


class CodexApprovalRouter:
    def __init__(self, client: BridgeClient, workspace: str, wait_seconds: int = 300) -> None:
        self.client = client
        self.workspace = workspace
        self.wait_seconds = wait_seconds

    async def __call__(
        self, method: str, params: dict[str, Any], server: CodexAppServer
    ) -> dict[str, Any]:
        if method not in APPROVAL_METHODS:
            raise BridgeError(f"{method} is not supported by Agent Voice Bridge yet")
        hook_input = to_hook_input(method, params, server, self.workspace)
        try:
            outcome: Outcome = await asyncio.to_thread(
                hook_decide,
                hook_input,
                agent="codex",
                client=self.client,
                wait_seconds=self.wait_seconds,
            )
        except BridgeError as exc:
            log.warning("no phone decision (%s); declining", exc)
            return {"decision": "decline"}
        return {"decision": "accept" if outcome.behavior == "allow" else "decline"}
