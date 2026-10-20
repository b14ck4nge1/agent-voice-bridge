from __future__ import annotations

import secrets

import httpx
import pytest

from agent_voice_bridge.config import Settings
from tests.conftest import AGENT_TOKEN, VOICE_TOKEN, LiveBridge, make_create

A = {"Authorization": f"Bearer {AGENT_TOKEN}"}
V = {"Authorization": f"Bearer {VOICE_TOKEN}"}


def _create(bridge: LiveBridge, **kw: object) -> tuple[dict[str, object], str]:
    payload = make_create(**kw)
    r = httpx.post(bridge.url + "/v1/approvals", json=payload.model_dump(mode="json"), headers=A)
    assert r.status_code == 201, r.text
    return r.json()["request"], payload.request_secret


def test_auth_is_required_and_roles_are_separate(live_bridge: LiveBridge) -> None:
    body = make_create().model_dump(mode="json")
    assert httpx.post(live_bridge.url + "/v1/approvals", json=body).status_code == 401
    bad = {"Authorization": "Bearer " + "x" * 40}
    assert httpx.post(live_bridge.url + "/v1/approvals", json=body, headers=bad).status_code == 401
    # The voice token cannot create requests (the assistant cannot invent approvals)...
    assert httpx.post(live_bridge.url + "/v1/approvals", json=body, headers=V).status_code == 403
    # ...and the agent token cannot decide them.
    assert httpx.get(live_bridge.url + "/v1/voice/pending", headers=A).status_code == 403


def test_create_wait_and_idempotency(live_bridge: LiveBridge) -> None:
    payload = make_create()
    body = payload.model_dump(mode="json")
    r1 = httpx.post(live_bridge.url + "/v1/approvals", json=body, headers=A)
    r2 = httpx.post(live_bridge.url + "/v1/approvals", json=body, headers=A)
    assert (r1.status_code, r2.status_code) == (201, 200)
    rid = r1.json()["request"]["id"]
    assert r2.json()["request"]["id"] == rid
    w = httpx.get(
        f"{live_bridge.url}/v1/approvals/{rid}/wait?timeout=0",
        headers={**A, "X-AVB-Request-Secret": payload.request_secret},
    )
    assert w.status_code == 200 and w.json()["state"] == "PENDING_CALL" and not w.json()["final"]
    wrong = httpx.get(
        f"{live_bridge.url}/v1/approvals/{rid}/wait?timeout=0",
        headers={**A, "X-AVB-Request-Secret": secrets.token_urlsafe(32)},
    )
    assert wrong.status_code == 404


def test_extra_fields_rejected(live_bridge: LiveBridge) -> None:
    body = make_create().model_dump(mode="json") | {"auto_approve": True}
    assert httpx.post(live_bridge.url + "/v1/approvals", json=body, headers=A).status_code == 422


def test_voice_flow_over_http(live_bridge: LiveBridge) -> None:
    req, secret = _create(live_bridge)
    rid = req["id"]
    pending = httpx.get(live_bridge.url + "/v1/voice/pending", headers=V).json()
    assert [p["id"] for p in pending] == [rid]
    for ev in ("dialing", "ringing", "answered", "presented"):
        r = httpx.post(
            f"{live_bridge.url}/v1/voice/approvals/{rid}/call-events",
            json={"event": ev, "call_id": "c1"},
            headers=V,
        )
        assert r.status_code == 200, r.text
    decision = {
        "decision": "approve",
        "fingerprint": req["fingerprint"],
        "call_id": "c1",
        "nonce": secrets.token_hex(16),
        "confirmed": True,
    }
    r = httpx.post(f"{live_bridge.url}/v1/voice/approvals/{rid}/decision", json=decision, headers=V)
    assert r.status_code == 200 and r.json()["state"] == "APPROVED"
    replay = httpx.post(
        f"{live_bridge.url}/v1/voice/approvals/{rid}/decision",
        json=decision | {"decision": "reject"},
        headers=V,
    )
    assert replay.status_code == 409 and replay.json()["error"] == "replay"
    w = httpx.get(
        f"{live_bridge.url}/v1/approvals/{rid}/wait?timeout=1",
        headers={**A, "X-AVB-Request-Secret": secret},
    )
    assert w.json() == {
        "id": rid,
        "state": "APPROVED",
        "final": True,
        "decision": "approve",
        "message": None,
    }


def test_openapi_document(live_bridge: LiveBridge) -> None:
    spec = httpx.get(live_bridge.url + "/openapi.json").json()
    assert "/v1/approvals" in spec["paths"]
    assert "/v1/voice/approvals/{request_id}/decision" in spec["paths"]


@pytest.mark.parametrize(
    "kw",
    [
        dict(agent_token=None),
        dict(agent_token="short"),
        dict(voice_token="a" * 40, agent_token="a" * 40),
        dict(host="0.0.0.0"),
    ],  # noqa: S104
)
def test_settings_validation(settings: Settings, kw: dict[str, object]) -> None:
    from dataclasses import replace

    with pytest.raises(ValueError):
        replace(settings, **kw).validate()  # type: ignore[arg-type]
