"""``avb-codex run``: drive a Codex thread whose approvals go to your phone.

Starts its own ``codex app-server``, opens a thread in the workspace with
``approvalPolicy=on-request`` and ``sandbox=workspace-write``, sends one prompt and
streams the agent's reply. Approval requests are routed through the bridge.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from agent_voice_bridge.agents.codex.app_server import CodexAppServer
from agent_voice_bridge.agents.codex.approvals import CodexApprovalRouter
from agent_voice_bridge.agents.hook import DEFAULT_URL, BridgeClient
from agent_voice_bridge.config import default_data_dir, load_env_file


async def run(args: argparse.Namespace) -> int:
    workspace = str(Path(args.cwd).resolve())
    client = BridgeClient(
        os.environ.get("AVB_BRIDGE_URL", DEFAULT_URL), os.environ["AVB_AGENT_TOKEN"]
    )
    done = asyncio.Event()
    status: dict[str, Any] = {}

    def on_note(method: str, params: dict[str, Any]) -> None:
        if method == "item/agentMessage/delta":
            sys.stdout.write(str(params.get("delta", "")))
            sys.stdout.flush()
        elif method == "turn/completed":
            status["turn"] = params.get("turn")
            done.set()

    server = CodexAppServer(
        shlex.split(args.codex_command, posix=os.name != "nt"),
        on_request=CodexApprovalRouter(client, workspace, args.wait_seconds),
        on_notification=on_note,
        cwd=workspace,
    )
    await server.start()
    try:
        started = await server.request(
            "thread/start",
            {"cwd": workspace, "approvalPolicy": "on-request", "sandbox": args.sandbox},
        )
        thread_id = started["thread"]["id"]
        await server.request(
            "turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": args.prompt}]}
        )
        await done.wait()
    finally:
        await server.close()
    turn = status.get("turn") or {}
    print(f"\n[turn {turn.get('id', '?')} finished: {turn.get('status', 'unknown')}]")
    return 0


def main(argv: list[str] | None = None) -> None:
    load_env_file(default_data_dir() / "hook.env")
    parser = argparse.ArgumentParser(prog="avb-codex")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="Run one Codex turn with phone approvals")
    p.add_argument("prompt")
    p.add_argument("--cwd", default=".")
    p.add_argument("--sandbox", default="workspace-write", choices=["read-only", "workspace-write"])
    p.add_argument("--wait-seconds", type=int, default=300)
    p.add_argument(
        "--codex-command", default=os.environ.get("AVB_CODEX_COMMAND", "codex app-server")
    )
    args = parser.parse_args(argv)
    if not os.environ.get("AVB_AGENT_TOKEN"):
        sys.exit("AVB_AGENT_TOKEN is not set (run avb-admin init)")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
