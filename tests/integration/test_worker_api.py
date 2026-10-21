from __future__ import annotations

import asyncio
from typing import Any

import httpx

from agent_voice_bridge.api.app import create_app
from agent_voice_bridge.assistant.runtime import AssistantRuntime
from agent_voice_bridge.assistant.worker_hub import WorkerHub, WorkerOffline
from agent_voice_bridge.config import Settings
from tests.conftest import AGENT_TOKEN, VOICE_TOKEN, RecordingDispatcher

AUTH = {"Authorization": f"Bearer {AGENT_TOKEN}"}


async def test_worker_endpoints_round_trip(settings: Settings) -> None:
    hub = WorkerHub()
    app = create_app(settings, dispatcher=RecordingDispatcher(), worker_hub=hub)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        bad = await client.post(
            "/v1/worker/poll?wait=0", headers={"Authorization": f"Bearer {VOICE_TOKEN}"}
        )
        assert bad.status_code == 403
        assert (await client.post("/v1/worker/poll?wait=0", headers=AUTH)).json() == {"jobs": []}
        assert hub.online

        async def pc() -> None:
            jobs = (await client.post("/v1/worker/poll?wait=2", headers=AUTH)).json()["jobs"]
            assert jobs[0]["kind"] == "list_tasks"
            r = await client.post(
                f"/v1/worker/jobs/{jobs[0]['id']}/result", headers=AUTH, json={"result": {"n": 1}}
            )
            assert r.json() == {"accepted": True}

        helper = asyncio.create_task(pc())
        assert await hub.run("list_tasks", {}, timeout=5) == {"n": 1}
        await helper

        events: list[dict[str, Any]] = []
        hub.on_task_finished = events.append
        r = await client.post(
            "/v1/worker/events",
            headers=AUTH,
            json={"task_id": "t1", "status": "done", "summary": "ok"},
        )
        assert r.status_code == 200 and events[0]["task_id"] == "t1"


async def test_offline_worker() -> None:
    hub = WorkerHub()
    try:
        await hub.run("list_tasks", {})
    except WorkerOffline:
        pass
    else:
        raise AssertionError("expected WorkerOffline")


class Reporter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_now(self, title: str) -> None:
        self.calls.append(title)


async def test_finished_task_triggers_a_call() -> None:
    rt = AssistantRuntime(stt=object(), tts=object(), brain_factory=lambda s, t: None)  # type: ignore[arg-type]
    reporter = Reporter()
    rt.reporter = reporter
    rt.worker.task_finished(
        {
            "task_id": "t1",
            "status": "done",
            "title": "Stripe refunds",
            "summary": "POST /v1/refunds",
        }
    )
    await asyncio.sleep(0)
    assert reporter.calls == ["Stripe refunds"]
    assert "POST /v1/refunds" in rt.hub.reasons[0]


def test_inbound_code_is_off_by_default(monkeypatch: Any) -> None:
    def make() -> AssistantRuntime:
        return AssistantRuntime(stt=object(), tts=object(), brain_factory=lambda s, t: None)  # type: ignore[arg-type]

    monkeypatch.delenv("AVB_INBOUND_CODE", raising=False)
    assert make().inbound_code is False
    monkeypatch.setenv("AVB_INBOUND_CODE", "1")
    assert make().inbound_code is True


async def test_due_reminder_calls_the_phone(tmp_path: Any) -> None:
    from agent_voice_bridge.assistant.personal import PersonalStore

    rt = AssistantRuntime(
        stt=object(),  # type: ignore[arg-type]
        tts=object(),  # type: ignore[arg-type]
        brain_factory=lambda s, t: None,  # type: ignore[arg-type,return-value]
        personal=PersonalStore(tmp_path / "p.json"),
    )
    reporter = Reporter()
    rt.reporter = reporter
    rt._reminder_due({"text": "drink water"})
    await asyncio.sleep(0)
    assert reporter.calls == ["Reminder"] and "drink water" in rt.hub.reasons[0]
