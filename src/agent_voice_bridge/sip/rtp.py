"""RTP (RFC 3550) audio session with G.711 and RFC 4733 DTMF.

* Sends 20 ms frames on a drift-corrected clock; silence when nothing is queued.
* Symmetric RTP ("latching"): once a packet arrives, replies go to the address it
  came from. That copes with NAT, and with peers that advertise an address other
  than the one their packets come from.
* Plain RTP only. Use it over a private network (e.g. Tailscale/WireGuard), which
  encrypts the traffic, or on a LAN.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import socket
import struct
import time
from collections.abc import Callable
from typing import Any

from agent_voice_bridge.sip import g711

log = logging.getLogger(__name__)

FRAME_SAMPLES = 160  # 20 ms at 8 kHz
FRAME_BYTES = FRAME_SAMPLES * 2  # PCM16
DTMF_EVENTS = "0123456789*#ABCD"


class RtpSession(asyncio.DatagramProtocol):
    def __init__(
        self,
        *,
        codec: int,
        remote: tuple[str, int],
        telephone_event: int | None,
        on_dtmf: Callable[[str], None] | None = None,
        on_audio: Callable[[bytes], None] | None = None,
        allowed_hosts: set[str] | None = None,
    ) -> None:
        self.codec = codec
        self.remote = remote
        self.telephone_event = telephone_event
        self.on_dtmf = on_dtmf
        self.on_audio = on_audio
        # Only latch to (or accept media from) these IPs: the offered media address and
        # the signalling peer. Stops another host on the network from injecting audio.
        self.allowed_hosts = allowed_hosts or {remote[0]}
        self.transport: asyncio.DatagramTransport | None = None
        self.ssrc = random.getrandbits(32)
        self.seq = random.getrandbits(16)
        self.timestamp = random.getrandbits(32)
        self._out = bytearray()  # PCM16 queued for sending
        self._sender: asyncio.Task[None] | None = None
        self._latched = False
        self._last_event: tuple[int, int] | None = None  # (timestamp, event) of last DTMF reported
        self.packets_in = 0
        self.packets_out = 0
        self._drained = asyncio.Event()
        self._drained.set()

    # -- lifecycle ------------------------------------------------------------

    @classmethod
    async def open(
        cls, bind_host: str, port_range: tuple[int, int], **kwargs: Any
    ) -> tuple[RtpSession, int]:
        loop = asyncio.get_running_loop()
        lo, hi = port_range
        ports = list(range(lo - lo % 2, hi, 2))
        random.shuffle(ports)
        last: OSError | None = None
        for port in ports[:50]:
            try:
                _, proto = await loop.create_datagram_endpoint(
                    lambda: cls(**kwargs), local_addr=(bind_host, port), family=socket.AF_INET
                )
                proto._sender = loop.create_task(proto._send_loop())
                return proto, port
            except OSError as exc:
                last = exc
        raise OSError(f"no free RTP port in {port_range}: {last}")

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def close(self) -> None:
        if self._sender:
            self._sender.cancel()
        if self.transport:
            self.transport.close()
        self._drained.set()

    # -- sending ------------------------------------------------------------------

    def play(self, pcm16: bytes) -> None:
        """Queue 8 kHz PCM16 audio for sending."""
        self._out.extend(pcm16)
        self._drained.clear()

    def stop_playback(self) -> None:
        self._out.clear()
        self._drained.set()

    async def wait_played(self) -> None:
        await self._drained.wait()

    @property
    def playing(self) -> bool:
        return bool(self._out)

    def _packet(self, payload: bytes, pt: int, marker: bool = False) -> bytes:
        header = struct.pack(
            "!BBHII",
            0x80,
            (0x80 if marker else 0) | pt,
            self.seq & 0xFFFF,
            self.timestamp & 0xFFFFFFFF,
            self.ssrc,
        )
        self.seq += 1
        return header + payload

    async def _send_loop(self) -> None:
        start = time.monotonic()
        frame = 0
        silence = b"\x00" * FRAME_BYTES
        while True:
            if self._out:
                chunk = bytes(self._out[:FRAME_BYTES]).ljust(FRAME_BYTES, b"\x00")
                del self._out[:FRAME_BYTES]
                if not self._out:
                    self._drained.set()
            else:
                chunk = silence
            if self.transport is not None:
                self.transport.sendto(
                    self._packet(g711.encode(chunk, self.codec), self.codec), self.remote
                )
                self.packets_out += 1
            self.timestamp += FRAME_SAMPLES
            frame += 1
            delay = start + frame * 0.02 - time.monotonic()
            if delay < -0.2:  # fell far behind (suspended?): resynchronise
                start, frame = time.monotonic(), 0
                delay = 0
            await asyncio.sleep(max(0.0, delay))

    # -- receiving ------------------------------------------------------------------

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 12 or data[0] >> 6 != 2 or addr[0] not in self.allowed_hosts:
            return
        if not self._latched or addr != self.remote:
            if not self._latched:
                log.debug("RTP latched to %s (offered %s)", addr, self.remote)
            self.remote = addr
            self._latched = True
        cc = data[0] & 0x0F
        has_ext = data[0] & 0x10
        pt = data[1] & 0x7F
        ts = struct.unpack("!I", data[4:8])[0]
        offset = 12 + 4 * cc
        if has_ext and len(data) >= offset + 4:
            offset += 4 + 4 * struct.unpack("!H", data[offset + 2 : offset + 4])[0]
        payload = data[offset:]
        if data[0] & 0x20 and payload:  # padding
            payload = payload[: -payload[-1]]
        self.packets_in += 1
        if pt == self.telephone_event and len(payload) >= 4:
            self._dtmf(payload, ts)
        elif pt in (0, 8) and self.on_audio is not None:
            self.on_audio(g711.decode(payload, pt))

    def _dtmf(self, payload: bytes, ts: int) -> None:
        event = payload[0]
        if event >= len(DTMF_EVENTS):
            return
        key = (ts, event)
        # Report each event once, on its first packet; retransmitted end packets repeat it.
        if key == self._last_event:
            return
        self._last_event = key
        if self.on_dtmf is not None:
            with contextlib.suppress(Exception):
                self.on_dtmf(DTMF_EVENTS[event])

    def error_received(self, exc: Exception) -> None:
        log.debug("RTP socket error: %s", exc)
