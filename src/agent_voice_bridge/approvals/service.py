"""Approval business logic: creation, waiting, call progress, decisions and expiry.

All public methods are coroutines and must run on the bridge's event loop, because
they wake long-polling waiters through asyncio events.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from agent_voice_bridge.agents.normalize import describe_operation
from agent_voice_bridge.approvals.state_machine import InvalidTransition
from agent_voice_bridge.config import HighRiskPolicy, Settings
from agent_voice_bridge.database.store import Store, iso, utcnow
from agent_voice_bridge.models import (
    TERMINAL_STATES,
    ApprovalCreate,
    ApprovalState,
    ApprovalView,
    CallEvent,
    CallEventKind,
    DecisionKind,
    DecisionSubmit,
    RiskLevel,
    WaitResult,
)
from agent_voice_bridge.security import totp
from agent_voice_bridge.security.auth import hash_secret, secret_matches
from agent_voice_bridge.security.fingerprint import request_fingerprint
from agent_voice_bridge.security.redaction import redact, redact_text
from agent_voice_bridge.security.risk import classify
from agent_voice_bridge.sip.adapter.base import CallDispatcher

log = logging.getLogger(__name__)


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _project_name(workspace: str) -> str:
    flavour = PureWindowsPath if (":" in workspace[:3] or "\\" in workspace) else PurePosixPath
    return flavour(workspace).name or workspace


def row_to_view(row: dict[str, Any], *, second_factor_required: bool = False) -> ApprovalView:
    return ApprovalView(
        id=row["id"],
        agent=row["agent"],
        kind=row["kind"],
        session_id=row["session_id"],
        workspace=row["workspace"],
        project_name=row["project_name"],
        tool_name=row["tool_name"],
        tool_input=json.loads(row["tool_input_redacted"]),
        summary=row["summary"],
        reason=row["reason"],
        risk=row["risk"],
        risk_reasons=json.loads(row["risk_reasons"]),
        fingerprint=row["fingerprint"],
        state=row["state"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        call_attempts=row["call_attempts"],
        active_call_id=row["active_call_id"],
        decision=row["decision"],
        decision_message=row["decision_message"],
        second_factor_required=second_factor_required,
    )


class ApprovalService:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        dispatcher: CallDispatcher,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.settings = settings
        self.dispatcher = dispatcher
        self.clock = clock
        self._events: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # -- helpers --------------------------------------------------------------

    def _needs_second_factor(self, risk: str) -> bool:
        return (
            RiskLevel(risk).needs_second_factor
            and self.settings.high_risk_policy != HighRiskPolicy.VOICE
        )

    def view(self, row: dict[str, Any]) -> ApprovalView:
        return row_to_view(row, second_factor_required=self._needs_second_factor(row["risk"]))

    def _require(self, request_id: str) -> dict[str, Any]:
        row = self.store.get_request(request_id)
        if row is None:
            raise ServiceError(404, "not_found", "unknown approval request")
        return row

    def _notify(self, request_id: str) -> None:
        event = self._events.pop(request_id, None)
        if event is not None:
            event.set()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dispatch(self, request_id: str, delay: float = 0.0) -> None:
        if delay:
            await asyncio.sleep(delay)
        row = self.store.get_request(request_id)
        if row is None or row["state"] != ApprovalState.PENDING_CALL:
            return
        try:
            await self.dispatcher.request_call(self.view(row))
        except Exception:
            log.exception("call dispatcher failed for %s", request_id)
            await self._fail(request_id, "dispatcher_error")

    async def _fail(self, request_id: str, reason: str) -> None:
        try:
            self.store.transition(
                request_id,
                ApprovalState.FAILED,
                actor="bridge",
                event="failed",
                details={"reason": reason},
            )
        except InvalidTransition:
            return
        self._notify(request_id)

    def _is_expired(self, row: dict[str, Any]) -> bool:
        return self.clock() >= datetime.fromisoformat(row["expires_at"])

    async def _expire(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            row = self.store.transition(
                row["id"], ApprovalState.EXPIRED, actor="bridge", event="expired"
            )
        except InvalidTransition:
            return self._require(row["id"])
        self._notify(row["id"])
        self._spawn(self.dispatcher.cancel_call(row["id"], row["active_call_id"]))
        return row

    # -- agent side -------------------------------------------------------------

    async def create(self, payload: ApprovalCreate) -> tuple[ApprovalView, bool]:
        ttl = min(
            payload.ttl_seconds or self.settings.default_ttl_seconds, self.settings.max_ttl_seconds
        )
        fingerprint = request_fingerprint(
            agent=payload.agent.value,
            session_id=payload.session_id,
            workspace=payload.workspace,
            tool_name=payload.tool_name,
            tool_input=payload.tool_input,
            kind=payload.kind.value,
        )
        redacted_input = redact(payload.tool_input)
        risk = classify(payload.tool_name, payload.tool_input, payload.workspace)
        now = self.clock()
        row = {
            "id": "apr_" + secrets.token_urlsafe(12),
            "agent": payload.agent.value,
            "kind": payload.kind.value,
            "session_id": payload.session_id,
            "workspace": payload.workspace,
            "project_name": (payload.project_name or _project_name(payload.workspace))[:200],
            "tool_name": payload.tool_name,
            "tool_input_redacted": json.dumps(redacted_input, default=str),
            "summary": describe_operation(payload.tool_name, redacted_input),
            "reason": redact_text(payload.reason) if payload.reason else None,
            "turn_id": payload.turn_id,
            "risk": risk.level.value,
            "risk_reasons": json.dumps(risk.reasons),
            "fingerprint": fingerprint,
            "idempotency_key": payload.idempotency_key,
            "secret_hash": hash_secret(payload.request_secret),
            "state": ApprovalState.CREATED.value,
            "created_at": iso(now),
            "updated_at": iso(now),
            "expires_at": iso(now + timedelta(seconds=ttl)),
            "call_attempts": 0,
            "active_call_id": None,
            "decision": None,
            "decision_message": None,
        }
        stored, created = self.store.insert_request(row)
        if not created:
            if not secret_matches(
                payload.request_secret, stored["secret_hash"]
            ) or not hmac.compare_digest(stored["fingerprint"], fingerprint):
                raise ServiceError(
                    409, "idempotency_conflict", "idempotency key reused for a different request"
                )
            return self.view(stored), False
        stored = self.store.transition(
            stored["id"], ApprovalState.PENDING_CALL, actor="bridge", event="queued"
        )
        self._spawn(self._dispatch(stored["id"]))
        return self.view(stored), True

    def _require_owner(self, request_id: str, request_secret: str) -> dict[str, Any]:
        row = self._require(request_id)
        if not secret_matches(request_secret, row["secret_hash"]):
            # Same answer as unknown id, so ids cannot be probed with the agent token.
            raise ServiceError(404, "not_found", "unknown approval request")
        return row

    async def wait(self, request_id: str, request_secret: str, timeout: float) -> WaitResult:
        row = self._require_owner(request_id, request_secret)
        if row["state"] not in TERMINAL_STATES and self._is_expired(row):
            row = await self._expire(row)
        if row["state"] not in TERMINAL_STATES and timeout > 0:
            event = self._events.setdefault(request_id, asyncio.Event())
            # Re-read after registering so a decision landing in between is not missed.
            row = self._require(request_id)
            if row["state"] not in TERMINAL_STATES:
                try:
                    await asyncio.wait_for(event.wait(), timeout=timeout)
                except TimeoutError:
                    pass
                row = self._require(request_id)
        state = ApprovalState(row["state"])
        return WaitResult(
            id=row["id"],
            state=state,
            final=state in TERMINAL_STATES,
            decision=row["decision"],
            message=row["decision_message"],
        )

    async def cancel(
        self, request_id: str, request_secret: str, reason: str | None
    ) -> ApprovalView:
        row = self._require_owner(request_id, request_secret)
        if row["state"] in TERMINAL_STATES:
            return self.view(row)
        row = self.store.transition(
            request_id,
            ApprovalState.CANCELLED,
            actor="agent",
            event="cancelled",
            details={"reason": reason},
        )
        self._notify(request_id)
        self._spawn(self.dispatcher.cancel_call(request_id, row["active_call_id"]))
        return self.view(row)

    # -- voice / call side --------------------------------------------------------

    def pending(self) -> list[ApprovalView]:
        active = [s for s in ApprovalState if s not in TERMINAL_STATES]
        return [self.view(r) for r in self.store.list_requests(active) if not self._is_expired(r)]

    def get(self, request_id: str) -> ApprovalView:
        return self.view(self._require(request_id))

    async def call_event(self, request_id: str, ev: CallEvent) -> ApprovalView:
        row = self._require(request_id)
        if row["state"] in TERMINAL_STATES:
            raise ServiceError(409, "final", f"request is already {row['state']}")
        if self._is_expired(row):
            await self._expire(row)
            raise ServiceError(410, "expired", "request has expired")
        state = ApprovalState(row["state"])
        details = {"call_id": ev.call_id, "detail": ev.detail}

        if ev.event == CallEventKind.DIALING:
            if state != ApprovalState.PENDING_CALL:
                raise ServiceError(409, "bad_state", f"cannot dial while {state}")
            row = self.store.transition(
                request_id,
                ApprovalState.PENDING_CALL,
                actor="voice",
                event="dialing",
                details=details,
                updates={"active_call_id": ev.call_id, "call_attempts": row["call_attempts"] + 1},
            )
            return self.view(row)

        if row["active_call_id"] is None or not hmac.compare_digest(
            row["active_call_id"], ev.call_id
        ):
            raise ServiceError(409, "call_mismatch", "event is not for the active call")

        target = {
            CallEventKind.RINGING: ApprovalState.RINGING,
            CallEventKind.ANSWERED: ApprovalState.IN_CALL,
            CallEventKind.PRESENTED: ApprovalState.AWAITING_DECISION,
        }.get(ev.event)
        try:
            if target is not None:
                row = self.store.transition(
                    request_id, target, actor="voice", event=ev.event.value, details=details
                )
                return self.view(row)
            if ev.event == CallEventKind.FAILED:
                await self._fail(request_id, f"call_failed: {ev.detail or ''}")
                return self.get(request_id)
            # NO_ANSWER / HANGUP: retry while attempts remain, otherwise give up (deny).
            if row["call_attempts"] >= self.settings.max_call_attempts:
                row = self.store.transition(
                    request_id,
                    ApprovalState.FAILED,
                    actor="voice",
                    event=ev.event.value,
                    details={**details, "reason": "call attempts exhausted"},
                    updates={"active_call_id": None},
                )
                self._notify(request_id)
                return self.view(row)
            row = self.store.transition(
                request_id,
                ApprovalState.PENDING_CALL,
                actor="voice",
                event=ev.event.value,
                details=details,
                updates={"active_call_id": None},
            )
            self._spawn(self._dispatch(request_id, delay=self.settings.redial_delay_seconds))
            return self.view(row)
        except InvalidTransition as exc:
            raise ServiceError(409, "bad_state", str(exc)) from exc

    async def decide(self, request_id: str, d: DecisionSubmit) -> ApprovalView:
        prior = self.store.get_decision(d.nonce)
        if prior is not None:
            if prior["request_id"] == request_id and prior["decision"] == d.decision.value:
                return self.get(request_id)  # idempotent retry of the same submission
            raise ServiceError(409, "replay", "decision nonce already used")

        row = self._require(request_id)
        state = ApprovalState(row["state"])
        if state in TERMINAL_STATES:
            raise ServiceError(409, "final", f"request is already {state}")
        if self._is_expired(row):
            await self._expire(row)
            raise ServiceError(410, "expired", "request has expired")
        if state != ApprovalState.AWAITING_DECISION:
            raise ServiceError(409, "bad_state", f"request is {state}, not AWAITING_DECISION")
        if row["active_call_id"] is None or not hmac.compare_digest(
            row["active_call_id"], d.call_id
        ):
            raise ServiceError(409, "call_mismatch", "decision is not from the active call")
        if not hmac.compare_digest(row["fingerprint"], d.fingerprint):
            self.store.audit(request_id, "voice", "fingerprint_mismatch", {"call_id": d.call_id})
            raise ServiceError(
                409, "fingerprint_mismatch", "decision does not match the presented request"
            )
        if not d.confirmed:
            raise ServiceError(422, "not_confirmed", "decision must be explicitly confirmed")

        counter: int | None = None
        if d.decision == DecisionKind.APPROVE and self._needs_second_factor(row["risk"]):
            counter = self._check_second_factor(request_id, d)

        target = (
            ApprovalState.APPROVED if d.decision == DecisionKind.APPROVE else ApprovalState.REJECTED
        )
        message = redact_text(d.message) if d.message else None
        try:
            row = self.store.record_decision(
                request_id=request_id,
                nonce=d.nonce,
                decision=d.decision.value,
                call_id=d.call_id,
                message=message,
                target=target,
                totp_counter=counter,
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(409, "replay", "nonce or second-factor code already used") from exc
        except InvalidTransition as exc:
            raise ServiceError(409, "bad_state", str(exc)) from exc
        self._notify(request_id)
        return self.view(row)

    def _check_second_factor(self, request_id: str, d: DecisionSubmit) -> int:
        policy = self.settings.high_risk_policy
        if policy == HighRiskPolicy.DENY:
            raise ServiceError(
                403, "approval_forbidden", "high-risk requests cannot be approved by phone"
            )
        if not self.settings.totp_secret:
            raise ServiceError(
                403, "second_factor_unavailable", "high-risk approval needs AVB_TOTP_SECRET"
            )
        if not d.totp_code:
            raise ServiceError(403, "second_factor_required", "a one-time code is required")
        counter = totp.verify(self.settings.totp_secret, d.totp_code, now=self.clock().timestamp())
        if counter is None or self.store.totp_counter_used(counter):
            self.store.audit(request_id, "voice", "second_factor_failed", {"call_id": d.call_id})
            raise ServiceError(
                403, "second_factor_invalid", "the one-time code is invalid or already used"
            )
        return counter

    # -- maintenance --------------------------------------------------------------

    async def sweep_expired(self) -> int:
        active = [s for s in ApprovalState if s not in TERMINAL_STATES]
        count = 0
        for row in self.store.list_requests(active):
            if self._is_expired(row):
                await self._expire(row)
                count += 1
        return count

    async def recover_after_restart(self) -> None:
        """A restart loses any call in progress; those requests go back to dialling."""
        for state in (
            ApprovalState.RINGING,
            ApprovalState.IN_CALL,
            ApprovalState.AWAITING_DECISION,
        ):
            for row in self.store.list_requests([state]):
                try:
                    self.store.transition(
                        row["id"],
                        ApprovalState.PENDING_CALL,
                        actor="bridge",
                        event="recovered",
                        updates={"active_call_id": None},
                    )
                except InvalidTransition:
                    continue
        for row in self.store.list_requests([ApprovalState.CREATED]):
            self.store.transition(
                row["id"], ApprovalState.PENDING_CALL, actor="bridge", event="recovered"
            )
        await self.sweep_expired()
        for row in self.store.list_requests([ApprovalState.PENDING_CALL]):
            self._spawn(self._dispatch(row["id"]))

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for event in self._events.values():
            event.set()
