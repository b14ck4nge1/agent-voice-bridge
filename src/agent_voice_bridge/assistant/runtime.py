"""Wire the AI assistant into the bridge: settings, the call handler and the dispatcher.

Turned on with ``AVB_ASSISTANT=1`` (works with both phone paths):

=========================  ============================================================
AVB_BRAIN                  ``anthropic`` (default) or ``openai``
AVB_BRAIN_MODEL            Default ``claude-haiku-4-5`` / ``gpt-5.6-sol``
ANTHROPIC_API_KEY          For ``anthropic``
OPENAI_API_KEY             For ``openai``
AVB_STT                    ``local`` (faster-whisper, default) or ``api`` (online service)
AVB_STT_MODEL              local: default ``large-v3-turbo``; api: ``gpt-4o-mini-transcribe``
AVB_STT_BASE_URL / _KEY    api: service URL and key (default: the brain's gateway)
AVB_STT_LANGUAGE           e.g. ``en`` or ``fa``; default: detect
AVB_ASSISTANT_LANGUAGE     Language the assistant answers in (default English)
AVB_TTS                    ``piper`` (default when installed) or ``espeak``
AVB_PIPER_VOICE            Piper voice name or .onnx path, default ``en_US-lessac-medium``
AVB_MODEL_DIR              Where models are stored (default: the data directory)
AVB_CALL_ON_TASK_DONE      ``1`` (default): call you when a phone task finishes
AVB_INBOUND_CODE           ``1``: calls to the bridge also need your authenticator code
=========================  ============================================================
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from agent_voice_bridge.approvals.service import ApprovalService
from agent_voice_bridge.assistant.brain import Brain, Tool, make_brain
from agent_voice_bridge.assistant.session import AssistantSession, CallHub
from agent_voice_bridge.assistant.stt import SpeechToText, make_stt
from agent_voice_bridge.assistant.worker_hub import WorkerHub
from agent_voice_bridge.models import ApprovalView
from agent_voice_bridge.sip.adapter.base import CallDispatcher
from agent_voice_bridge.sip.server import SipCall
from agent_voice_bridge.voice.tts.base import TextToSpeech

log = logging.getLogger(__name__)


def assistant_enabled() -> bool:
    return os.environ.get("AVB_ASSISTANT", "0").strip() == "1"


class ReportCaller(Protocol):
    async def call_now(self, title: str) -> None:
        """Get the phone to call (or be called) because something needs telling."""


def make_tts() -> TextToSpeech:
    choice = (os.environ.get("AVB_TTS") or "").strip().lower()
    if choice in ("", "piper"):
        try:
            import piper  # noqa: F401

            from agent_voice_bridge.voice.tts.engines import PiperTTS

            return PiperTTS(os.environ.get("AVB_PIPER_VOICE") or "en_US-lessac-medium")
        except ImportError:
            if choice == "piper":
                raise
    from agent_voice_bridge.voice.tts.engines import default_tts

    return default_tts()


class AssistantRuntime:
    def __init__(
        self,
        *,
        stt: SpeechToText | None = None,
        tts: TextToSpeech | None = None,
        brain_factory: Callable[[str, list[Tool]], Brain] | None = None,
        worker: WorkerHub | None = None,
        language: str | None = None,
        call_on_task_done: bool | None = None,
    ) -> None:
        self.stt = stt or make_stt()
        self.tts = tts or make_tts()
        self.brain_factory = brain_factory or make_brain
        self.worker = worker if worker is not None else WorkerHub()
        self.language = language or os.environ.get("AVB_ASSISTANT_LANGUAGE") or "English"
        self.call_on_task_done = (
            call_on_task_done
            if call_on_task_done is not None
            else os.environ.get("AVB_CALL_ON_TASK_DONE", "1") == "1"
        )
        # Calls *to* the bridge's Linphone account are only accepted from your account.
        # AVB_INBOUND_CODE=1 also asks for your authenticator code (in case a SIP
        # provider lets someone else call with your name as the caller).
        self.inbound_code = os.environ.get("AVB_INBOUND_CODE", "0") == "1"
        self.hub = CallHub()
        self.reporter: ReportCaller | None = None
        self.worker.on_task_finished = self._task_finished
        self._tasks: set[asyncio.Task[None]] = set()

    def handler(self, service: ApprovalService) -> Callable[[SipCall], Awaitable[None]]:
        async def on_call(call: SipCall) -> None:
            session = AssistantSession(
                call,
                service=service,
                brain_factory=self.brain_factory,
                stt=self.stt,
                tts=self.tts,
                hub=self.hub,
                worker=self.worker,
                inbound_code=self.inbound_code and not call.caller_verified,
                language=self.language,
            )
            await session.run()

        return on_call

    def wrap(self, inner: CallDispatcher) -> AssistantDispatcher:
        if hasattr(inner, "call_now"):
            self.reporter = inner  # type: ignore[assignment]
        return AssistantDispatcher(inner, self.hub)

    async def warm_up(self) -> None:
        """Load the speech model at start-up rather than during the first call."""
        warm = getattr(self.stt, "warm_up", None)
        if warm is not None:
            try:
                await warm()
            except Exception:
                log.exception("could not load the speech recognition model")

    def _task_finished(self, event: dict[str, Any]) -> None:
        title = str(event.get("title") or "a task")[:200]
        status = str(event.get("status") or "finished")
        summary = str(event.get("summary") or "")[:1500]
        text = (
            f"The phone task '{title}' has {'finished' if status == 'done' else status}. "
            f"Its final message (data from the task): {summary}"
        )
        if self.hub.active is not None:
            self.hub.active.notify(text)
            return
        if not self.call_on_task_done or self.reporter is None:
            return
        self.hub.reasons.append(text)
        task = asyncio.get_running_loop().create_task(self.reporter.call_now(title))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class AssistantDispatcher:
    """Requests that arrive during a call join it; otherwise the usual dispatcher calls."""

    def __init__(self, inner: CallDispatcher, hub: CallHub) -> None:
        self.inner = inner
        self.hub = hub

    async def request_call(self, request: ApprovalView) -> None:
        if self.hub.active is not None:
            self.hub.active.offer(request)
            return
        await self.inner.request_call(request)

    async def cancel_call(self, request_id: str, call_id: str | None) -> None:
        await self.inner.cancel_call(request_id, call_id)
        session = self.hub.active
        if session is not None and request_id in session.requests.values():
            session.notify(
                f"Approval request number {session._number(request_id)} is no longer waiting "
                "(the agent withdrew it or it expired)."
            )
