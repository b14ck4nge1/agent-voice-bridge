"""The bark-sip phone path end to end, with real processes and sockets:

avb-hook (subprocess) -> bridge (uvicorn) -> Bark push (captured) -> test phone dials the
bridge's SIP server -> spoken prompts over RTP -> keypad digits -> decision -> hook output.

Speech is synthesised by a fake TTS (tones) so the test is fast and portable; real
TTS engines are tested separately.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import httpx

from agent_voice_bridge.notify.bark import BarkConfig, BarkNotifier, decrypt_gcm
from agent_voice_bridge.phone import PhoneStack
from agent_voice_bridge.phone_config import PhoneSettings
from agent_voice_bridge.security import totp
from agent_voice_bridge.sip.server import SipServerConfig
from agent_voice_bridge.voice.audio import silence
from tests.conftest import TOTP_SECRET, run_bridge
from tests.end_to_end.test_hook_e2e import claude_input, decision, finish, run_hook
from tests.support.sip_client import TestPhone, goertzel, tone

SIP_PASSWORD = "phone-password-0123456789"
BARK_KEY = "0123456789abcdef0123456789abcdef"


class ToneTTS:
    """Stands in for speech: 0.3 s of 700 Hz per utterance."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        self.spoken.append(text)
        return tone(700, 0.3) + silence(0.05), 8000


def _port() -> int:
    """A port free for both TCP and UDP (on Windows, some UDP ports are reserved)."""
    for _ in range(50):
        with socket.socket() as t, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            t.bind(("127.0.0.1", 0))
            port = int(t.getsockname()[1])
            try:
                u.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port")


def _stack(pushes: list[dict[str, Any]]) -> tuple[PhoneStack, ToneTTS]:
    def handler(request: httpx.Request) -> httpx.Response:
        pushes.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 200, "message": "success"})

    port = _port()
    settings = PhoneSettings(
        bark=BarkConfig(device_key="dev-key", encryption_key=BARK_KEY),
        sip=SipServerConfig(bind_host="127.0.0.1", port=port, password=SIP_PASSWORD),
        public_host="127.0.0.1",
        transport="udp",
        tts_voice=None,
        tts_rate=0,
    )
    tts = ToneTTS()
    notifier = BarkNotifier(
        settings.bark, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return PhoneStack(settings, tts=tts, notifier=notifier), tts


async def _wait_for(predicate: Any, timeout: float = 10) -> None:
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def _dial(port: int) -> TestPhone:
    phone = TestPhone("127.0.0.1", port, "phone", SIP_PASSWORD)
    await phone.connect()
    assert await phone.register() == 200
    assert (await phone.invite()).status == 200
    return phone


async def test_bark_wake_then_keypad_approval(settings: Any, tmp_path: Path) -> None:
    pushes: list[dict[str, Any]] = []
    stack, tts = _stack(pushes)
    with run_bridge(settings, stack.dispatcher, stack.run) as url:
        proc = await run_hook(url, claude_input("sess-phone", "npm install"), tmp=tmp_path)
        await _wait_for(lambda: pushes)

        # The Bark push is encrypted and its tap action dials the bridge.
        push = pushes[0]
        assert set(push) == {"device_key", "ciphertext", "iv"}
        inner = json.loads(decrypt_gcm(BARK_KEY, push["ciphertext"], push["iv"]))
        assert inner["url"] == f"linphone-sip:avb@127.0.0.1:{stack.settings.sip.port}"
        assert inner["level"] == "critical" and inner["call"] == "1"
        assert "npm install" not in json.dumps(inner)  # the command never goes in a push

        phone = await _dial(stack.settings.sip.port)
        await _wait_for(lambda: any("npm install" in s for s in tts.spoken))
        assert any("Press 1 to approve" in s for s in tts.spoken)
        # The prompts (700 Hz stand-in speech) reached the phone; check the whole call so
        # far rather than a short tail, which may fall in the silence between prompts.
        assert phone.rtp is not None
        await _wait_for(lambda: goertzel(bytes(phone.rtp.received), 700) > 50)  # type: ignore[union-attr]

        await phone.press("3")  # details
        await _wait_for(lambda: any(s.startswith("Exactly:") for s in tts.spoken))
        await phone.press("1")  # approve
        await _wait_for(lambda: any(s.startswith("Please confirm: approve") for s in tts.spoken))
        await phone.press("1")  # yes

        out, _ = await finish(proc)
        assert decision(out) == {"behavior": "allow"}
        await _wait_for(lambda: "BYE" in phone.requests_in)  # the bridge hangs up when done
        phone.close()


async def test_high_risk_needs_keypad_code(settings: Any, tmp_path: Path) -> None:
    pushes: list[dict[str, Any]] = []
    stack, tts = _stack(pushes)
    with run_bridge(settings, stack.dispatcher, stack.run) as url:
        proc = await run_hook(
            url, claude_input("sess-risky", "git push --force origin main"), tmp=tmp_path
        )
        await _wait_for(lambda: pushes)
        phone = await _dial(stack.settings.sip.port)
        await _wait_for(lambda: any("authenticator" in s for s in tts.spoken))
        for d in "11":  # approve, yes
            await phone.press(d)
            await asyncio.sleep(0.4)
        await _wait_for(lambda: any("on the keypad" in s for s in tts.spoken))
        for d in totp.code_at(TOTP_SECRET, totp.current_counter()):
            await phone.press(d)
        out, _ = await finish(proc)
        assert decision(out) == {"behavior": "allow"}
        phone.close()


async def test_hanging_up_decides_nothing(settings: Any, tmp_path: Path) -> None:
    pushes: list[dict[str, Any]] = []
    stack, tts = _stack(pushes)
    with run_bridge(settings, stack.dispatcher, stack.run) as url:
        proc = await run_hook(
            url, claude_input("sess-hang", "ls"), extra=["--wait-seconds", "6"], tmp=tmp_path
        )
        await _wait_for(lambda: pushes)
        phone = await _dial(stack.settings.sip.port)
        await _wait_for(lambda: len(tts.spoken) >= 2)
        await phone.press("1")  # approve...
        await asyncio.sleep(0.4)
        assert await phone.bye() == 200  # ...but hang up before confirming
        out, _ = await finish(proc)
        assert decision(out)["behavior"] == "deny"
        phone.close()


async def test_call_with_nothing_pending(settings: Any) -> None:
    pushes: list[dict[str, Any]] = []
    stack, tts = _stack(pushes)
    with run_bridge(settings, stack.dispatcher, stack.run):
        phone = await _dial(stack.settings.sip.port)
        await _wait_for(lambda: "BYE" in phone.requests_in)
        assert "Nothing is waiting" in tts.spoken[0]
        phone.close()
