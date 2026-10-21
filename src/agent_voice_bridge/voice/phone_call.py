"""Run the approval conversation over an answered SIP call.

Speech out: text-to-speech into the RTP stream. Input: keypad digits (RFC 4733 DTMF)
for now; speech recognition plugs in later through the same ``Conversation``.
Pressing a key while the assistant is talking interrupts it (barge-in).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging

from agent_voice_bridge.approvals.service import ApprovalService, ServiceError
from agent_voice_bridge.models import ApprovalState, CallEvent, CallEventKind
from agent_voice_bridge.sip.server import SipCall
from agent_voice_bridge.voice.audio import silence
from agent_voice_bridge.voice.conversation.engine import Conversation, Turn
from agent_voice_bridge.voice.conversation.ops import InProcessOps
from agent_voice_bridge.voice.tts.base import TextToSpeech

log = logging.getLogger(__name__)


class PhoneCallHandler:
    def __init__(
        self,
        service: ApprovalService,
        tts: TextToSpeech,
        *,
        silence_timeout: float = 30.0,
        max_call_seconds: float = 900.0,
    ) -> None:
        self.service = service
        self.tts = tts
        self.silence_timeout = silence_timeout
        self.max_call_seconds = max_call_seconds

    async def __call__(self, call: SipCall) -> None:
        try:
            await asyncio.wait_for(self._run(call), self.max_call_seconds)
        except TimeoutError:
            log.warning("call %s exceeded %ss", call.call_id, self.max_call_seconds)

    async def _say(self, call: SipCall, lines: list[str]) -> str | None:
        """Speak; return a digit if the caller pressed one while we were talking."""
        for line in lines:
            if call.ended.is_set():
                return None
            pcm, _ = await self.tts.synthesize(line)
            call.rtp.play(pcm + silence(0.3))
        if not call.dtmf.empty():
            call.rtp.stop_playback()
            return call.dtmf.get_nowait()
        digit = asyncio.ensure_future(call.dtmf.get())
        played = asyncio.ensure_future(call.rtp.wait_played())
        ended = asyncio.ensure_future(call.ended.wait())
        done, pending = await asyncio.wait(
            {digit, played, ended}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if digit in done:
            call.rtp.stop_playback()
            return digit.result()
        return None

    async def _next_digit(self, call: SipCall) -> str | None:
        digit = asyncio.ensure_future(call.dtmf.get())
        ended = asyncio.ensure_future(call.ended.wait())
        done, pending = await asyncio.wait(
            {digit, ended}, timeout=self.silence_timeout, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        return digit.result() if digit in done else None

    async def _run(self, call: SipCall) -> None:
        call_id = "sip_" + hashlib.sha256(call.call_id.encode()).hexdigest()[:16]
        ops = InProcessOps(self.service)
        ids: list[str] = []
        for req in self.service.pending():
            if req.state != ApprovalState.PENDING_CALL:
                continue
            try:
                await ops.call_event(
                    req.id, CallEvent(event=CallEventKind.DIALING, call_id=call_id)
                )
                await ops.call_event(
                    req.id, CallEvent(event=CallEventKind.RINGING, call_id=call_id)
                )
                ids.append(req.id)
            except ServiceError as exc:
                log.info("not taking %s into this call: %s", req.id, exc.message)
        if not ids:
            await self._say(
                call,
                [
                    "Hello, this is Agent Voice Bridge. Nothing is waiting for a decision right now. Goodbye."
                ],
            )
            return

        convo = Conversation(ops, ids, call_id, keypad=True)
        finished = False
        try:
            turn: Turn = await convo.start()
            quiet = 0
            while not call.ended.is_set():
                digit = await self._say(call, turn.say)
                if turn.ended:
                    finished = True
                    break
                if digit is None:
                    digit = await self._next_digit(call)
                if call.ended.is_set():
                    break
                if digit is None:
                    quiet += 1
                    if quiet >= 2:
                        await self._say(
                            call,
                            [
                                "I haven't heard anything, so I'll hang up. "
                                "Nothing has been approved. Goodbye."
                            ],
                        )
                        break
                    turn = Turn(["Are you still there?", *convo.last_said])
                    continue
                quiet = 0
                reply = await convo.press(digit)
                turn = reply if reply is not None else Turn([])
        finally:
            if not finished:
                await convo.hangup()  # undecided requests go back to waiting
            with contextlib.suppress(Exception):
                if not call.ended.is_set():
                    await asyncio.wait_for(call.rtp.wait_played(), 10)
