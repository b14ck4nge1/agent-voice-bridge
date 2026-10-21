from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agent_voice_bridge.assistant.personal import PersonalStore, Scheduler


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)  # 11:30 in Tehran

    def __call__(self) -> datetime:
        return self.now


def _store(tmp_path: Path, clock: Clock) -> PersonalStore:
    return PersonalStore(tmp_path / "p.json", zone=ZoneInfo("Asia/Tehran"), clock=clock)


async def test_schedule_list_cancel_and_persist(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    tools = {t.name: t for t in store.tools()}
    out = await tools["schedule_call"].run({"in_minutes": 10, "message": "take the pizza out"})
    assert "Wednesday 21 October, 11:40" in out
    out = await tools["schedule_call"].run({"at": "2026-10-22 07:00", "message": "wake up"})
    assert "Thursday 22 October, 07:00" in out
    assert "in the past" in await tools["schedule_call"].run(
        {"at": "2026-10-20 07:00", "message": "x"}
    )
    assert "11:40, take the pizza out" in await tools["list_scheduled_calls"].run({})

    reloaded = _store(tmp_path, clock)  # survives a restart
    assert [r["text"] for r in reloaded.reminders] == ["take the pizza out", "wake up"]
    rid = reloaded.reminders[1]["id"]
    assert (
        await {t.name: t for t in reloaded.tools()}["cancel_scheduled_call"].run({"id": rid})
        == "Cancelled."
    )
    assert len(_store(tmp_path, clock).reminders) == 1


async def test_memories_and_context(tmp_path: Path) -> None:
    store = _store(tmp_path, Clock())
    tools = {t.name: t for t in store.tools()}
    await tools["remember"].run({"fact": "The user's name is Kian"})
    await tools["remember"].run({"fact": "Likes short answers"})
    ctx = _store(tmp_path, Clock()).context()
    assert "11:30" in ctx and "Asia/Tehran" in ctx and "Kian" in ctx
    assert await tools["forget"].run({"text": "short"}) == "Forgot 1 thing(s)."
    assert "short answers" not in _store(tmp_path, Clock()).context()


async def test_scheduler_fires_due_calls(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.add_reminder(clock.now + timedelta(minutes=10), "stand up")
    fired: list[str] = []
    sched = Scheduler(store, lambda r: fired.append(r["text"]), interval=0.01)
    sched.start()
    await asyncio.sleep(0.05)
    assert fired == []
    clock.now += timedelta(minutes=11)
    await asyncio.sleep(0.05)
    assert fired == ["stand up"] and store.reminders == []
    await sched.stop()
