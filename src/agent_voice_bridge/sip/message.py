"""Minimal SIP (RFC 3261) message model, parser and serializer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

COMPACT = {
    "i": "Call-ID",
    "m": "Contact",
    "e": "Content-Encoding",
    "l": "Content-Length",
    "c": "Content-Type",
    "f": "From",
    "s": "Subject",
    "k": "Supported",
    "t": "To",
    "v": "Via",
}
_CANON = {
    "call-id": "Call-ID",
    "cseq": "CSeq",
    "www-authenticate": "WWW-Authenticate",
    "content-length": "Content-Length",
    "content-type": "Content-Type",
    "user-agent": "User-Agent",
}
MAX_MESSAGE = 64 * 1024


class SipParseError(ValueError):
    pass


def canon(name: str) -> str:
    name = name.strip()
    if len(name) == 1 and name.lower() in COMPACT:
        return COMPACT[name.lower()]
    lower = name.lower()
    return _CANON.get(lower, "-".join(p.capitalize() for p in lower.split("-")))


@dataclass
class SipMessage:
    method: str | None = None  # request
    uri: str | None = None
    status: int | None = None  # response
    reason: str = ""
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    @property
    def is_request(self) -> bool:
        return self.method is not None

    def get(self, name: str) -> str | None:
        name = canon(name)
        for k, v in self.headers:
            if k == name:
                return v
        return None

    def get_all(self, name: str) -> list[str]:
        name = canon(name)
        return [v for k, v in self.headers if k == name]

    def set(self, name: str, value: str) -> None:
        name = canon(name)
        self.headers = [(k, v) for k, v in self.headers if k != name]
        self.headers.append((name, value))

    def add(self, name: str, value: str) -> None:
        self.headers.append((canon(name), value))

    @property
    def call_id(self) -> str:
        return self.get("Call-ID") or ""

    @property
    def cseq(self) -> tuple[int, str]:
        raw = (self.get("CSeq") or "0 NONE").split()
        try:
            return int(raw[0]), raw[1].upper()
        except (ValueError, IndexError) as exc:
            raise SipParseError("bad CSeq") from exc

    def to_bytes(self) -> bytes:
        if self.is_request:
            start = f"{self.method} {self.uri} SIP/2.0"
        else:
            start = f"SIP/2.0 {self.status} {self.reason}"
        headers = [(k, v) for k, v in self.headers if k != "Content-Length"]
        lines = [start, *(f"{k}: {v}" for k, v in headers), f"Content-Length: {len(self.body)}"]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + self.body


def parse(data: bytes) -> SipMessage:
    if len(data) > MAX_MESSAGE:
        raise SipParseError("message too large")
    head, sep, body = data.partition(b"\r\n\r\n")
    if not sep:
        head, sep, body = data.partition(b"\n\n")
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SipParseError("headers are not UTF-8") from exc
    lines = re.split(r"\r?\n", text)
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        raise SipParseError("empty message")
    start = lines[0].split(" ", 2)
    msg = SipMessage()
    if start[0] == "SIP/2.0":
        if len(start) < 2 or not start[1].isdigit():
            raise SipParseError("bad status line")
        msg.status = int(start[1])
        msg.reason = start[2] if len(start) > 2 else ""
    else:
        if len(start) != 3 or start[2] != "SIP/2.0":
            raise SipParseError("bad request line")
        msg.method, msg.uri = start[0].upper(), start[1]
    raw: list[list[str]] = []
    for line in lines[1:]:
        if line[:1] in (" ", "\t") and raw:  # header folding
            raw[-1][1] += " " + line.strip()
            continue
        if ":" not in line:
            raise SipParseError(f"bad header line {line[:40]!r}")
        name, value = line.split(":", 1)
        raw.append([canon(name), value.strip()])
        if len(raw) > 200:
            raise SipParseError("too many headers")
    msg.headers = [(k, v) for k, v in raw]
    length = msg.get("Content-Length")
    if length is not None:
        try:
            n = int(length)
        except ValueError as exc:
            raise SipParseError("bad Content-Length") from exc
        body = body[:n]
    msg.body = body
    return msg


def frame_length(buffer: bytes) -> int | None:
    """For stream transports: total length of the first complete message, or None."""
    end = buffer.find(b"\r\n\r\n")
    if end < 0:
        if len(buffer) > MAX_MESSAGE:
            raise SipParseError("headers too large")
        return None
    match = re.search(rb"(?im)^(?:content-length|l)[ \t]*:[ \t]*(\d+)", buffer[:end])
    body_len = int(match.group(1)) if match else 0
    if body_len > MAX_MESSAGE:
        raise SipParseError("body too large")
    total = end + 4 + body_len
    return total if len(buffer) >= total else None


_PARAM = re.compile(r";\s*([^=;\s]+)(?:=(\"[^\"]*\"|[^;]*))?")


def header_params(value: str) -> dict[str, str]:
    """Header parameters (after ``>``, or after the first ``;`` when there are no brackets)."""
    if ">" in value:
        tail = value.split(">", 1)[1]
    else:
        tail = value[value.find(";") :] if ";" in value else ""
    out = {}
    for m in _PARAM.finditer(tail):
        out[m.group(1).lower()] = (m.group(2) or "").strip('"')
    return out


def tag_of(value: str | None) -> str | None:
    return header_params(value).get("tag") if value else None


def uri_of(value: str) -> str:
    """The URI inside a From/To/Contact header value."""
    if "<" in value:
        return value.split("<", 1)[1].split(">", 1)[0]
    return value.split(";", 1)[0].strip()


def user_of(uri: str) -> str | None:
    m = re.match(r"sips?:([^@;>]+)@", uri)
    return m.group(1) if m else None


def response_for(
    req: SipMessage, status: int, reason: str, *, to_tag: str | None = None
) -> SipMessage:
    resp = SipMessage(status=status, reason=reason)
    for name in ("Via", "From", "To", "Call-ID", "CSeq"):
        for value in req.get_all(name):
            if name == "To" and to_tag and tag_of(value) is None:
                value = f"{value};tag={to_tag}"
            resp.add(name, value)
    return resp
