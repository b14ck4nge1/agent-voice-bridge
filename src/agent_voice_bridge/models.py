"""Shared data models for the approval bridge API."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentKind(StrEnum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    TEST = "test"


class RequestKind(StrEnum):
    TOOL_PERMISSION = "tool_permission"  # Claude Code / Codex PermissionRequest hook
    COMMAND_EXECUTION = "command_execution"  # Codex app-server commandExecution approval
    FILE_CHANGE = "file_change"  # Codex app-server fileChange approval
    QUESTION = "question"  # clarification; answered with free text


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def needs_second_factor(self) -> bool:
        return self in (RiskLevel.HIGH, RiskLevel.CRITICAL)


class ApprovalState(StrEnum):
    CREATED = "CREATED"
    PENDING_CALL = "PENDING_CALL"
    RINGING = "RINGING"
    IN_CALL = "IN_CALL"
    AWAITING_DECISION = "AWAITING_DECISION"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATES


TERMINAL_STATES = frozenset(
    {
        ApprovalState.APPROVED,
        ApprovalState.REJECTED,
        ApprovalState.EXPIRED,
        ApprovalState.CANCELLED,
        ApprovalState.FAILED,
    }
)


class DecisionKind(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class CallEventKind(StrEnum):
    DIALING = "dialing"
    RINGING = "ringing"
    ANSWERED = "answered"
    PRESENTED = "presented"  # assistant has read the request; decision may now be taken
    NO_ANSWER = "no_answer"  # missed / declined / busy
    HANGUP = "hangup"  # call ended without a decision
    FAILED = "failed"  # transport failure


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApprovalCreate(_Strict):
    agent: AgentKind
    kind: RequestKind = RequestKind.TOOL_PERMISSION
    session_id: str = Field(min_length=1, max_length=200)
    workspace: str = Field(min_length=1, max_length=1000, description="Absolute project path (cwd)")
    project_name: str | None = Field(default=None, max_length=200)
    tool_name: str = Field(min_length=1, max_length=200)
    tool_input: dict[str, Any] = Field(default_factory=dict)
    # Free text from the agent (e.g. Codex's approval reason). It is untrusted: the
    # assistant reads it as "the agent says ...", never as the bridge's own summary.
    reason: str | None = Field(default=None, max_length=2000)
    turn_id: str | None = Field(default=None, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=200)
    request_secret: str = Field(
        min_length=32,
        max_length=200,
        description="Client-generated secret; required later to wait on or cancel this request",
    )
    ttl_seconds: int | None = Field(default=None, ge=1)


class ApprovalView(BaseModel):
    """What any authenticated party may see. Contains only redacted input."""

    id: str
    agent: AgentKind
    kind: RequestKind
    session_id: str
    workspace: str
    project_name: str
    tool_name: str
    tool_input: dict[str, Any]
    summary: str
    reason: str | None
    risk: RiskLevel
    risk_reasons: list[str]
    fingerprint: str
    state: ApprovalState
    created_at: datetime
    expires_at: datetime
    updated_at: datetime
    call_attempts: int
    active_call_id: str | None
    decision: DecisionKind | None = None
    decision_message: str | None = None
    second_factor_required: bool = False


class CallEvent(_Strict):
    event: CallEventKind
    call_id: str = Field(min_length=1, max_length=200)
    detail: str | None = Field(default=None, max_length=500)


class DecisionSubmit(_Strict):
    decision: DecisionKind
    fingerprint: str = Field(min_length=64, max_length=64)
    call_id: str = Field(min_length=1, max_length=200)
    nonce: str = Field(min_length=16, max_length=200, description="Unique per submission")
    confirmed: bool = Field(description="User explicitly confirmed this exact decision")
    message: str | None = Field(default=None, max_length=2000, description="Feedback to the agent")
    totp_code: str | None = Field(default=None, pattern=r"^\d{6}$")


class WaitResult(BaseModel):
    id: str
    state: ApprovalState
    final: bool
    decision: DecisionKind | None = None
    message: str | None = None
