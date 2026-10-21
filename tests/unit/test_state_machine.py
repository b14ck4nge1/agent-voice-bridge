import pytest

from agent_voice_bridge.approvals.state_machine import (
    TRANSITIONS,
    InvalidTransition,
    check_transition,
)
from agent_voice_bridge.models import TERMINAL_STATES
from agent_voice_bridge.models import ApprovalState as S


def test_every_state_has_a_rule() -> None:
    assert set(TRANSITIONS) == set(S)


def test_terminal_states_are_final() -> None:
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()


@pytest.mark.parametrize("state", [s for s in S if s not in TERMINAL_STATES])
def test_non_terminal_states_can_abort(state: S) -> None:
    for target in (S.CANCELLED, S.EXPIRED, S.FAILED):
        check_transition(state, target)


def test_decision_only_from_awaiting_decision() -> None:
    sources = {
        s for s, targets in TRANSITIONS.items() if S.APPROVED in targets or S.REJECTED in targets
    }
    assert sources == {S.AWAITING_DECISION}


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.EXPIRED, S.APPROVED),
        (S.REJECTED, S.APPROVED),
        (S.CREATED, S.APPROVED),
        (S.IN_CALL, S.APPROVED),
        (S.PENDING_CALL, S.AWAITING_DECISION),
        (S.APPROVED, S.REJECTED),
    ],
)
def test_invalid_transitions_raise(current: S, target: S) -> None:
    with pytest.raises(InvalidTransition):
        check_transition(current, target)
