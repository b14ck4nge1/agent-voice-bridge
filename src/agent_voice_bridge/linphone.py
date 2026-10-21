"""The "linphone" phone path: the bridge calls your Linphone account through sip.linphone.org.

The bridge signs in with its own free Linphone account and calls yours. Linphone's push
service wakes the iPhone, so it rings like a normal call even when locked; no Bark.
Choose it with ``AVB_CALL_DISPATCHER=linphone``. Settings (environment or ``.env``):

=============================  ======================================================
AVB_LINPHONE_USERNAME          The bridge's own Linphone account (required)
AVB_LINPHONE_PASSWORD          Its password (required)
AVB_LINPHONE_CALL              Your Linphone account, e.g. ``alice`` (required)
AVB_LINPHONE_DOMAIN            Default ``sip.linphone.org``
AVB_LINPHONE_PROXY             ``host[:port]`` of the TLS proxy (default: domain, 5061)
AVB_LINPHONE_REQUIRE_SRTP      ``1`` (default) refuses calls without encrypted audio
AVB_LINPHONE_RING_SECONDS      How long each call rings (default 60)
AVB_LINPHONE_RETRIES           Calls per request if unanswered (default 3)
AVB_LINPHONE_RETRY_SECONDS     Pause between them (default 60)
AVB_SIP_MEDIA_IP               The server's public IP, for audio (needed on AWS/NAT)
AVB_RTP_PORTS                  Default ``40000-40200`` (UDP, must be open)
=============================  ======================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from agent_voice_bridge.approvals.service import ApprovalService
from agent_voice_bridge.assistant.runtime import AssistantRuntime
from agent_voice_bridge.models import ApprovalState, ApprovalView
from agent_voice_bridge.phone import make_tts
from agent_voice_bridge.sip.adapter.base import CallDispatcher
from agent_voice_bridge.sip.server import DialError, SipCall
from agent_voice_bridge.sip.uac import AccountConfig, SipAccountClient
from agent_voice_bridge.voice.phone_call import PhoneCallHandler
from agent_voice_bridge.voice.tts.base import TextToSpeech

log = logging.getLogger(__name__)


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class LinphoneSettings:
    account: AccountConfig
    target: str
    ring_seconds: float = 60
    retries: int = 3
    retry_seconds: float = 60
    tts_voice: str | None = None
    tts_rate: int = 0

    @classmethod
    def from_env(cls) -> LinphoneSettings:
        missing = [
            n
            for n in ("AVB_LINPHONE_USERNAME", "AVB_LINPHONE_PASSWORD", "AVB_LINPHONE_CALL")
            if not _get(n)
        ]
        if missing:
            raise ValueError(f"AVB_CALL_DISPATCHER=linphone needs: {', '.join(missing)}")
        domain = _get("AVB_LINPHONE_DOMAIN", "sip.linphone.org") or "sip.linphone.org"
        proxy = _get("AVB_LINPHONE_PROXY", domain) or domain
        host, _, port = proxy.partition(":")
        lo, _, hi = (_get("AVB_RTP_PORTS", "40000-40200") or "").partition("-")
        username = _get("AVB_LINPHONE_USERNAME") or ""
        target = _get("AVB_LINPHONE_CALL") or ""
        if target.split("@")[0].removeprefix("sip:") == username:
            raise ValueError(
                "AVB_LINPHONE_CALL is the bridge's own account; the bridge needs a second "
                "Linphone account to call yours from"
            )
        return cls(
            account=AccountConfig(
                username=username,
                password=_get("AVB_LINPHONE_PASSWORD") or "",
                domain=domain,
                proxy_host=host,
                proxy_port=int(port or 5061),
                media_ip=_get("AVB_SIP_MEDIA_IP"),
                rtp_ports=(int(lo), int(hi or lo)),
                require_srtp=(_get("AVB_LINPHONE_REQUIRE_SRTP", "1") == "1"),
                accept_calls_from=target,
            ),
            target=target,
            ring_seconds=float(_get("AVB_LINPHONE_RING_SECONDS", "60") or 60),
            retries=int(_get("AVB_LINPHONE_RETRIES", "3") or 3),
            retry_seconds=float(_get("AVB_LINPHONE_RETRY_SECONDS", "60") or 60),
            tts_voice=_get("AVB_TTS_VOICE"),
            tts_rate=int(_get("AVB_TTS_RATE", "0") or 0),
        )


class LinphoneDispatcher:
    """Calls the phone while requests wait; one call covers every waiting request."""

    def __init__(
        self,
        client: SipAccountClient,
        target: str,
        *,
        ring_seconds: float = 60,
        retries: int = 3,
        retry_seconds: float = 60,
        between_calls: float = 3,
    ) -> None:
        self.client = client
        self.target = target
        self.ring_seconds = ring_seconds
        self.retries = retries
        self.retry_seconds = retry_seconds
        self.between_calls = between_calls
        self.waiting: Callable[[], bool] = lambda: False
        self._loop: asyncio.Task[None] | None = None
        self._ringing = False

    def attach(self, service: ApprovalService, *, also: Callable[[], bool] | None = None) -> None:
        """``also``: another reason to keep calling (e.g. a task report to deliver)."""
        extra = also or (lambda: False)
        self.waiting = lambda: (
            extra() or any(r.state == ApprovalState.PENDING_CALL for r in service.pending())
        )

    async def call_now(self, title: str) -> None:
        """Call to report something (a finished task); the call picks up the reason."""
        if self._loop is not None and not self._loop.done():
            return
        if not self.client.registered.is_set():
            log.warning("can't call about %r: not signed in to Linphone", title)
            return
        self._loop = asyncio.get_running_loop().create_task(self._call_loop())

    async def request_call(self, request: ApprovalView) -> None:
        if self._loop is not None and not self._loop.done():
            log.info("a call is already under way; it covers %s too", request.id)
            return
        if not self.client.registered.is_set():
            # Fails the request now (the agent is denied) instead of leaving it waiting.
            raise DialError(f"not signed in to Linphone: {self.client.last_error or '...'}")
        self._loop = asyncio.get_running_loop().create_task(self._call_loop())

    async def _call_loop(self) -> None:
        failures = 0
        while self.waiting() and failures < self.retries:
            self._ringing = True
            try:
                call = await self.client.dial(self.target, ring_seconds=self.ring_seconds)
            except DialError as exc:
                failures += 1
                log.warning(
                    "call to %s failed (%d/%d): %s", self.target, failures, self.retries, exc
                )
                pause = self.retry_seconds
            else:
                failures = 0
                pause = self.between_calls
                self._ringing = False
                await call.ended.wait()
            finally:
                self._ringing = False
            if self.waiting():
                await asyncio.sleep(pause)
        if self.waiting():
            log.warning(
                "no answer after %d calls; requests keep waiting until they expire", self.retries
            )

    async def cancel_call(self, request_id: str, call_id: str | None) -> None:
        if self._ringing and not self.waiting() and self._loop is not None:
            self._loop.cancel()  # hangs up the ringing phone (CANCEL)

    def close(self) -> None:
        if self._loop is not None:
            self._loop.cancel()


class LinphoneStack:
    def __init__(
        self,
        settings: LinphoneSettings,
        *,
        tts: TextToSpeech | None = None,
        assistant: AssistantRuntime | None = None,
    ) -> None:
        self.settings = settings
        self.assistant = assistant
        self.tts = tts or (assistant.tts if assistant else make_tts(settings))
        self._handler: Callable[[SipCall], Awaitable[None]] | None = None
        self.client = SipAccountClient(settings.account, self._on_call)
        self.dispatcher = LinphoneDispatcher(
            self.client,
            settings.target,
            ring_seconds=settings.ring_seconds,
            retries=settings.retries,
            retry_seconds=settings.retry_seconds,
        )

    async def _on_call(self, call: SipCall) -> None:
        assert self._handler is not None
        await self._handler(call)

    @property
    def call_dispatcher(self) -> CallDispatcher:
        return self.assistant.wrap(self.dispatcher) if self.assistant else self.dispatcher

    @asynccontextmanager
    async def run(self, service: ApprovalService) -> AsyncIterator[None]:
        if self.assistant is not None:
            self._handler = self.assistant.handler(service)
            hub = self.assistant.hub
            self.dispatcher.attach(service, also=lambda: bool(hub.reasons))
            asyncio.get_running_loop().create_task(self.assistant.warm_up())
        else:
            self._handler = PhoneCallHandler(service, self.tts)
            self.dispatcher.attach(service)
        await self.client.start()  # wrong password etc. stops start-up with a clear error
        log.info(
            "Phone path ready: calling %s via %s",
            self.settings.target,
            self.settings.account.domain,
        )
        try:
            yield
        finally:
            self.dispatcher.close()
            with contextlib.suppress(Exception):
                await self.client.stop()
