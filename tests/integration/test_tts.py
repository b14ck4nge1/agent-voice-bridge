import shutil
import sys

import pytest

from agent_voice_bridge.voice.tts.engines import EspeakTTS, SapiTTS
from tests.support.sip_client import goertzel

TEXT = "Claude Code needs your permission to run npm install."


def _speechlike(pcm: bytes) -> bool:
    # Non-trivial length and energy in the speech band, at the telephone rate.
    return len(pcm) > 8000 and goertzel(pcm, 500) + goertzel(pcm, 1000) + goertzel(pcm, 250) > 1


@pytest.mark.skipif(
    not (shutil.which("espeak-ng") or shutil.which("espeak")), reason="espeak-ng not installed"
)
async def test_espeak_produces_8khz_speech() -> None:
    pcm, rate = await EspeakTTS().synthesize(TEXT)
    assert rate == 8000 and _speechlike(pcm)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows SAPI only")
async def test_windows_sapi_produces_8khz_speech() -> None:
    pcm, rate = await SapiTTS().synthesize(TEXT + ' It\'s "quoted" & $(not) executed; `really`.')
    assert rate == 8000 and _speechlike(pcm)
