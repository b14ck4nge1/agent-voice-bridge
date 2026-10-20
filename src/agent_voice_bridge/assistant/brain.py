"""The assistant's "brain": an LLM that talks with you and calls tools.

Two providers, chosen with ``AVB_BRAIN``:

* ``anthropic`` (default) - Claude via the Anthropic API, model ``AVB_BRAIN_MODEL``
  (default ``claude-haiku-4-5``), key ``ANTHROPIC_API_KEY``.
* ``openai`` - an OpenAI chat model, ``AVB_BRAIN_MODEL`` (e.g. ``gpt-5.6-sol``), key
  ``OPENAI_API_KEY``.

To go through an API gateway instead (one that offers the Anthropic Messages API or the
OpenAI Chat Completions API), set ``AVB_BRAIN_BASE_URL`` (e.g. ``https://gateway/v1``
for OpenAI-style) and ``AVB_BRAIN_API_KEY``. ``avb-admin brain-test`` checks it works,
tool calls included.

The brain only *talks*. Anything with consequences (approving, starting a task) goes
through tools whose effect the code confirms with you itself - the model can't approve.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (object)
    run: Callable[[dict[str, Any]], Awaitable[str]]


class Brain(Protocol):
    async def respond(self, text: str) -> str:
        """Add the user's words (or an event note) and return what to say."""


async def _call_tool(tools: dict[str, Tool], name: str, args: Any) -> str:
    tool = tools.get(name)
    if tool is None:
        return f"error: unknown tool {name}"
    if not isinstance(args, dict):
        return "error: tool arguments must be an object"
    try:
        return await tool.run(args)
    except Exception as exc:  # the model sees the error and can tell the user
        log.exception("tool %s failed", name)
        return f"error: {type(exc).__name__}: {exc}"


class AnthropicBrain:
    def __init__(
        self,
        system: str,
        tools: list[Tool],
        *,
        model: str = "claude-haiku-4-5",
        client: Any = None,
        max_tokens: int = 1024,
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic()
        self.client = client
        self.model = model
        self.system = system
        self.tools = {t.name: t for t in tools}
        self.max_tokens = max_tokens
        self.messages: list[dict[str, Any]] = []

    def _tool_defs(self) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in self.tools.values()
        ]

    async def respond(self, text: str) -> str:
        self.messages.append({"role": "user", "content": text})
        spoken: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS):
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.system,
                tools=self._tool_defs(),
                messages=self.messages,
            )
            self.messages.append({"role": "assistant", "content": resp.content})
            spoken += [b.text for b in resp.content if b.type == "text" and b.text.strip()]
            if resp.stop_reason == "refusal":
                return "Sorry, I can't help with that."
            calls = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not calls:
                break
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": c.id,
                    "content": await _call_tool(self.tools, c.name, c.input),
                }
                for c in calls
            ]
            self.messages.append({"role": "user", "content": results})
        return " ".join(spoken).strip()


class OpenAIBrain:
    def __init__(
        self,
        system: str,
        tools: list[Tool],
        *,
        model: str = "gpt-5.6-sol",
        client: Any = None,
    ) -> None:
        if client is None:
            import openai

            client = openai.AsyncOpenAI()
        self.client = client
        self.model = model
        self.tools = {t.name: t for t in tools}
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    def _tool_defs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self.tools.values()
        ]

    async def respond(self, text: str) -> str:
        self.messages.append({"role": "user", "content": text})
        spoken: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS):
            resp = await self.client.chat.completions.create(
                model=self.model, messages=self.messages, tools=self._tool_defs()
            )
            msg = resp.choices[0].message
            entry: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.function.name, "arguments": c.function.arguments},
                    }
                    for c in msg.tool_calls
                ]
            self.messages.append(entry)
            if msg.content and msg.content.strip():
                spoken.append(msg.content.strip())
            if not msg.tool_calls:
                break
            for c in msg.tool_calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except ValueError:
                    args = None
                result = await _call_tool(self.tools, c.function.name, args)
                self.messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
        return " ".join(spoken).strip()


def make_brain(system: str, tools: list[Tool]) -> Brain:
    provider = (os.environ.get("AVB_BRAIN") or "anthropic").strip().lower()
    model = (os.environ.get("AVB_BRAIN_MODEL") or "").strip()
    base_url = (os.environ.get("AVB_BRAIN_BASE_URL") or "").strip() or None
    api_key = (os.environ.get("AVB_BRAIN_API_KEY") or "").strip() or None
    if provider == "anthropic":
        import anthropic

        client: Any = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
        return AnthropicBrain(system, tools, model=model or "claude-haiku-4-5", client=client)
    if provider == "openai":
        import openai

        client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        return OpenAIBrain(system, tools, model=model or "gpt-5.6-sol", client=client)
    raise ValueError("AVB_BRAIN must be anthropic or openai")


async def brain_test() -> int:
    """``avb-admin brain-test``: one question that needs a tool call, through the settings."""
    called: list[dict[str, Any]] = []

    async def clock(args: dict[str, Any]) -> str:
        called.append(args)
        return "It is 10:42 in the morning."

    tools = [
        Tool(
            "get_time",
            "The current local time.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            clock,
        )
    ]
    provider = os.environ.get("AVB_BRAIN") or "anthropic"
    print(
        f"Brain: {provider}, model {os.environ.get('AVB_BRAIN_MODEL') or '(default)'}, "
        f"via {os.environ.get('AVB_BRAIN_BASE_URL') or 'the official API'}"
    )
    try:
        brain = make_brain("You are a phone assistant. Answer in one short sentence.", tools)
        reply = await brain.respond("What time is it? Use the get_time tool.")
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"Reply: {reply!r}")
    print(f"Tool call: {'yes' if called else 'NO'}")
    if not called:
        print("The model answered without calling the tool; the assistant needs tool calls.")
        return 1
    print("OK")
    return 0
