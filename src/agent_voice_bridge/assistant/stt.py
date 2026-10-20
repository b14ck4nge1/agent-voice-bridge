"""Speech recognition with faster-whisper (local, free). Default model: large-v3-turbo.

The model is downloaded on first use into ``AVB_MODEL_DIR`` (about 1.6 GB for
large-v3-turbo) and runs on the CPU with int8 weights. ``AVB_STT_MODEL`` picks a
smaller one (``small``, ``base``) for a small server; ``AVB_STT_LANGUAGE`` fixes the
language (e.g. ``en``, ``fa``) instead of detecting it on every sentence.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Protocol

from agent_voice_bridge.voice.audio import resample

log = logging.getLogger(__name__)


class SpeechToText(Protocol):
    async def transcribe(self, pcm16_8k: bytes) -> str: ...


class WhisperSTT:
    def __init__(
        self,
        model: str | None = None,
        *,
        language: str | None = None,
        model_dir: str | None = None,
        threads: int = 0,
    ) -> None:
        self.model_name = model or os.environ.get("AVB_STT_MODEL") or "large-v3-turbo"
        self.language = language or os.environ.get("AVB_STT_LANGUAGE") or None
        self.model_dir = model_dir or os.environ.get("AVB_MODEL_DIR") or None
        self.threads = threads
        self._model: Any = None
        self._lock = asyncio.Lock()

    def _load(self) -> Any:
        from faster_whisper import WhisperModel

        log.info("loading speech model %s (first time: downloading)", self.model_name)
        return WhisperModel(
            self.model_name,
            device="cpu",
            compute_type="int8",
            download_root=self.model_dir,
            cpu_threads=self.threads,
        )

    async def warm_up(self) -> None:
        async with self._lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load)

    def _run(self, pcm16_16k: bytes) -> str:
        import numpy as np

        audio = np.frombuffer(pcm16_16k, dtype="<i2").astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(
            audio,
            language=self.language,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        return "".join(s.text for s in segments).strip()

    async def transcribe(self, pcm16_8k: bytes) -> str:
        await self.warm_up()
        async with self._lock:  # one transcription at a time on a small CPU
            return await asyncio.to_thread(self._run, resample(pcm16_8k, 8000, 16000))
