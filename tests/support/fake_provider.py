"""A fake SIP provider for tests: a TLS registrar/proxy and the called phone in one.

It challenges REGISTER (401) and INVITE (407) like sip.linphone.org, then answers the
call itself as the "phone": 180, 200 with an SDP answer (SRTP unless told otherwise)
and a Record-Route, RTP audio and RFC 4733 DTMF. Messages are built by hand.
"""

from __future__ import annotations

import asyncio
import base64
import os
import random
import re
import secrets
import ssl
import struct
from dataclasses import dataclass, field

from agent_voice_bridge.sip import g711
from agent_voice_bridge.sip.digest import DigestAuthenticator
from tests.support.sip_client import _Rtp

RECORD_ROUTE = "<sip:127.0.0.1;transport=tls;lr>"


@dataclass
class Received:
    method: str
    uri: str
    headers: dict[str, list[str]]
    body: bytes

    def header(self, name: str) -> str | None:
        values = self.headers.get(name.lower())
        return values[0] if values else None


def _parse(raw: bytes) -> tuple[str, Received | None]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers.setdefault(k.strip().lower(), []).append(v.strip())
    first = lines[0].split()
    if first[0] == "SIP/2.0":
        return first[1], None
    return first[0], Received(first[0], first[1], headers, body)


@dataclass
class FakeProvider:
    cert: str
    key: str
    username: str = "bot"
    password: str = "bot-password-123456"
    answer_status: int = 200  # 0 = never answer
    plain_answer: bool = False  # answer without a=crypto
    reject_savp: bool = False  # 488 to RTP/SAVP offers
    received: list[Received] = field(default_factory=list)
    port: int = 0
    rtp: _Rtp | None = None
    outbound: object = None
    remote_media: tuple[str, int] | None = None
    _writer: asyncio.StreamWriter | None = None
    _server: asyncio.Server | None = None
    invite: Received | None = None
    tag: str = field(default_factory=lambda: secrets.token_hex(4))
    registered: asyncio.Event = field(default_factory=asyncio.Event)
    answered: asyncio.Event = field(default_factory=asyncio.Event)
    _seq: int = 0

    def __post_init__(self) -> None:
        self.auth = DigestAuthenticator("localhost", self.username, self.password)
        self.auth.challenge_algorithms = ("SHA-256", "MD5")

    async def start(self) -> None:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.cert, self.key)
        self._server = await asyncio.start_server(self._on_conn, "127.0.0.1", 0, ssl=ctx)
        self.port = self._server.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        self.rtp_transport, self.rtp = await loop.create_datagram_endpoint(
            _Rtp, local_addr=("127.0.0.1", 0)
        )
        self.rtp_port = self.rtp_transport.get_extra_info("sockname")[1]

    async def stop(self) -> None:
        if self._writer:
            self._writer.close()
        if self._server:
            self._server.close()
        self.rtp_transport.close()

    def methods(self) -> list[str]:
        return [r.method for r in self.received]

    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        buf = b""
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                buf = buf.lstrip(b"\r\n")
                if b"\r\n\r\n" not in buf:
                    break
                head, _, rest = buf.partition(b"\r\n\r\n")
                m = re.search(rb"(?i)content-length:\s*(\d+)", head)
                n = int(m.group(1)) if m else 0
                if len(rest) < n:
                    break
                self._incoming(head + b"\r\n\r\n" + rest[:n])
                buf = rest[n:]

    def _send(self, data: bytes) -> None:
        assert self._writer is not None
        self._writer.write(data)

    def _respond(
        self,
        req: Received,
        status: str,
        *,
        extra: list[str] | None = None,
        body: bytes = b"",
        to_tag: str | None = None,
    ) -> None:
        lines = [f"SIP/2.0 {status}"]
        for name in ("via", "from", "to", "call-id", "cseq"):
            for v in req.headers.get(name, []):
                if name == "to" and to_tag and "tag=" not in v:
                    v += f";tag={to_tag}"
                lines.append(f"{name.title()}: {v}")
        lines += [*(extra or []), f"Content-Length: {len(body)}"]
        self._send(("\r\n".join(lines) + "\r\n\r\n").encode() + body)

    def _incoming(self, raw: bytes) -> None:
        kind, req = _parse(raw)
        if req is None:
            return  # responses (to our BYE)
        self.received.append(req)
        if req.method == "REGISTER":
            ok, _ = self.auth.verify(req.header("authorization"), "REGISTER")
            if not ok:
                self._respond(
                    req,
                    "401 Unauthorized",
                    extra=[f"WWW-Authenticate: {c}" for c in self.auth.challenges()],
                )
                return
            self._respond(req, "200 OK", extra=[f"Contact: {req.header('contact')};expires=600"])
            self.registered.set()
        elif req.method == "INVITE":
            ok, _ = self.auth.verify(req.header("proxy-authorization"), "INVITE")
            if not ok:
                self._respond(
                    req,
                    "407 Proxy Authentication Required",
                    extra=[f"Proxy-Authenticate: {c}" for c in self.auth.challenges()],
                    to_tag="p1",
                )
                return
            self._respond(req, "100 Trying")
            self.invite = req
            if b"RTP/SAVP" in req.body and self.reject_savp:
                self._respond(req, "488 Not Acceptable Here", to_tag=self.tag)
                return
            self._respond(req, "180 Ringing", to_tag=self.tag)
            if self.answer_status == 0:
                return
            if self.answer_status != 200:
                self._respond(req, f"{self.answer_status} Declined", to_tag=self.tag)
                return
            self._answer(req)
        elif req.method == "CANCEL":
            self._respond(req, "200 OK")
            assert self.invite is not None
            self._respond(self.invite, "487 Request Terminated", to_tag=self.tag)
        elif req.method == "BYE":
            self._respond(req, "200 OK")
        elif req.method == "ACK":
            pass

    def _answer(self, req: Received) -> None:
        text = req.body.decode()
        ip = re.search(r"c=IN IP4 (\S+)", text).group(1)  # type: ignore[union-attr]
        port = int(re.search(r"m=audio (\d+)", text).group(1))  # type: ignore[union-attr]
        self.remote_media = (ip, port)
        profile = re.search(r"m=audio \d+ (\S+)", text).group(1)  # type: ignore[union-attr]
        crypto: list[str] = []
        m = re.search(r"a=crypto:(\S+) (\S+) inline:([A-Za-z0-9+/=]+)", text)
        if m and not self.plain_answer:
            from pylibsrtp import Policy, Session

            mine = os.urandom(30)
            prof = Policy.SRTP_PROFILE_AES128_CM_SHA1_80
            self.outbound = Session(
                Policy(key=mine, ssrc_type=Policy.SSRC_ANY_OUTBOUND, srtp_profile=prof)
            )
            assert self.rtp is not None
            self.rtp.inbound = Session(
                Policy(
                    key=base64.b64decode(m.group(3)),
                    ssrc_type=Policy.SSRC_ANY_INBOUND,
                    srtp_profile=prof,
                )
            )
            crypto = [
                f"a=crypto:{m.group(1)} {m.group(2)} inline:{base64.b64encode(mine).decode()}"
            ]
        elif self.plain_answer:
            profile = "RTP/AVP"
        sdp = "\r\n".join(
            [
                "v=0",
                "o=phone 1 1 IN IP4 127.0.0.1",
                "s=-",
                "c=IN IP4 127.0.0.1",
                "t=0 0",
                f"m=audio {self.rtp_port} {profile} 0 101",
                "a=rtpmap:101 telephone-event/8000",
                *crypto,
                "a=sendrecv",
                "",
            ]
        ).encode()
        self._respond(
            req,
            "200 OK",
            to_tag=self.tag,
            extra=[
                f"Record-Route: {RECORD_ROUTE}",
                "Contact: <sip:phone@10.9.8.7:5555;transport=tls>",
                "Content-Type: application/sdp",
            ],
            body=sdp,
        )
        self.answered.set()

    def _protect(self, packet: bytes) -> bytes:
        return bytes(self.outbound.protect(packet)) if self.outbound is not None else packet  # type: ignore[attr-defined]

    async def press(self, digit: str) -> None:
        assert self.remote_media is not None
        event = "0123456789*#".index(digit)
        ts, ssrc = random.getrandbits(32), 0x4321
        for n, end in enumerate([False, False, True, True, True]):
            payload = struct.pack("!BBH", event, (0x80 if end else 0) | 10, 160 * (n + 1))
            self._seq += 1  # SRTP refuses to protect a repeated sequence number
            pkt = (
                struct.pack("!BBHII", 0x80, (0x80 if n == 0 else 0) | 101, self._seq, ts, ssrc)
                + payload
            )
            self.rtp_transport.sendto(self._protect(pkt), self.remote_media)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)

    async def send_audio(self, pcm: bytes) -> None:
        assert self.remote_media is not None
        seq, ts, ssrc = random.getrandbits(16), random.getrandbits(32), 0x5555
        for i in range(0, len(pcm), 320):
            pkt = struct.pack("!BBHII", 0x80, 0, seq & 0xFFFF, ts, ssrc) + g711.encode(
                pcm[i : i + 320], 0
            )
            self.rtp_transport.sendto(self._protect(pkt), self.remote_media)
            seq, ts = seq + 1, ts + 160
            await asyncio.sleep(0.02)

    def hang_up(self) -> None:
        """The phone sends BYE (as the proxy would forward it)."""
        assert self.invite is not None
        inv = self.invite
        lines = [
            f"BYE {inv.header('contact').strip('<>').split(';')[0]} SIP/2.0",  # type: ignore[union-attr]
            f"Via: SIP/2.0/TLS 127.0.0.1:{self.port};branch=z9hG4bK{secrets.token_hex(6)}",
            f"From: {inv.header('to')};tag={self.tag}",
            f"To: {inv.header('from')}",
            f"Call-ID: {inv.header('call-id')}",
            "CSeq: 1 BYE",
            "Content-Length: 0",
        ]
        self._send(("\r\n".join(lines) + "\r\n\r\n").encode())
