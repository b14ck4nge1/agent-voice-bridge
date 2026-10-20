from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_voice_bridge.sip.server import (
    DialError,
    SipCall,
    SipServer,
    SipServerConfig,
    is_private_address,
)
from tests.support.sip_client import TestPhone, goertzel, make_self_signed, tone

PASSWORD = "correct-horse-battery-staple"


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
    srv.auth.challenge_algorithms = ("MD5", "SHA-256")  # opt-in; default offers MD5 only
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


async def test_unsupported_srtp_suite_is_refused(server: tuple[SipServer, Recorder]) -> None:
    srv, rec = server
    phone = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD)
    await phone.connect()
    r = await phone.invite(sdp=phone.offer(profile="RTP/SAVP", crypto="AEAD_AES_256_GCM"))
    assert r.status == 488 and "SRTP" in (r.header("warning") or "")
    assert rec.calls == []
    phone.close()


async def test_srtp_call_round_trip(server: tuple[SipServer, Recorder]) -> None:
    # What Linphone sends with media encryption = SRTP (mandatory): RTP/SAVP + a=crypto.
    srv, rec = server
    phone = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD)
    await phone.connect()
    r = await phone.invite(sdp=phone.offer(profile="RTP/SAVP", crypto=True))
    assert r.status == 200
    assert b"RTP/SAVP 0 101" in r.body and b"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:" in r.body
    await phone.send_audio(tone(440, 1.0))
    await phone.press("7")
    await asyncio.sleep(0.3)
    assert rec.digits == ["7"]
    heard = bytes(rec.audio[-4000:])
    assert goertzel(heard, 440) > 20 * goertzel(heard, 1000)
    assert phone.rtp is not None and phone.rtp.rejected == 0
    assert goertzel(bytes(phone.rtp.received[:8000]), 1000) > 20 * goertzel(
        bytes(phone.rtp.received[:8000]), 440
    )
    await phone.bye()
    phone.close()


async def test_forged_rtp_is_dropped_under_srtp(server: tuple[SipServer, Recorder]) -> None:
    srv, rec = server
    phone = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD)
    await phone.connect()
    assert (await phone.invite(sdp=phone.offer(crypto=True))).status == 200
    phone.outbound = None  # send plain RTP: no valid SRTP auth tag
    await phone.send_audio(tone(440, 0.5))
    await phone.press("1")
    await asyncio.sleep(0.3)
    assert rec.digits == [] and len(rec.audio) == 0
    await phone.bye()
    phone.close()


async def test_plain_rtp_refused_when_srtp_required() -> None:
    rec = Recorder()
    srv = SipServer(
        SipServerConfig(bind_host="127.0.0.1", port=_port(), password=PASSWORD, require_srtp=True),
        rec,
    )
    await srv.start()
    phone = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD)
    await phone.connect()
    r = await phone.invite()
    assert r.status == 488 and "SRTP" in (r.header("warning") or "")
    phone.close()
    await srv.stop()


async def test_tls_transport_with_srtp(tmp_path: Path) -> None:
    cert, key = make_self_signed(tmp_path)
    rec = Recorder()
    srv = SipServer(
        SipServerConfig(
            bind_host="127.0.0.1",
            port=_port(),
            tls_port=_port(),
            password=PASSWORD,
            udp=False,
            tcp=False,
            tls_cert=cert,
            tls_key=key,
            require_srtp=True,
        ),
        rec,
    )
    await srv.start()
    phone = TestPhone(
        "127.0.0.1", srv.config.tls_port, "phone", PASSWORD, transport="tls", tls_ca=cert
    )
    await phone.connect()
    assert await phone.register() == 200
    r = await phone.invite(sdp=phone.offer(profile="RTP/SAVP", crypto=True))
    assert r.status == 200 and ";transport=tls" in (r.header("contact") or "")
    await phone.press("5")
    await asyncio.sleep(0.3)
    assert rec.digits == ["5"]
    assert await phone.bye() == 200
    phone.close()
    await srv.stop()


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
        ("0.0.0.0", False),
        ("nope", False),
    ],
)
def test_private_address(host: str, private: bool) -> None:
    assert is_private_address(host) is private


@pytest.mark.parametrize("transport", ["udp", "tls"])
async def test_server_dials_registered_phone(tmp_path: Path, transport: str) -> None:
    cert, key = make_self_signed(tmp_path)
    rec = Recorder()
    tls = transport == "tls"
    srv = SipServer(
        SipServerConfig(
            bind_host="127.0.0.1",
            port=_port(),
            tls_port=_port(),
            password=PASSWORD,
            udp=not tls,
            tcp=False,
            tls_cert=cert if tls else None,
            tls_key=key if tls else None,
            require_srtp=tls,
        ),
        rec,
    )
    await srv.start()
    port = srv.config.tls_port if tls else srv.config.port
    phone = TestPhone("127.0.0.1", port, "phone", PASSWORD, transport=transport, tls_ca=cert)
    await phone.connect()
    with pytest.raises(DialError, match="not registered"):
        await srv.dial()
    assert await phone.register() == 200

    call = await srv.dial(ring_seconds=5)
    await asyncio.sleep(0.1)
    assert phone.requests_in.count("INVITE") >= 1 and "ACK" in phone.requests_in
    assert (phone.srtp_key is not None) is tls  # SRTP offered over TLS
    await phone.press("4")
    await phone.send_audio(tone(440, 0.5))
    await asyncio.sleep(0.3)
    assert rec.digits == ["4"]
    assert phone.rtp is not None and phone.rtp.rejected == 0
    heard = bytes(phone.rtp.received[:8000])
    assert goertzel(heard, 1000) > 20 * goertzel(heard, 440)
    assert await phone.bye() == 200
    await asyncio.wait_for(rec.done.wait(), 3)
    assert call.ended.is_set() and not srv.calls
    phone.close()
    await srv.stop()


async def test_dial_declined(server: tuple[SipServer, Recorder]) -> None:
    srv, rec = server
    phone = TestPhone("127.0.0.1", srv.config.port, "phone", PASSWORD, answer_status=486)
    await phone.connect()
    assert await phone.register() == 200
    with pytest.raises(DialError, match="486"):
        await srv.dial(ring_seconds=3)
    assert rec.calls == [] and not srv.calls
    phone.close()
