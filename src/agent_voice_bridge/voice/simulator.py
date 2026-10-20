"""``avb-voice-sim``: a text-mode stand-in for the phone call.

It watches the bridge for requests waiting for a call, "rings" you in the terminal,
and runs the same conversation engine the phone call will use. Type what you would
say. This exercises the real hook -> bridge -> decision -> agent path end to end,
without SIP or speech. It is a development tool, not a substitute for the phone.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys

from agent_voice_bridge.approvals.service import ServiceError
from agent_voice_bridge.config import default_data_dir, load_env_file
from agent_voice_bridge.models import ApprovalState, CallEvent, CallEventKind
from agent_voice_bridge.voice.conversation.engine import Conversation
from agent_voice_bridge.voice.conversation.ops import HttpOps


async def _ainput(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def run(base_url: str, token: str, poll: float) -> None:
    ops = HttpOps(base_url, token)
    print(f"Watching {base_url} for approval requests. Ctrl+C to stop.")
    try:
        while True:
            pending = [r for r in await ops.pending() if r.state == ApprovalState.PENDING_CALL]
            if not pending:
                await asyncio.sleep(poll)
                continue
            call_id = "sim_" + secrets.token_hex(6)
            ids = []
            for req in pending:
                try:
                    await ops.call_event(
                        req.id, CallEvent(event=CallEventKind.DIALING, call_id=call_id)
                    )
                    await ops.call_event(
                        req.id, CallEvent(event=CallEventKind.RINGING, call_id=call_id)
                    )
                    ids.append(req.id)
                except ServiceError as exc:
                    print(f"(skipping {req.id}: {exc.message})")
            if not ids:
                continue
            answer = (
                (await _ainput(f"\n*** RING RING: {len(ids)} request(s). Answer? [Y/n] "))
                .strip()
                .lower()
            )
            convo = Conversation(ops, ids, call_id)
            if answer in {"n", "no"}:
                for rid in ids:
                    await ops.call_event(
                        rid, CallEvent(event=CallEventKind.NO_ANSWER, call_id=call_id)
                    )
                print("(declined)")
                continue
            turn = await convo.start()
            while True:
                for line in turn.say:
                    print(f"ASSISTANT: {line}")
                if turn.ended:
                    break
                try:
                    heard = await _ainput("YOU: ")
                except EOFError:
                    await convo.hangup()
                    print("(hung up)")
                    break
                if heard.strip().lower() in {"/hangup", "/bye"}:
                    await convo.hangup()
                    print("(hung up)")
                    break
                turn = await convo.hear(heard)
    finally:
        await ops.aclose()


def main(argv: list[str] | None = None) -> None:
    load_env_file(default_data_dir() / "bridge.env")
    parser = argparse.ArgumentParser(prog="avb-voice-sim", description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=os.environ.get("AVB_BRIDGE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--poll", type=float, default=1.0)
    args = parser.parse_args(argv)
    token = os.environ.get("AVB_VOICE_TOKEN")
    if not token:
        sys.exit("AVB_VOICE_TOKEN is not set (run avb-admin init, or set it in the environment)")
    try:
        asyncio.run(run(args.url, token, args.poll))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
