"""Explicit lifecycle transitions for an approval request.

Every state change in the bridge goes through :func:`check_transition`. Anything not
listed here is rejected, so a bug elsewhere cannot, for example, move an EXPIRED
request to APPROVED.
"""

from __future__ import annotations

from agent_voice_bridge.models import ApprovalState as S

# Any non-terminal state may also go to CANCELLED, EXPIRED or FAILED.
_ABORT = frozenset({S.CANCELLED, S.EXPIRED, S.FAILED})

TRANSITIONS: dict[S, frozenset[S]] = {
    S.CREATED: frozenset({S.PENDING_CALL}) | _ABORT,
    S.PENDING_CALL: frozenset({S.RINGING, S.IN_CALL}) | _ABORT,
    # A missed call goes back to PENDING_CALL so it can be retried.
    S.RINGING: frozenset({S.IN_CALL, S.PENDING_CALL}) | _ABORT,
    # A call that drops before the request was presented is retried.
    S.IN_CALL: frozenset({S.AWAITING_DECISION, S.PENDING_CALL}) | _ABORT,
    # Decisions are only accepted once the assistant has presented the request.
    S.AWAITING_DECISION: frozenset({S.APPROVED, S.REJECTED, S.PENDING_CALL}) | _ABORT,
    S.APPROVED: frozenset(),
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
    S.CANCELLED: frozenset(),
    S.FAILED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, current: S, target: S) -> None:
        super().__init__(f"invalid transition {current} -> {target}")
        self.current = current
        self.target = target


def can_transition(current: S, target: S) -> bool:
    return target in TRANSITIONS[current]


def check_transition(current: S, target: S) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
