from __future__ import annotations

import secrets
from dataclasses import replace

import pytest

from agent_voice_bridge.approvals.service import ApprovalService, ServiceError
from agent_voice_bridge.config import HighRiskPolicy, Settings
from agent_voice_bridge.database.store import Store
from agent_voice_bridge.models import (
    ApprovalState as S,
)
from agent_voice_bridge.models import (
    CallEvent,
    DecisionKind,
    DecisionSubmit,
)
from agent_voice_bridge.models import (
    CallEventKind as E,
)
from agent_voice_bridge.security import totp
from tests.conftest import TOTP_SECRET, Clock, RecordingDispatcher, make_create, settle


async def _to_awaiting(service: ApprovalService, rid: str, call_id: str = "call-1") -> None:
    for ev in (E.DIALING, E.RINGING, E.ANSWERED, E.PRESENTED):
        await service.call_event(rid, CallEvent(event=ev, call_id=call_id))


def _decision(
    view_fp: str, decision: DecisionKind = DecisionKind.APPROVE, **kw: object
) -> DecisionSubmit:
    data: dict[str, object] = {
        "decision": decision,
        "fingerprint": view_fp,
        "call_id": "call-1",
        "nonce": secrets.token_hex(16),
        "confirmed": True,
    }
    data.update(kw)
    return DecisionSubmit.model_validate(data)


async def test_create_queues_a_call_and_redacts(
    service: ApprovalService, dispatcher: RecordingDispatcher
) -> None:
    view, created = await service.create(
        make_create(tool_input={"command": "TOKEN=abcdefghijk npm publish"})
    )
    await settle()
    assert created and view.state == S.PENDING_CALL
    assert "abcdefghijk" not in view.tool_input["command"]
    assert "abcdefghijk" not in view.summary
    assert view.project_name == "frontend"
    assert [r.id for r in dispatcher.requested] == [view.id]
    row = service.store.get_request(view.id)
    assert row is not None and "abcdefghijk" not in str(row)


async def test_idempotent_create(service: ApprovalService) -> None:
    payload = make_create()
    first, created1 = await service.create(payload)
    second, created2 = await service.create(payload)
    assert created1 and not created2 and first.id == second.id


async def test_idempotency_key_reuse_for_other_request_conflicts(service: ApprovalService) -> None:
    payload = make_create()
    await service.create(payload)
    other = payload.model_copy(update={"tool_input": {"command": "rm -rf /"}})
    with pytest.raises(ServiceError) as exc:
        await service.create(other)
    assert exc.value.code == "idempotency_conflict"


async def test_full_approval_flow_wakes_waiter(service: ApprovalService) -> None:
    payload = make_create()
    view, _ = await service.create(payload)
    await _to_awaiting(service, view.id)
    import asyncio

    waiter = asyncio.create_task(service.wait(view.id, payload.request_secret, timeout=5))
    await settle()
    result = await service.decide(view.id, _decision(view.fingerprint))
    assert result.state == S.APPROVED
    waited = await waiter
    assert waited.final and waited.state == S.APPROVED and waited.decision == DecisionKind.APPROVE


async def test_reject_with_feedback(service: ApprovalService) -> None:
    payload = make_create()
    view, _ = await service.create(payload)
    await _to_awaiting(service, view.id)
    await service.decide(
        view.id, _decision(view.fingerprint, DecisionKind.REJECT, message="use pnpm")
    )
    waited = await service.wait(view.id, payload.request_secret, timeout=0)
    assert waited.state == S.REJECTED and waited.message == "use pnpm"


async def test_wait_requires_request_secret(service: ApprovalService) -> None:
    view, _ = await service.create(make_create())
    with pytest.raises(ServiceError) as exc:
        await service.wait(view.id, secrets.token_urlsafe(32), timeout=0)
    assert exc.value.status == 404


async def test_decision_before_presentation_is_refused(service: ApprovalService) -> None:
    view, _ = await service.create(make_create())
    await service.call_event(view.id, CallEvent(event=E.DIALING, call_id="call-1"))
    with pytest.raises(ServiceError) as exc:
        await service.decide(view.id, _decision(view.fingerprint))
    assert exc.value.code == "bad_state"


async def test_fingerprint_mismatch_is_refused(service: ApprovalService) -> None:
    view, _ = await service.create(make_create())
    await _to_awaiting(service, view.id)
    with pytest.raises(ServiceError) as exc:
        await service.decide(view.id, _decision("0" * 64))
    assert exc.value.code == "fingerprint_mismatch"
    assert service.get(view.id).state == S.AWAITING_DECISION


async def test_decision_from_other_call_is_refused(service: ApprovalService) -> None:
    view, _ = await service.create(make_create())
    await _to_awaiting(service, view.id)
    with pytest.raises(ServiceError) as exc:
        await service.decide(view.id, _decision(view.fingerprint, call_id="call-2"))
    assert exc.value.code == "call_mismatch"


async def test_unconfirmed_decision_is_refused(service: ApprovalService) -> None:
    view, _ = await service.create(make_create())
    await _to_awaiting(service, view.id)
    with pytest.raises(ServiceError) as exc:
        await service.decide(view.id, _decision(view.fingerprint, confirmed=False))
    assert exc.value.code == "not_confirmed"


async def test_replay_and_duplicate_decisions(service: ApprovalService) -> None:
    a, _ = await service.create(make_create(session_id="A"))
    b, _ = await service.create(make_create(session_id="B"))
    await _to_awaiting(service, a.id)
    await _to_awaiting(service, b.id, call_id="call-1")
    d = _decision(a.fingerprint)
    assert (await service.decide(a.id, d)).state == S.APPROVED
    # Exact retry is idempotent.
    assert (await service.decide(a.id, d)).state == S.APPROVED
    # Same nonce replayed onto another request is refused.
    with pytest.raises(ServiceError) as exc:
        await service.decide(b.id, d.model_copy(update={"fingerprint": b.fingerprint}))
    assert exc.value.code == "replay"
    # A second, different decision on a decided request is refused.
    with pytest.raises(ServiceError) as exc:
        await service.decide(a.id, _decision(a.fingerprint, DecisionKind.REJECT))
    assert exc.value.code == "final"


async def test_expiry(
    service: ApprovalService, clock: Clock, dispatcher: RecordingDispatcher
) -> None:
    payload = make_create(ttl_seconds=60)
    view, _ = await service.create(payload)
    await _to_awaiting(service, view.id)
    clock.advance(61)
    with pytest.raises(ServiceError) as exc:
        await service.decide(view.id, _decision(view.fingerprint))
    assert exc.value.status == 410
    await settle()
    assert (await service.wait(view.id, payload.request_secret, timeout=0)).state == S.EXPIRED
    assert view.id in dispatcher.cancelled


async def test_sweeper_expires_untouched_requests(service: ApprovalService, clock: Clock) -> None:
    view, _ = await service.create(make_create(ttl_seconds=10))
    clock.advance(11)
    assert await service.sweep_expired() == 1
    assert service.get(view.id).state == S.EXPIRED


async def test_ttl_is_capped(service: ApprovalService, settings: Settings) -> None:
    view, _ = await service.create(make_create(ttl_seconds=10_000_000))
    assert (view.expires_at - view.created_at).total_seconds() == settings.max_ttl_seconds


async def test_cancel(service: ApprovalService, dispatcher: RecordingDispatcher) -> None:
    payload = make_create()
    view, _ = await service.create(payload)
    await _to_awaiting(service, view.id)
    cancelled = await service.cancel(view.id, payload.request_secret, "agent moved on")
    assert cancelled.state == S.CANCELLED
    await settle()
    assert view.id in dispatcher.cancelled
    with pytest.raises(ServiceError):
        await service.decide(view.id, _decision(view.fingerprint))


async def test_missed_calls_retry_then_fail(
    service: ApprovalService, dispatcher: RecordingDispatcher
) -> None:
    payload = make_create()
    view, _ = await service.create(payload)
    await settle()
    for attempt in range(2):
        await service.call_event(view.id, CallEvent(event=E.DIALING, call_id=f"c{attempt}"))
        await service.call_event(view.id, CallEvent(event=E.RINGING, call_id=f"c{attempt}"))
        await service.call_event(view.id, CallEvent(event=E.NO_ANSWER, call_id=f"c{attempt}"))
        await settle()
    assert service.get(view.id).state == S.FAILED
    assert len(dispatcher.requested) == 2  # first dial + one redial
    assert (await service.wait(view.id, payload.request_secret, timeout=0)).state == S.FAILED


async def test_call_events_for_stale_call_are_refused(service: ApprovalService) -> None:
    view, _ = await service.create(make_create())
    await service.call_event(view.id, CallEvent(event=E.DIALING, call_id="new"))
    with pytest.raises(ServiceError) as exc:
        await service.call_event(view.id, CallEvent(event=E.ANSWERED, call_id="old"))
    assert exc.value.code == "call_mismatch"


async def test_hangup_after_presentation_goes_back_to_dialling(service: ApprovalService) -> None:
    view, _ = await service.create(make_create())
    await _to_awaiting(service, view.id)
    after = await service.call_event(view.id, CallEvent(event=E.HANGUP, call_id="call-1"))
    assert after.state == S.PENDING_CALL and after.active_call_id is None


async def test_concurrent_sessions_are_isolated(service: ApprovalService) -> None:
    p1, p2 = make_create(session_id="claude-1"), make_create(session_id="claude-2")
    v1, _ = await service.create(p1)
    v2, _ = await service.create(p2)
    assert v1.fingerprint != v2.fingerprint
    await _to_awaiting(service, v1.id, "c1")
    await _to_awaiting(service, v2.id, "c2")
    with pytest.raises(ServiceError):  # v1's decision cannot land on v2
        await service.decide(v2.id, _decision(v1.fingerprint, call_id="c2"))
    await service.decide(v1.id, _decision(v1.fingerprint, call_id="c1"))
    await service.decide(v2.id, _decision(v2.fingerprint, DecisionKind.REJECT, call_id="c2"))
    assert (await service.wait(v1.id, p1.request_secret, 0)).state == S.APPROVED
    assert (await service.wait(v2.id, p2.request_secret, 0)).state == S.REJECTED
    with pytest.raises(ServiceError):  # one session cannot read another's result
        await service.wait(v1.id, p2.request_secret, 0)


# -- high risk ------------------------------------------------------------------


async def _high_risk(service: ApprovalService) -> str:
    view, _ = await service.create(
        make_create(tool_input={"command": "git push --force origin main"})
    )
    assert view.second_factor_required
    await _to_awaiting(service, view.id)
    return view.id


async def test_high_risk_needs_totp(service: ApprovalService, clock: Clock) -> None:
    rid = await _high_risk(service)
    fp = service.get(rid).fingerprint
    with pytest.raises(ServiceError) as exc:
        await service.decide(rid, _decision(fp))
    assert exc.value.code == "second_factor_required"
    with pytest.raises(ServiceError) as exc:
        await service.decide(rid, _decision(fp, totp_code="000000"))
    assert exc.value.code == "second_factor_invalid"
    code = totp.code_at(TOTP_SECRET, totp.current_counter(clock().timestamp()))
    assert (await service.decide(rid, _decision(fp, totp_code=code))).state == S.APPROVED


async def test_totp_code_cannot_be_reused(service: ApprovalService, clock: Clock) -> None:
    code = totp.code_at(TOTP_SECRET, totp.current_counter(clock().timestamp()))
    r1 = await _high_risk(service)
    await service.decide(r1, _decision(service.get(r1).fingerprint, totp_code=code))
    view, _ = await service.create(
        make_create(session_id="s2", tool_input={"command": "sudo rm -r /opt/x"})
    )
    await _to_awaiting(service, view.id)
    with pytest.raises(ServiceError) as exc:
        await service.decide(view.id, _decision(view.fingerprint, totp_code=code))
    assert exc.value.code == "second_factor_invalid"


async def test_high_risk_can_always_be_rejected_without_code(service: ApprovalService) -> None:
    rid = await _high_risk(service)
    result = await service.decide(rid, _decision(service.get(rid).fingerprint, DecisionKind.REJECT))
    assert result.state == S.REJECTED


async def test_deny_policy_and_missing_secret(
    settings: Settings, dispatcher: RecordingDispatcher, clock: Clock
) -> None:
    for s, code in (
        (replace(settings, high_risk_policy=HighRiskPolicy.DENY), "approval_forbidden"),
        (replace(settings, totp_secret=None), "second_factor_unavailable"),
    ):
        svc = ApprovalService(Store(s.db_path), s, dispatcher, clock=clock)
        rid = await _high_risk(svc)
        with pytest.raises(ServiceError) as exc:
            await svc.decide(rid, _decision(svc.get(rid).fingerprint, totp_code="123456"))
        assert exc.value.code == code


async def test_restart_recovery_redials_interrupted_calls(
    settings: Settings, dispatcher: RecordingDispatcher, clock: Clock, service: ApprovalService
) -> None:
    view, _ = await service.create(make_create())
    await _to_awaiting(service, view.id)
    fresh = RecordingDispatcher()
    restarted = ApprovalService(Store(settings.db_path), settings, fresh, clock=clock)
    await restarted.recover_after_restart()
    await settle()
    after = restarted.get(view.id)
    assert after.state == S.PENDING_CALL and after.active_call_id is None
    assert [r.id for r in fresh.requested] == [view.id]


async def test_audit_log_records_lifecycle(service: ApprovalService) -> None:
    view, _ = await service.create(make_create())
    await _to_awaiting(service, view.id)
    await service.decide(view.id, _decision(view.fingerprint))
    events = [e["event"] for e in service.store.audit_events(view.id)]
    assert events == [
        "created",
        "queued",
        "dialing",
        "ringing",
        "answered",
        "presented",
        "decision",
    ]
