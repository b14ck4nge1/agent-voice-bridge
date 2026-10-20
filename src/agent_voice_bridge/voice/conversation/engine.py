"""Dialog manager for an approval phone call.

The engine is text in / text out; speech recognition and synthesis wrap around it.
It can do exactly three things to the bridge (see :class:`BridgeOps`): read a
request, report call progress, and submit a decision for a request the bridge
created. It has no shell, file or network access of its own, and it cannot invent
requests or change their arguments.

Decision rules:

* Only an explicit approve/reject from the user starts a decision, never content
  read out of the request itself.
* Every decision needs a spoken confirmation of that exact request.
* Approving a high-risk request also needs a valid one-time code.
* Each decision carries the request's fingerprint and the call id, so it cannot
  land on a different or modified request.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from agent_voice_bridge.approvals.service import ServiceError
from agent_voice_bridge.models import (
    AgentKind,
    ApprovalState,
    ApprovalView,
    CallEvent,
    CallEventKind,
    DecisionKind,
    DecisionSubmit,
)
from agent_voice_bridge.voice.conversation.intents import Intent, extract_code, parse

AGENT_NAMES = {
    AgentKind.CLAUDE_CODE: "Claude Code",
    AgentKind.CODEX: "Codex",
    AgentKind.TEST: "A test agent",
}
MAX_CODE_ATTEMPTS = 2


class BridgeOps(Protocol):
    async def get(self, request_id: str) -> ApprovalView: ...

    async def call_event(self, request_id: str, event: CallEvent) -> ApprovalView: ...

    async def decide(self, request_id: str, decision: DecisionSubmit) -> ApprovalView: ...


class Phase(StrEnum):
    NOT_STARTED = "not_started"
    DISCUSSING = "discussing"
    CONFIRMING = "confirming"
    AWAITING_CODE = "awaiting_code"
    DONE = "done"


@dataclass
class Turn:
    say: list[str]
    ended: bool = False


@dataclass
class _Pending:
    decision: DecisionKind
    message: str | None = None
    code_attempts: int = 0


@dataclass
class Conversation:
    ops: BridgeOps
    request_ids: list[str]
    call_id: str
    nonce_factory: Callable[[], str] = field(default=lambda: secrets.token_urlsafe(24))
    phase: Phase = Phase.NOT_STARTED
    outcomes: dict[str, str] = field(default_factory=dict)
    _index: int = 0
    _current: ApprovalView | None = None
    _pending: _Pending | None = None
    _last_said: list[str] = field(default_factory=list)

    # -- public ---------------------------------------------------------------

    async def start(self) -> Turn:
        """Call once the user has answered. Reports ANSWERED and presents the first request."""
        for rid in self.request_ids:
            await self._event(rid, CallEventKind.ANSWERED)
        opener = "Hello, this is Agent Voice Bridge."
        if len(self.request_ids) > 1:
            opener += f" There are {len(self.request_ids)} requests waiting for you."
        turn = await self._present_next()
        turn.say.insert(0, opener)
        return self._remember(turn)

    async def hear(self, text: str) -> Turn:
        if self.phase in (Phase.DONE, Phase.NOT_STARTED) or self._current is None:
            return Turn([], ended=self.phase == Phase.DONE)
        if not await self._still_open():
            return self._remember(
                await self._advance(["That request is no longer pending, so nothing was decided."])
            )
        if self.phase == Phase.AWAITING_CODE:
            return self._remember(await self._on_code(text))
        if self.phase == Phase.CONFIRMING:
            return self._remember(await self._on_confirm(text))
        return self._remember(await self._on_discuss(text))

    async def hangup(self) -> None:
        """The call ended. Undecided requests go back to the bridge for a retry."""
        if self.phase == Phase.DONE:
            return
        self.phase = Phase.DONE
        for rid in self.request_ids:
            if rid not in self.outcomes:
                try:
                    await self.ops.call_event(
                        rid, CallEvent(event=CallEventKind.HANGUP, call_id=self.call_id)
                    )
                except ServiceError:
                    pass

    # -- phases -------------------------------------------------------------------

    async def _on_discuss(self, text: str) -> Turn:
        assert self._current is not None
        req = self._current
        parsed = parse(text)
        if parsed.intent == Intent.APPROVE:
            self._pending = _Pending(DecisionKind.APPROVE)
            self.phase = Phase.CONFIRMING
            return Turn(
                [
                    f"Please confirm: approve {self._agent(req)}'s request to {req.summary}? Say yes or no."
                ]
            )
        if parsed.intent == Intent.REJECT:
            self._pending = _Pending(DecisionKind.REJECT, parsed.message)
            self.phase = Phase.CONFIRMING
            extra = f" and tell it: {parsed.message}" if parsed.message else ""
            return Turn([f"Please confirm: reject this request{extra}? Say yes or no."])
        if parsed.intent == Intent.DETAILS:
            return Turn([self._details(req)])
        if parsed.intent == Intent.RISK:
            return Turn([self._risk(req)])
        if parsed.intent == Intent.CONTEXT:
            return Turn([self._context(req)])
        if parsed.intent == Intent.REPEAT:
            return Turn(list(self._last_said) or [self._presentation(req)])
        if parsed.intent == Intent.SKIP:
            return await self._advance(["Okay, I'll leave that one undecided."], skipped=True)
        return Turn(
            [
                "You can ask what exactly it wants to do, what the risks are, or which project this is. "
                "Or say approve, or reject. To ask for changes, say reject, followed by what it should do instead."
            ]
        )

    async def _on_confirm(self, text: str) -> Turn:
        assert self._current is not None and self._pending is not None
        parsed = parse(text, confirming=True)
        if parsed.intent == Intent.YES:
            if (
                self._pending.decision == DecisionKind.APPROVE
                and self._current.second_factor_required
            ):
                self.phase = Phase.AWAITING_CODE
                return Turn(
                    [
                        "This is a high-risk request, so I need a second factor. "
                        "Please read the six-digit code from your authenticator app."
                    ]
                )
            return await self._submit(None)
        if parsed.intent == Intent.NO:
            self._pending = None
            self.phase = Phase.DISCUSSING
            return Turn(
                [
                    "Okay, nothing has been decided. Do you approve or reject, or do you have a question?"
                ]
            )
        return Turn(["Sorry, I need a clear yes or no."])

    async def _on_code(self, text: str) -> Turn:
        assert self._pending is not None
        code = extract_code(text)
        if code is None:
            return Turn(
                ["I didn't catch six digits. Please read the code again, one digit at a time."]
            )
        return await self._submit(code)

    async def _submit(self, code: str | None) -> Turn:
        assert self._current is not None and self._pending is not None
        req, pending = self._current, self._pending
        submission = DecisionSubmit(
            decision=pending.decision,
            fingerprint=req.fingerprint,
            call_id=self.call_id,
            nonce=self.nonce_factory(),
            confirmed=True,
            message=pending.message,
            totp_code=code,
        )
        try:
            result = await self.ops.decide(req.id, submission)
        except ServiceError as exc:
            if exc.code in {"second_factor_invalid", "second_factor_required"}:
                pending.code_attempts += 1
                if pending.code_attempts < MAX_CODE_ATTEMPTS:
                    return Turn(
                        ["That code was not accepted. Please wait for the next code and read it."]
                    )
                self._pending = None
                self.phase = Phase.DISCUSSING
                return Turn(
                    [
                        "That code was not accepted either, so I have not approved it. "
                        "You can reject it, or let it time out."
                    ]
                )
            if exc.code in {"approval_forbidden", "second_factor_unavailable"}:
                self._pending = None
                self.phase = Phase.DISCUSSING
                return Turn(
                    [
                        "This request is high risk and your settings don't allow approving it by phone. "
                        "You can reject it, or approve it at your computer."
                    ]
                )
            return await self._advance(
                [f"I couldn't record that: {exc.message}. Nothing was approved."]
            )
        self.outcomes[req.id] = result.state.value
        done = "Approved." if result.state == ApprovalState.APPROVED else "Rejected."
        tail = (
            f" {self._agent(req)} will continue." if result.state == ApprovalState.APPROVED else ""
        )
        return await self._advance([done + tail])

    # -- helpers --------------------------------------------------------------------

    async def _advance(self, say: list[str], *, skipped: bool = False) -> Turn:
        self._pending = None
        if self._current is not None and skipped:
            self.outcomes.setdefault(self._current.id, "SKIPPED")
            try:
                await self.ops.call_event(
                    self._current.id,
                    CallEvent(event=CallEventKind.HANGUP, call_id=self.call_id, detail="skipped"),
                )
            except ServiceError:
                pass
        elif self._current is not None:
            self.outcomes.setdefault(self._current.id, "CLOSED")
        nxt = await self._present_next()
        return Turn(say + nxt.say, ended=nxt.ended)

    async def _present_next(self) -> Turn:
        while self._index < len(self.request_ids):
            rid = self.request_ids[self._index]
            self._index += 1
            try:
                req = await self.ops.get(rid)
            except ServiceError:
                continue
            if req.state != ApprovalState.IN_CALL:
                continue
            try:
                await self.ops.call_event(
                    rid, CallEvent(event=CallEventKind.PRESENTED, call_id=self.call_id)
                )
                req = await self.ops.get(rid)
            except ServiceError:
                continue
            self._current = req
            self.phase = Phase.DISCUSSING
            return Turn([self._presentation(req)])
        self._current = None
        self.phase = Phase.DONE
        return Turn(["That's everything. Goodbye."], ended=True)

    async def _still_open(self) -> bool:
        assert self._current is not None
        try:
            fresh = await self.ops.get(self._current.id)
        except ServiceError:
            return False
        if fresh.fingerprint != self._current.fingerprint:
            return False
        return fresh.state == ApprovalState.AWAITING_DECISION

    async def _event(self, rid: str, kind: CallEventKind) -> None:
        try:
            await self.ops.call_event(rid, CallEvent(event=kind, call_id=self.call_id))
        except ServiceError:
            pass

    def _remember(self, turn: Turn) -> Turn:
        if turn.say:
            self._last_said = list(turn.say)
        return turn

    @staticmethod
    def _agent(req: ApprovalView) -> str:
        return AGENT_NAMES.get(req.agent, str(req.agent))

    def _presentation(self, req: ApprovalView) -> str:
        parts = [
            f"{self._agent(req)} is working on the project {req.project_name} and needs your permission.",
            f"It wants to {req.summary}.",
            f"I rate this {req.risk.value} risk.",
        ]
        if req.reason:
            parts.append(f"The agent's own explanation, which I can't verify, is: {req.reason}.")
        if req.second_factor_required:
            parts.append("Approving it will need a code from your authenticator app.")
        parts.append("Do you approve or reject? You can also ask me about it.")
        return " ".join(parts)

    def _details(self, req: ApprovalView) -> str:
        text = f"Exactly: it wants to {req.summary}."
        if req.workspace:
            text += f" The workspace is {req.workspace}."
        text += f" The tool is {req.tool_name}."
        return text

    def _risk(self, req: ApprovalView) -> str:
        reasons = ", ".join(req.risk_reasons) if req.risk_reasons else "no specific concerns"
        return (
            f"I rated it {req.risk.value} risk because it: {reasons}. That rating comes from simple rules. "
            "I can't run or inspect the operation, so I can't guarantee what it will actually do; "
            "for example, package installs can run their own scripts."
        )

    def _context(self, req: ApprovalView) -> str:
        return (
            f"This is {self._agent(req)}, in the project {req.project_name} at {req.workspace}. "
            f"Its session id ends in {req.session_id[-6:]}."
            + (f" The agent says: {req.reason}." if req.reason else "")
        )
