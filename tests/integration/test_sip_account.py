from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_voice_bridge.sip.server import DialError, SipCall
from agent_voice_bridge.sip.uac import AccountConfig, AccountError, SipAccountClient
from tests.support.fake_provider import RECORD_ROUTE, FakeProvider
from tests.support.sip_client import goertzel, make_self_signed, tone


class Recorder:
    def __init__(self) -> None:
        self.calls: list[SipCall] = []
        self.digits: list[str] = []
        self.audio = bytearray()
        self.done = asyncio.Event()
        self.hang_up_after = 10.0

    async def __call__(self, call: SipCall) -> None:
        self.calls.append(call)
        call.rtp.on_audio = self.audio.extend
        call.rtp.play(tone(1000, 1.5))
        deadline = asyncio.get_running_loop().time() + self.hang_up_after
        while not call.ended.is_set() and asyncio.get_running_loop().time() < deadline:
            try:
                self.digits.append(await asyncio.wait_for(call.dtmf.get(), 0.1))
            except TimeoutError:
                pass
        self.done.set()


@pytest.fixture
async def provider(tmp_path: Path) -> AsyncIterator[FakeProvider]:
    cert, key = make_self_signed(tmp_path)
    p = FakeProvider(cert, key)
    await p.start()
    yield p
    await p.stop()


def _config(p: FakeProvider, **kw: object) -> AccountConfig:
    return AccountConfig(
        username="bot",
        password=str(kw.pop("password", p.password)),
        domain="localhost",
        proxy_host="127.0.0.1",
        proxy_port=p.port,
        bind_host="127.0.0.1",
        media_ip="127.0.0.1",
        ca_file=p.cert,
        **kw,  # type: ignore[arg-type]
    )


async def test_register_call_srtp_dtmf_and_hangups(provider: FakeProvider) -> None:
    rec = Recorder()
    client = SipAccountClient(_config(provider), rec)
    await client.start()
    assert provider.registered.is_set() and client.registered.is_set()
    reg = [r for r in provider.received if r.method == "REGISTER"]
    assert "authorization" in reg[-1].headers and ";ob" in (reg[-1].header("contact") or "")

    call = await client.dial("phone", ring_seconds=5)
    invite = provider.invite
    assert invite is not None and invite.uri == "sip:phone@localhost"
    assert b"RTP/SAVP" in invite.body and b"a=crypto:" in invite.body
    await asyncio.sleep(0.1)
    ack = [r for r in provider.received if r.method == "ACK"][-1]
    assert ack.uri == "sip:phone@10.9.8.7:5555;transport=tls"  # the phone's Contact
    assert ack.header("route") == RECORD_ROUTE  # follows the route set

    await provider.press("3")
    await provider.send_audio(tone(440, 0.5))
    await asyncio.sleep(0.3)
    assert rec.digits == ["3"]
    heard = bytes(rec.audio[-3000:])
    assert goertzel(heard, 440) > 20 * goertzel(heard, 1000)
    assert provider.rtp is not None and provider.rtp.rejected == 0
    got = bytes(provider.rtp.received[:8000])
    assert goertzel(got, 1000) > 20 * goertzel(got, 440)

    provider.hang_up()  # phone hangs up
    await asyncio.wait_for(rec.done.wait(), 3)
    assert call.ended.is_set() and not client.calls

    # Second call: the bridge hangs up.
    rec2 = Recorder()
    rec2.hang_up_after = 0.3
    client.on_call = rec2
    await client.dial("sip:phone@localhost", ring_seconds=5)
    await asyncio.wait_for(rec2.done.wait(), 3)
    await asyncio.sleep(0.2)
    bye = [r for r in provider.received if r.method == "BYE"][-1]
    assert bye.header("route") == RECORD_ROUTE
    await client.stop()


async def test_wrong_password(provider: FakeProvider) -> None:
    client = SipAccountClient(_config(provider, password="not-the-password"), Recorder())
    with pytest.raises(AccountError, match="wrong username or password"):
        await client.start(timeout=5)


async def test_declined_and_unanswered(provider: FakeProvider) -> None:
    rec = Recorder()
    client = SipAccountClient(_config(provider), rec)
    await client.start()
    provider.answer_status = 603
    with pytest.raises(DialError, match="603"):
        await client.dial("phone", ring_seconds=5)
    provider.answer_status = 0
    with pytest.raises(DialError, match="487|no answer"):
        await client.dial("phone", ring_seconds=0.5)
    assert "CANCEL" in provider.methods()
    assert rec.calls == [] and not client.calls
    await client.stop()


async def test_plain_answer_refused_when_srtp_required(provider: FakeProvider) -> None:
    provider.plain_answer = True
    client = SipAccountClient(_config(provider), Recorder())
    await client.start()
    with pytest.raises(DialError, match="SRTP"):
        await client.dial("phone", ring_seconds=5)
    await client.stop()


async def test_falls_back_to_avp_when_allowed(provider: FakeProvider) -> None:
    provider.reject_savp = True
    rec = Recorder()
    rec.hang_up_after = 0.3
    client = SipAccountClient(_config(provider, require_srtp=False), rec)
    await client.start()
    await client.dial("phone", ring_seconds=5)
    assert provider.invite is not None and b"RTP/AVP" in provider.invite.body
    assert b"a=crypto:" in provider.invite.body  # still offers SRTP keys
    await asyncio.wait_for(rec.done.wait(), 3)
    assert rec.calls[0].rtp.srtp is not None
    await client.stop()
