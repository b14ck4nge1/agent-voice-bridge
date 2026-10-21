"""Text-to-speech interface (Phase 4). Implementations: Windows SAPI, Kokoro, cloud APIs."""

from __future__ import annotations

from typing import Protocol


class TextToSpeech(Protocol):
    async def synthesize(self, text: str) -> tuple[bytes, int]:
        """Return 16-bit mono PCM audio and its sample rate."""
