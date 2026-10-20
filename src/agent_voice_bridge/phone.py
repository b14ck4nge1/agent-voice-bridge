"""Assemble the bark-sip phone path: Bark dispatcher + SIP server + call handler."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agent_voice_bridge.approvals.service import ApprovalService
from agent_voice_bridge.models import ApprovalState
from agent_voice_bridge.notify.bark import BarkNotifier
from agent_voice_bridge.phone_config import PhoneSettings
from agent_voice_bridge.sip.adapter.bark_wake import BarkWakeDispatcher
from agent_voice_bridge.sip.server import SipServer
from agent_voice_bridge.voice.phone_call import PhoneCallHandler
from agent_voice_bridge.voice.tts.base import TextToSpeech
from agent_voice_bridge.voice.tts.engines import EspeakTTS, SapiTTS, default_tts

log = logging.getLogger(__name__)


def make_tts(settings: PhoneSettings) -> TextToSpeech:
    tts = default_tts()
    if isinstance(tts, SapiTTS):
        return SapiTTS(voice=settings.tts_voice, rate=settings.tts_rate)
    assert isinstance(tts, EspeakTTS)
    return tts


class PhoneStack:
    def __init__(
        self,
        settings: PhoneSettings,
        *,
        tts: TextToSpeech | None = None,
        notifier: BarkNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.tts = tts or make_tts(settings)
        self.dispatcher = BarkWakeDispatcher(
            notifier or BarkNotifier(settings.bark), settings.dial_url
        )
        self.sip: SipServer | None = None

    @asynccontextmanager
    async def run(self, service: ApprovalService) -> AsyncIterator[None]:
        def state_of(request_id: str) -> ApprovalState | None:
            row = service.store.get_request(request_id)
            return ApprovalState(row["state"]) if row else None

        self.dispatcher.attach(state_of)
        self.sip = SipServer(self.settings.sip, PhoneCallHandler(service, self.tts))
        await self.sip.start()
        log.info("Phone path ready: Bark taps dial %s", self.settings.dial_url)
        try:
            yield
        finally:
            self.dispatcher.close()
            await self.sip.stop()
