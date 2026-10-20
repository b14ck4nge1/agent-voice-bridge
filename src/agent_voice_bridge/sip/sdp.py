"""Just enough SDP (RFC 4566 / 3264) for one audio stream with G.711 and RFC 4733 DTMF."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field

SUPPORTED = {0: "PCMU", 8: "PCMA"}


class SdpError(ValueError):
    pass


@dataclass
class AudioOffer:
    address: str
    port: int
    profile: str
    payload_types: list[int]
    rtpmap: dict[int, str] = field(default_factory=dict)
    telephone_event: int | None = None
    has_crypto: bool = False
    direction: str = "sendrecv"


def parse_offer(body: bytes) -> AudioOffer:
    text = body.decode("utf-8", "replace")
    session_addr: str | None = None
    media_addr: str | None = None
    audio: AudioOffer | None = None
    in_audio = False
    for line in re.split(r"\r?\n", text):
        if line.startswith("c=") and not in_audio:
            session_addr = line.split()[-1]
        elif line.startswith("m="):
            parts = line[2:].split()
            in_audio = parts[0] == "audio" and audio is None
            if in_audio:
                if len(parts) < 4 or not parts[1].isdigit():
                    raise SdpError("bad m= line")
                pts = [int(p) for p in parts[3:] if p.isdigit()]
                audio = AudioOffer(
                    address="", port=int(parts[1]), profile=parts[2], payload_types=pts
                )
        elif in_audio and audio is not None:
            if line.startswith("c="):
                media_addr = line.split()[-1]
            elif line.startswith("a=rtpmap:"):
                pt, _, enc = line[9:].partition(" ")
                if pt.isdigit():
                    audio.rtpmap[int(pt)] = enc
                    if enc.lower().startswith("telephone-event/8000"):
                        audio.telephone_event = int(pt)
            elif line.startswith("a=crypto:"):
                audio.has_crypto = True
            elif line.strip() in ("a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"):
                audio.direction = line.strip()[2:]
    if audio is None:
        raise SdpError("no audio stream offered")
    address = (media_addr or session_addr or "").split("/")[0]
    if not address:
        raise SdpError("no connection address")
    audio.address = address
    return audio


def choose_codec(offer: AudioOffer) -> int | None:
    """First G.711 codec in the offerer's order of preference (static payload types only)."""
    for pt in offer.payload_types:
        if pt in SUPPORTED and offer.rtpmap.get(pt, SUPPORTED[pt]).upper().startswith(
            SUPPORTED[pt]
        ):
            return pt
    return None


def build_answer(
    *,
    address: str,
    port: int,
    codec: int,
    telephone_event: int | None,
    direction: str = "sendrecv",
    session_id: int | None = None,
) -> bytes:
    sid = session_id or secrets.randbelow(2**31)
    fmts = [str(codec)] + ([str(telephone_event)] if telephone_event is not None else [])
    lines = [
        "v=0",
        f"o=avb {sid} {sid} IN IP4 {address}",
        "s=Agent Voice Bridge",
        f"c=IN IP4 {address}",
        "t=0 0",
        f"m=audio {port} RTP/AVP {' '.join(fmts)}",
        f"a=rtpmap:{codec} {SUPPORTED[codec]}/8000",
    ]
    if telephone_event is not None:
        lines += [
            f"a=rtpmap:{telephone_event} telephone-event/8000",
            f"a=fmtp:{telephone_event} 0-15",
        ]
    lines += ["a=ptime:20", f"a={direction}"]
    return ("\r\n".join(lines) + "\r\n").encode()
