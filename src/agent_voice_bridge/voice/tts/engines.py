"""Local text-to-speech engines that need no downloads or accounts.

* Windows: the built-in SAPI voices through ``System.Speech`` (PowerShell).
* Linux/macOS: ``espeak-ng`` if installed (used by the test suite).

The text is passed through an environment variable, never interpolated into a
command line, so request content can't inject shell or PowerShell syntax.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from agent_voice_bridge.voice.audio import TELEPHONE_RATE, resample, wav_to_pcm16

MAX_CHARS = 2000

_SAPI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($env:AVB_TTS_VOICE) { $s.SelectVoice($env:AVB_TTS_VOICE) }
$s.Rate = [int]$env:AVB_TTS_RATE
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(8000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($env:AVB_TTS_OUT, $fmt)
$s.Speak($env:AVB_TTS_TEXT)
$s.Dispose()
"""


class TtsError(Exception):
    pass


async def _run(cmd: list[str], env: dict[str, str] | None = None, timeout: float = 60) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError as exc:
        proc.kill()
        raise TtsError(f"{cmd[0]} timed out") from exc
    if proc.returncode != 0:
        raise TtsError(f"{cmd[0]} failed: {err.decode(errors='replace')[:300]}")
    return out


class SapiTTS:
    """Windows built-in voices. ``rate`` is SAPI's -10..10 speed."""

    def __init__(self, voice: str | None = None, rate: int = 0) -> None:
        self.voice = voice
        self.rate = rate

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "speech.wav"
            env = {
                "AVB_TTS_TEXT": text[:MAX_CHARS],
                "AVB_TTS_OUT": str(out),
                "AVB_TTS_RATE": str(self.rate),
                "AVB_TTS_VOICE": self.voice or "",
            }
            shell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
            await _run([shell, "-NoProfile", "-NonInteractive", "-Command", _SAPI_SCRIPT], env)
            pcm, rate = wav_to_pcm16(out.read_bytes())
        return resample(pcm, rate), TELEPHONE_RATE


class EspeakTTS:
    def __init__(self, voice: str = "en-us", words_per_minute: int = 160) -> None:
        self.voice = voice
        self.wpm = words_per_minute
        self.binary = shutil.which("espeak-ng") or shutil.which("espeak")
        if self.binary is None:
            raise TtsError("espeak-ng is not installed")

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        assert self.binary is not None
        # "--stdin" keeps the text off the command line.
        proc = await asyncio.create_subprocess_exec(
            self.binary,
            "-v",
            self.voice,
            "-s",
            str(self.wpm),
            "--stdout",
            "--stdin",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(text[:MAX_CHARS].encode("utf-8")), 60)
        if proc.returncode != 0:
            raise TtsError(f"espeak-ng failed: {err.decode(errors='replace')[:300]}")
        pcm, rate = wav_to_pcm16(out)
        return resample(pcm, rate), TELEPHONE_RATE


def default_tts() -> SapiTTS | EspeakTTS:
    if sys.platform == "win32":
        return SapiTTS()
    return EspeakTTS()


class PiperTTS:
    """Natural-sounding local speech with Piper (free; voices from rhasspy/piper-voices).

    ``voice`` is a voice name such as ``en_US-lessac-medium`` (looked up, and downloaded
    if missing, in ``model_dir``) or a path to an ``.onnx`` voice file.
    """

    def __init__(self, voice: str = "en_US-lessac-medium", model_dir: str | None = None) -> None:
        self.voice_name = voice
        self.model_dir = Path(model_dir or os.environ.get("AVB_MODEL_DIR") or ".") / "piper"
        self._voice: Any = None
        self._lock = asyncio.Lock()

    def _path(self) -> Path:
        if self.voice_name.endswith(".onnx"):
            return Path(self.voice_name)
        return self.model_dir / f"{self.voice_name}.onnx"

    def _load(self) -> Any:
        from piper import PiperVoice

        path = self._path()
        if not path.is_file():
            import subprocess

            self.model_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(  # noqa: S603 - fixed module, voice name from settings
                [
                    sys.executable,
                    "-m",
                    "piper.download_voices",
                    self.voice_name,
                    "--download-dir",
                    str(self.model_dir),
                ],
                check=True,
                timeout=600,
            )
        return PiperVoice.load(path)

    def _run(self, text: str) -> tuple[bytes, int]:
        chunks = list(self._voice.synthesize(text))
        if not chunks:
            return b"", TELEPHONE_RATE
        rate = chunks[0].sample_rate
        return b"".join(c.audio_int16_bytes for c in chunks), rate

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        async with self._lock:
            if self._voice is None:
                self._voice = await asyncio.to_thread(self._load)
            pcm, rate = await asyncio.to_thread(self._run, text[:MAX_CHARS])
        return resample(pcm, rate), TELEPHONE_RATE
