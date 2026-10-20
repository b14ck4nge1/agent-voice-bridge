"""A deliberately simple SIP user agent (client) for tests: REGISTER, INVITE, ACK, BYE,
RTP audio and RFC 4733 DTMF, over UDP or TCP. It builds messages by hand rather than
reusing the server's serializer, so the two don't share bugs.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import re
import secrets
import struct
from dataclasses import dataclass, field

from agent_voice_bridge.sip import g711


def goertzel(pcm: bytes, freq: float, rate: int = 8000) -> float:
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    k = 2 * math.cos(2 * math.pi * freq / rate)
    s1 = s2 = 0.0
    for x in samples:
        s1, s2 = x + k * s1 - s2, s1
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - k * s1 * s2)) / len(samples)


def tone(freq: float, seconds: float, rate: int = 8000, amp: int = 8000) -> bytes:
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(int(seconds * rate))
    )


@dataclass
class Response:
    status: int
    headers: dict[str, list[str]]
    body: bytes

    def header(self, name: str) -> str | None:
        values = self.headers.get(name.lower())
        return values[0] if values else None


def _parse(raw: bytes) -> Response:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    status = int(lines[0].split()[1])
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers.setdefault(k.strip().lower(), []).append(v.strip())
    return Response(status, headers, body)


class _Rtp(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.received = bytearray()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        pt = data[1] & 0x7F
        if pt in (0, 8):
            self.received.extend(g711.decode(data[12:], pt))


@dataclass
class TestPhone:
    __test__ = False  # not a pytest test class

    host: str
    port: int
    username: str
    password: str
    transport: str = "udp"
    local_ip: str = "127.0.0.1"
    call_id: str = field(default_factory=lambda: secrets.token_hex(8))
    cseq: int = 0
    tag: str = field(default_factory=lambda: secrets.token_hex(4))
    remote_tag: str | None = None
    _udp: asyncio.DatagramTransport | None = None
    _queue: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue)
    _reader: asyncio.StreamReader | None = None
    _writer: asyncio.StreamWriter | None = None
    local_port: int = 0
    rtp: _Rtp | None = None
    rtp_port: int = 0
    remote_media: tuple[str, int] | None = None
    nc: int = 0
    requests_in: list[str] = field(default_factory=list)

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        if self.transport == "udp":
            phone = self

            class P(asyncio.DatagramProtocol):
                def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                    phone._incoming(data)

            self._udp, _ = await loop.create_datagram_endpoint(P, local_addr=(self.local_ip, 0))
            self.local_port = self._udp.get_extra_info("sockname")[1]
        else:
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
            self.local_port = self._writer.get_extra_info("sockname")[1]
            loop.create_task(self._tcp_reader())
        self.rtp_transport, self.rtp = await loop.create_datagram_endpoint(
            _Rtp, local_addr=(self.local_ip, 0)
        )
        self.rtp_port = self.rtp_transport.get_extra_info("sockname")[1]

    async def _tcp_reader(self) -> None:
        assert self._reader is not None
        buf = b""
        while True:
            chunk = await self._reader.read(65536)
            if not chunk:
                return
            buf += chunk
            while b"\r\n\r\n" in buf:
                head, _, rest = buf.partition(b"\r\n\r\n")
                m = re.search(rb"(?i)content-length:\s*(\d+)", head)
                n = int(m.group(1)) if m else 0
                if len(rest) < n:
                    break
                self._incoming(head + b"\r\n\r\n" + rest[:n])
                buf = rest[n:]

    def _incoming(self, data: bytes) -> None:
        if data.startswith(b"SIP/2.0"):
            self._queue.put_nowait(data)
        else:  # a request from the server (BYE)
            first = data.split(b"\r\n", 1)[0].decode()
            self.requests_in.append(first.split()[0])
            self._respond_ok(data)

    def _respond_ok(self, req: bytes) -> None:
        head = req.split(b"\r\n\r\n", 1)[0].decode().split("\r\n")
        keep = [
            line
            for line in head[1:]
            if line.split(":", 1)[0].lower() in ("via", "from", "to", "call-id", "cseq")
        ]
        self._send(
            ("SIP/2.0 200 OK\r\n" + "\r\n".join(keep) + "\r\nContent-Length: 0\r\n\r\n").encode()
        )

    def _send(self, data: bytes) -> None:
        if self._udp is not None:
            self._udp.sendto(data, (self.host, self.port))
        else:
            assert self._writer is not None
            self._writer.write(data)

    def _request(
        self,
        method: str,
        uri: str,
        *,
        auth: str | None = None,
        body: bytes = b"",
        extra: list[str] | None = None,
        to_tag: str | None = None,
        cseq: int | None = None,
    ) -> None:
        if cseq is None:
            self.cseq += 1
            cseq = self.cseq
        t = self.transport.upper()
        to = f"<sip:{'avb' if method != 'REGISTER' else self.username}@{self.host}>"
        if to_tag:
            to += f";tag={to_tag}"
        lines = [
            f"{method} {uri} SIP/2.0",
            f"Via: SIP/2.0/{t} {self.local_ip}:{self.local_port};branch=z9hG4bK{secrets.token_hex(6)};rport",
            "Max-Forwards: 70",
            f"From: <sip:{self.username}@{self.host}>;tag={self.tag}",
            f"To: {to}",
            f"Call-ID: {self.call_id}",
            f"CSeq: {cseq} {method}",
            f"Contact: <sip:{self.username}@{self.local_ip}:{self.local_port};transport={self.transport}>",
            *(extra or []),
        ]
        if auth:
            lines.append(f"Authorization: {auth}")
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body)}")
        self._send(("\r\n".join(lines) + "\r\n\r\n").encode() + body)

    async def response(self, timeout: float = 5) -> Response:
        while True:
            r = _parse(await asyncio.wait_for(self._queue.get(), timeout))
            if r.status >= 200 or r.status == 180:
                return r

    def _auth(
        self,
        challenge: str,
        method: str,
        uri: str,
        password: str | None = None,
        algorithm: str = "MD5",
    ) -> str:
        realm = re.search(r'realm="([^"]+)"', challenge).group(1)  # type: ignore[union-attr]
        nonce = re.search(r'nonce="([^"]+)"', challenge).group(1)  # type: ignore[union-attr]
        h = hashlib.md5 if algorithm == "MD5" else hashlib.sha256
        self.nc += 1
        nc, cnonce = f"{self.nc:08x}", secrets.token_hex(4)
        ha1 = h(f"{self.username}:{realm}:{password or self.password}".encode()).hexdigest()
        ha2 = h(f"{method}:{uri}".encode()).hexdigest()
        resp = h(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        return (
            f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
            f'response="{resp}", algorithm={algorithm}, qop=auth, nc={nc}, cnonce="{cnonce}"'
        )

    def _pick(self, r: Response, algorithm: str) -> str:
        return next(c for c in r.headers["www-authenticate"] if f"algorithm={algorithm}" in c)

    async def register(self, *, password: str | None = None, algorithm: str = "MD5") -> int:
        uri = f"sip:{self.host}:{self.port}"
        self._request("REGISTER", uri, extra=["Expires: 600"])
        r = await self.response()
        if r.status != 401:
            return r.status
        self._request(
            "REGISTER",
            uri,
            extra=["Expires: 600"],
            auth=self._auth(self._pick(r, algorithm), "REGISTER", uri, password, algorithm),
        )
        return (await self.response()).status

    def offer(self, *, profile: str = "RTP/AVP", crypto: bool = False) -> bytes:
        lines = [
            "v=0",
            f"o=test 1 1 IN IP4 {self.local_ip}",
            "s=-",
            f"c=IN IP4 {self.local_ip}",
            "t=0 0",
            f"m=audio {self.rtp_port} {profile} 96 0 8 101",
            "a=rtpmap:96 opus/48000/2",
            "a=rtpmap:101 telephone-event/8000",
            "a=fmtp:101 0-16",
        ]
        if crypto:
            lines.append(
                "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:WVNfX19zZW1jdGwgKCkgewkyMjA7fQp9CnVubGVz"
            )
        lines += ["a=rtcp-mux", "a=sendrecv"]
        return ("\r\n".join(lines) + "\r\n").encode()

    async def invite(self, *, password: str | None = None, sdp: bytes | None = None) -> Response:
        uri = f"sip:avb@{self.host}:{self.port}"
        body = sdp or self.offer()
        self._request("INVITE", uri, body=body)
        r = await self.response()
        invite_cseq = self.cseq
        if r.status == 401:
            self._request("ACK", uri, cseq=invite_cseq)
            self._request(
                "INVITE",
                uri,
                body=body,
                auth=self._auth(self._pick(r, "MD5"), "INVITE", uri, password),
            )
            r = await self.response()
            invite_cseq = self.cseq
        if r.status == 180:
            r = await self.response()
        to = r.header("to") or ""
        m = re.search(r"tag=([^;>\s]+)", to)
        self.remote_tag = m.group(1) if m else None
        self._request("ACK", uri, to_tag=self.remote_tag, cseq=invite_cseq)
        if r.status == 200:
            text = r.body.decode()
            ip = re.search(r"c=IN IP4 (\S+)", text).group(1)  # type: ignore[union-attr]
            port = int(re.search(r"m=audio (\d+)", text).group(1))  # type: ignore[union-attr]
            self.remote_media = (ip, port)
        return r

    async def send_audio(self, pcm: bytes, pt: int = 0) -> None:
        assert self.remote_media is not None
        seq, ts, ssrc = random.getrandbits(16), random.getrandbits(32), random.getrandbits(32)
        for i in range(0, len(pcm), 320):
            payload = g711.encode(pcm[i : i + 320], pt)
            self.rtp_transport.sendto(
                struct.pack("!BBHII", 0x80, pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc) + payload,
                self.remote_media,
            )
            seq, ts = seq + 1, ts + 160
            await asyncio.sleep(0.02)

    async def press(self, digit: str) -> None:
        """RFC 4733: a few event packets with the same timestamp, the last ones with E bit."""
        assert self.remote_media is not None
        event = "0123456789*#".index(digit)
        ts, ssrc = random.getrandbits(32), 0x1234
        for n, end in enumerate([False, False, True, True, True]):
            payload = struct.pack("!BBH", event, (0x80 if end else 0) | 10, 160 * (n + 1))
            pkt = struct.pack("!BBHII", 0x80, (0x80 if n == 0 else 0) | 101, n, ts, ssrc) + payload
            self.rtp_transport.sendto(pkt, self.remote_media)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)

    async def bye(self) -> int:
        self._request("BYE", f"sip:avb@{self.host}:{self.port}", to_tag=self.remote_tag)
        return (await self.response()).status

    def close(self) -> None:
        if self._udp:
            self._udp.close()
        if self._writer:
            self._writer.close()
        self.rtp_transport.close()
