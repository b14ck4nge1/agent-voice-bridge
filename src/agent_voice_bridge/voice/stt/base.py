"""Speech-to-text interface (Phase 4). Implementations: local faster-whisper, cloud APIs."""

from __future__ import annotations

from typing import Protocol


class SpeechToText(Protocol):
    async def transcribe(self, pcm16: bytes, sample_rate: int) -> str:
        """Transcribe one utterance of 16-bit mono PCM audio."""
