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


# Words a coding assistant hears often; passed as a hint so they're spelled right.
VOCABULARY = (
    "Claude Code, Codex, approve, reject, npm, pnpm, git, GitHub, API, pull request, "
    "repository, commit, deploy, Python, JavaScript, TypeScript, Stripe, Docker."
)


class ApiSTT:
    """Speech recognition through an OpenAI-style ``/v1/audio/transcriptions`` API.

    Works with OpenAI, gateways such as api-models.com, and other compatible services.
    Settings: ``AVB_STT_MODEL`` (default ``gpt-4o-mini-transcribe``), ``AVB_STT_BASE_URL``
    and ``AVB_STT_API_KEY`` (default: the brain's gateway URL and key when the brain
    uses the OpenAI style), ``AVB_STT_LANGUAGE``.
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        language: str | None = None,
        client: Any = None,
        timeout: float = 20,
    ) -> None:
        env = os.environ.get
        self.model = model or env("AVB_STT_MODEL") or "gpt-4o-mini-transcribe"
        self.language = language or env("AVB_STT_LANGUAGE") or None
        if client is None:
            import openai

            brain_openai = (env("AVB_BRAIN") or "") == "openai"
            client = openai.AsyncOpenAI(
                base_url=base_url
                or env("AVB_STT_BASE_URL")
                or (env("AVB_BRAIN_BASE_URL") if brain_openai else None)
                or None,
                api_key=api_key or env("AVB_STT_API_KEY") or env("AVB_BRAIN_API_KEY") or None,
                timeout=timeout,
                max_retries=1,
            )
        self.client = client

    async def transcribe(self, pcm16_8k: bytes) -> str:
        import io
        import wave

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:  # 16 kHz suits every model
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(resample(pcm16_8k, 8000, 16000))
        extra: dict[str, Any] = {"prompt": VOCABULARY}
        if self.language:
            extra["language"] = self.language
        result = await self.client.audio.transcriptions.create(
            model=self.model, file=("speech.wav", buf.getvalue(), "audio/wav"), **extra
        )
        text = getattr(result, "text", result)
        return str(text or "").strip()


def make_stt() -> SpeechToText:
    """``AVB_STT=api`` (online service) or ``local`` (faster-whisper, the default)."""
    if (os.environ.get("AVB_STT") or "local").strip().lower() == "api":
        return ApiSTT()
    return WhisperSTT()


async def stt_test() -> int:
    """``avb-admin stt-test``: speak a known sentence (espeak) and transcribe it."""
    import time

    from agent_voice_bridge.voice.tts.engines import EspeakTTS

    sentence = "Please approve the request from Claude Code to run npm install."
    try:
        pcm, _ = await EspeakTTS().synthesize(sentence)
        stt = make_stt()
        print(
            f"Speech-to-text: {type(stt).__name__} {getattr(stt, 'model', getattr(stt, 'model_name', ''))}"
        )
        started = time.monotonic()
        text = await stt.transcribe(pcm)
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"Said:  {sentence}")
    print(f"Heard: {text}  ({time.monotonic() - started:.1f} s)")
    return 0 if text else 1
