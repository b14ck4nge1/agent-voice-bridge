from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest

from agent_voice_bridge.sip.server import SipCall, SipServer, SipServerConfig, is_private_address
from tests.support.sip_client import TestPhone, goertzel, tone

PASSWORD = "correct-horse-battery-staple"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Recorder:
    def __init__(self) -> None:
        self.calls: list[SipCall] = []
        self.audio = bytearray()
        self.digits: list[str] = []
        self.done = asyncio.Event()
        self.hang_up_after: float | None = None

    async def __call__(self, call: SipCall) -> None:
        self.calls.append(call)
        call.rtp.on_audio = self.audio.extend
        call.rtp.play(tone(1000, 1.5))
        deadline = asyncio.get_running_loop().time() + (self.hang_up_after or 10)
        while not call.ended.is_set() and asyncio.get_running_loop().time() < deadline:
            try:
                self.digits.append(await asyncio.wait_for(call.dtmf.get(), 0.1))
            except TimeoutError:
                pass
        self.done.set()


@pytest.fixture
async def server() -> AsyncIterator[tuple[SipServer, Recorder]]:
    rec = Recorder()
    srv = SipServer(
        SipServerConfig(
            bind_host="127.0.0.1", port=_port(), password=PASSWORD, max_auth_failures=3
        ),
        rec,
    )
    await srv.start()
    yield srv, rec
    await srv.stop()


@pytest.mark.parametrize("transport", ["udp", "tcp"])
async def test_register_call_audio_dtmf_and_bye(
    server: tuple[SipServer, Recorder], transport: str
) -> None:
    srv, rec = server
    phone = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD, transport=transport)
    await phone.connect()
    assert await phone.register() == 200
    assert srv.phone_registered

    r = await phone.invite()
    assert r.status == 200
    assert b"RTP/AVP 0 101" in r.body  # PCMU chosen (opus skipped), DTMF negotiated
    await phone.send_audio(tone(440, 1.0))
    for d in "12#":
        await phone.press(d)
    await asyncio.sleep(0.3)

    assert rec.digits == ["1", "2", "#"]
    heard_by_server = bytes(rec.audio[-4000:])
    assert goertzel(heard_by_server, 440) > 20 * goertzel(heard_by_server, 1000)
    assert phone.rtp is not None
    heard_by_phone = bytes(phone.rtp.received[:8000])
    assert goertzel(heard_by_phone, 1000) > 20 * goertzel(heard_by_phone, 440)

    assert await phone.bye() == 200
    await asyncio.wait_for(rec.done.wait(), 3)
    assert rec.calls[0].ended.is_set() and not srv.calls
    phone.close()


async def test_sha256_digest(server: tuple[SipServer, Recorder]) -> None:
    srv, _ = server
    phone = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD)
    await phone.connect()
    assert await phone.register(algorithm="SHA-256") == 200
    phone.close()


async def test_server_hangs_up_with_bye(server: tuple[SipServer, Recorder]) -> None:
    srv, rec = server
    rec.hang_up_after = 0.5
    phone = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD, transport="tcp")
    await phone.connect()
    assert (await phone.invite()).status == 200
    await asyncio.wait_for(rec.done.wait(), 3)
    await asyncio.sleep(0.3)
    assert phone.requests_in == ["BYE"]
    phone.close()


async def test_wrong_password_then_lockout(server: tuple[SipServer, Recorder]) -> None:
    srv, _ = server
    phone = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD)
    await phone.connect()
    for _ in range(3):
        assert await phone.register(password="wrong-password-xyz") == 401
    # Locked out now, even with the right password.
    assert await phone.register() == 403
    phone.close()


async def test_unauthenticated_invite_never_reaches_handler(
    server: tuple[SipServer, Recorder],
) -> None:
    srv, rec = server
    phone = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD)
    await phone.connect()
    r = await phone.invite(password="wrong-password-xyz")
    assert r.status == 401
    assert rec.calls == []
    phone.close()


async def test_srtp_only_offer_is_refused(server: tuple[SipServer, Recorder]) -> None:
    srv, rec = server
    phone = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD)
    await phone.connect()
    r = await phone.invite(sdp=phone.offer(profile="RTP/SAVP", crypto=True))
    assert r.status == 488 and "media encryption" in (r.header("warning") or "")
    assert rec.calls == []
    phone.close()


async def test_optional_srtp_offer_is_answered_with_plain_rtp(
    server: tuple[SipServer, Recorder],
) -> None:
    # Linphone's default for third-party accounts: SRTP preferred but not mandatory.
    srv, _ = server
    phone = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD)
    await phone.connect()
    r = await phone.invite(sdp=phone.offer(crypto=True))
    assert r.status == 200 and b"a=crypto" not in r.body
    await phone.bye()
    phone.close()


async def test_second_call_is_busy(server: tuple[SipServer, Recorder]) -> None:
    srv, _ = server
    a = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD)
    b = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD)
    await a.connect()
    await b.connect()
    assert (await a.invite()).status == 200
    assert (await b.invite()).status == 486
    await a.bye()
    a.close()
    b.close()


def test_weak_password_rejected() -> None:
    with pytest.raises(ValueError):
        SipServer(SipServerConfig(bind_host="127.0.0.1", password="short"), Recorder())


@pytest.mark.parametrize(
    ("host", "private"),
    [
        ("100.101.102.103", True),
        ("192.168.1.5", True),
        ("127.0.0.1", True),
        ("8.8.8.8", False),
        ("nope", False),
    ],
)
def test_private_address(host: str, private: bool) -> None:
    assert is_private_address(host) is private
