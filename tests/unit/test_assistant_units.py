from __future__ import annotations

import json
import math
import struct
from types import SimpleNamespace
from typing import Any

from agent_voice_bridge.assistant.brain import AnthropicBrain, OpenAIBrain, Tool
from agent_voice_bridge.assistant.vad import Endpointer


def _tone(seconds: float, amp: int) -> bytes:
    n = int(8000 * seconds)
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * 300 * i / 8000))) for i in range(n)
    )


def test_endpointer_finds_utterances_and_barge_in() -> None:
    got: list[bytes] = []
    starts: list[int] = []
    ep = Endpointer(got.append, on_speech_start=lambda: starts.append(1))
    ep.feed(_tone(1.0, 40))  # line noise
    ep.feed(_tone(1.2, 6000))  # speech
    ep.feed(_tone(1.0, 40))  # silence ends it
    assert len(got) == 1 and len(starts) == 1
    assert 1.0 <= len(got[0]) / 16000 <= 1.6
    ep.feed(_tone(0.04, 6000) + _tone(1.0, 40))  # a click: ignored
    assert len(got) == 1


async def _echo(args: dict[str, Any]) -> str:
    return f"echo {args['x']}"


TOOLS = [Tool("echo", "Echo x", {"type": "object", "properties": {"x": {"type": "string"}}}, _echo)]


class FakeAnthropic:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **kw: Any) -> Any:
        self.requests.append({**kw, "messages": list(kw["messages"])})  # as sent
        if len(self.requests) == 1:
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[
                    SimpleNamespace(type="text", text="Let me check."),
                    SimpleNamespace(type="tool_use", id="tu1", name="echo", input={"x": "hi"}),
                ],
            )
        return SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text="It said hi.")]
        )


async def test_anthropic_brain_tool_loop() -> None:
    fake = FakeAnthropic()
    brain = AnthropicBrain("sys", TOOLS, client=fake)
    assert await brain.respond("hello") == "Let me check. It said hi."
    second = fake.requests[1]
    assert second["model"] == "claude-haiku-4-5" and second["system"] == "sys"
    result = second["messages"][-1]["content"][0]
    assert result == {"type": "tool_result", "tool_use_id": "tu1", "content": "echo hi"}
    assert second["tools"][0]["input_schema"]["properties"]["x"]["type"] == "string"


class FakeOpenAI:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kw: Any) -> Any:
        self.requests.append(json.loads(json.dumps(kw)))
        if len(self.requests) == 1:
            call = SimpleNamespace(
                id="c1", function=SimpleNamespace(name="echo", arguments='{"x": "yo"}')
            )
            msg = SimpleNamespace(content=None, tool_calls=[call])
        else:
            msg = SimpleNamespace(content="Done: yo.", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


async def test_openai_brain_tool_loop() -> None:
    fake = FakeOpenAI()
    brain = OpenAIBrain("sys", TOOLS, client=fake, model="gpt-5.6-sol")
    assert await brain.respond("hello") == "Done: yo."
    msgs = fake.requests[1]["messages"]
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[-1] == {"role": "tool", "tool_call_id": "c1", "content": "echo yo"}
    assert fake.requests[0]["model"] == "gpt-5.6-sol"


async def test_unknown_tool_and_bad_args_are_reported_to_the_model() -> None:
    from agent_voice_bridge.assistant.brain import _call_tool

    tools = {t.name: t for t in TOOLS}
    assert (await _call_tool(tools, "nope", {})).startswith("error")
    assert (await _call_tool(tools, "echo", None)).startswith("error")
    assert (await _call_tool(tools, "echo", {})).startswith("error: KeyError")


def test_endpointer_ignores_echo_while_talking() -> None:
    got: list[bytes] = []
    playing = [True]
    ep = Endpointer(got.append, boost=lambda: 10.0 if playing[0] else 1.0)
    ep.feed(_tone(0.5, 40))
    ep.feed(_tone(1.0, 2000) + _tone(1.0, 40))  # our echo, quieter than real speech
    assert got == []
    playing[0] = False
    ep.feed(_tone(1.0, 2000) + _tone(1.0, 40))
    assert len(got) == 1


async def test_cancelled_turn_leaves_no_trace_in_history() -> None:
    import asyncio

    class Slow:
        def __init__(self) -> None:
            self.messages = self

        async def create(self, **kw: Any) -> Any:
            await asyncio.sleep(10)

    brain = AnthropicBrain("sys", TOOLS, client=Slow())
    task = asyncio.ensure_future(brain.respond("half a sentence"))
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert brain.messages == []
