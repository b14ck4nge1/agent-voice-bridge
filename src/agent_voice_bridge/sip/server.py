"""A small SIP registrar and call-answering endpoint (UAS) for one phone.

Scope, on purpose: one SIP account (the iPhone's), digest-authenticated REGISTER
and INVITE, G.711 audio, RFC 4733 DTMF, over UDP/TCP (private networks such as
Tailscale) or TLS with SDES-SRTP (a public server). No proxying or forwarding. Calls normally
come *from* the phone, started by the Bark notification, so the server never has to reach a
sleeping phone. ``dial()`` can also ring the phone directly over the connection it registered
on; on iOS that only works while Linphone is open (or was very recently).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import secrets
import socket
import ssl
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from agent_voice_bridge.sip import sdp
from agent_voice_bridge.sip.digest import DigestAuthenticator, parse_auth
from agent_voice_bridge.sip.message import (
    SipMessage,
    SipParseError,
    frame_length,
    header_params,
    parse,
    response_for,
    tag_of,
    uri_of,
    user_of,
)
from agent_voice_bridge.sip.rtp import RtpSession
from agent_voice_bridge.sip.srtp import SrtpContext, SrtpError, from_answer, make_offer
from agent_voice_bridge.sip.srtp import negotiate as srtp_negotiate

log = logging.getLogger(__name__)

ALLOW = "INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER"
USER_AGENT = "AgentVoiceBridge/0.1"
T1 = 0.5
LOCKOUT_SECONDS = 600


@dataclass(frozen=True)
class SipServerConfig:
    bind_host: str
    password: str
    port: int = 5060
    username: str = "phone"
    realm: str = "agent-voice-bridge"
    media_ip: str | None = (
        None  # address advertised in SDP/Contact; default: the local address facing the peer
    )
    rtp_ports: tuple[int, int] = (40000, 40200)
    udp: bool = True
    tcp: bool = True
    max_auth_failures: int = 10
    # SIP over TLS (needed when the server faces the internet). Certificate files
    # are re-read periodically, so renewals (e.g. by Caddy/Let's Encrypt) apply.
    tls_cert: str | None = None
    tls_key: str | None = None
    tls_port: int = 5061
    require_srtp: bool = False

    def validate(self) -> None:
        if len(self.password) < 16:
            raise ValueError("SIP password must be at least 16 characters")
        if self.bind_host in ("0.0.0.0", "::"):  # noqa: S104
            log.warning("SIP server listens on all interfaces; prefer the Tailscale/LAN address")


Reply = Callable[[bytes], None]


@dataclass
class _Peer:
    kind: str  # "udp" | "tcp"
    addr: tuple[str, int]
    local_ip: str
    send: Reply
    closed: bool = False


class DialError(Exception):
    """A call we placed did not connect."""


@dataclass
class SipCall:
    """An answered call. Audio and DTMF are available once ``on_call`` is invoked."""

    call_id: str
    caller: str
    peer: _Peer
    rtp: RtpSession
    local_tag: str
    remote_from: str  # their From header (becomes To in our requests)
    local_to: str  # our To header with tag (becomes From in our requests)
    remote_contact: str
    rtp_port: int = 0
    answer_sdp: bytes = b""
    dtmf: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    _cseq: int = 1
    _server: SipServer | None = None
    ended_callbacks: list[Callable[[], object]] = field(default_factory=list)

    async def hangup(self) -> None:
        if self.ended.is_set():
            return
        self.ended.set()
        if self._server is not None:
            self._server._send_bye(self)
        self.rtp.close()


class SipServer:
    def __init__(
        self, config: SipServerConfig, on_call: Callable[[SipCall], Awaitable[None]]
    ) -> None:
        config.validate()
        self.config = config
        self.on_call = on_call
        self.auth = DigestAuthenticator(config.realm, config.username, config.password)
        self.registration: tuple[str, float] | None = None  # (contact, expires_at)
        self._reg_peer: _Peer | None = None  # where the phone registered from
        self._client_tx: dict[str, asyncio.Queue[SipMessage]] = {}  # our INVITEs, by Call-ID
        self.calls: dict[str, SipCall] = {}
        self._pending_ack: dict[str, tuple[SipMessage, asyncio.Task[None]]] = {}
        self._recent: dict[tuple[str, int, str], bytes] = {}  # retransmission cache
        self._failures: dict[str, list[float]] = {}
        self._udp: asyncio.DatagramTransport | None = None
        self._tcp: asyncio.Server | None = None
        self._tls: asyncio.Server | None = None
        self._tls_ctx: ssl.SSLContext | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # -- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        host, port = self.config.bind_host, self.config.port
        if self.config.udp:
            server = self

            class _Udp(asyncio.DatagramProtocol):
                def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                    server._on_datagram(data, addr)

            self._udp, _ = await loop.create_datagram_endpoint(_Udp, local_addr=(host, port))
        if self.config.tcp:
            self._tcp = await asyncio.start_server(self._on_tcp, host, port)
        if self.config.tls_cert and self.config.tls_key:
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(self.config.tls_cert, self.config.tls_key)
            self._tls_ctx = ctx

            # On 3.12+ handshake explicitly so failures (wrong name, old TLS, scanners) are
            # logged; 3.11's StreamWriter.start_tls can't act as a server.
            explicit = sys.version_info >= (3, 12)

            async def on_tls(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                if explicit:
                    peer = writer.get_extra_info("peername")
                    try:
                        await asyncio.wait_for(writer.start_tls(ctx), 15)
                    except (ssl.SSLError, OSError, TimeoutError) as exc:
                        log.warning(
                            "TLS handshake with %s failed: %s %s (timeout means the client "
                            "never sent a TLS hello: it may be using UDP/TCP, not TLS)",
                            peer,
                            type(exc).__name__,
                            exc,
                        )
                        writer.close()
                        return
                await self._on_tcp(reader, writer, kind="tls")

            self._tls = await asyncio.start_server(
                on_tls, host, self.config.tls_port, ssl=None if explicit else ctx
            )
            reload = loop.create_task(self._reload_cert())
            self._tasks.add(reload)
        log.info(
            "SIP listening on %s (udp=%s tcp=%s on %s, tls=%s on %s, srtp required=%s)",
            host,
            self.config.udp,
            self.config.tcp,
            port,
            self._tls is not None,
            self.config.tls_port,
            self.config.require_srtp,
        )

    async def _reload_cert(self) -> None:
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                assert self._tls_ctx is not None and self.config.tls_cert and self.config.tls_key
                self._tls_ctx.load_cert_chain(self.config.tls_cert, self.config.tls_key)
            except (OSError, ssl.SSLError) as exc:
                log.warning("could not reload TLS certificate: %s", exc)

    async def stop(self) -> None:
        for call in list(self.calls.values()):
            await call.hangup()
        for task in list(self._tasks):
            task.cancel()
        if self._udp:
            self._udp.close()
        for srv in (self._tcp, self._tls):
            if srv:
                srv.close()
                await srv.wait_closed()

    @property
    def phone_registered(self) -> bool:
        return self.registration is not None and self.registration[1] > time.time()

    # -- transports -----------------------------------------------------------------

    def _local_ip_for(self, remote_ip: str) -> str:
        if self.config.media_ip:
            return self.config.media_ip
        if self.config.bind_host not in ("0.0.0.0", "::", ""):  # noqa: S104
            return self.config.bind_host
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
            try:
                s.connect((remote_ip, 9))
                return str(s.getsockname()[0])
            except OSError:
                return "127.0.0.1"

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        if data.strip() == b"":  # keep-alive (CRLF)
            return
        assert self._udp is not None
        udp = self._udp
        peer = _Peer("udp", addr, self._local_ip_for(addr[0]), lambda b: udp.sendto(b, addr))
        self._dispatch(data, peer)

    async def _on_tcp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, kind: str = "tcp"
    ) -> None:
        addr = writer.get_extra_info("peername")[:2]
        local = writer.get_extra_info("sockname")[0]
        peer = _Peer(kind, addr, self.config.media_ip or local, writer.write)
        log.info("SIP %s connection from %s:%s", kind.upper(), *addr)
        buffer = b""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    buffer = buffer.lstrip(b"\r\n")  # CRLF keep-alives
                    try:
                        n = frame_length(buffer)
                    except SipParseError:
                        log.warning("dropping TCP connection from %s: bad framing", addr)
                        return
                    if n is None:
                        break
                    message, buffer = buffer[:n], buffer[n:]
                    self._dispatch(message, peer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            peer.closed = True
            writer.close()

    # -- dispatch -----------------------------------------------------------------------

    def _dispatch(self, data: bytes, peer: _Peer) -> None:
        try:
            msg = parse(data)
        except SipParseError as exc:
            log.debug("ignoring malformed SIP from %s: %s", peer.addr, exc)
            return
        if not msg.is_request:
            queue = self._client_tx.get(msg.call_id)
            if queue is not None and msg.cseq[1] == "INVITE":
                queue.put_nowait(msg)
            return  # responses to our BYE/CANCEL need no handling
        try:
            seq, method = msg.cseq
        except SipParseError:
            return
        key = (msg.call_id, seq, method)
        if method != "ACK" and key in self._recent:
            peer.send(self._recent[key])  # retransmitted request: repeat our answer
            return
        handler = {
            "REGISTER": self._on_register,
            "INVITE": self._on_invite,
            "ACK": self._on_ack,
            "BYE": self._on_bye,
            "CANCEL": self._on_cancel,
            "OPTIONS": self._on_options,
        }.get(msg.method or "")
        if handler is None:
            self._reply(peer, msg, 501, "Not Implemented")
            return
        handler(msg, peer)

    def _reply(
        self,
        peer: _Peer,
        req: SipMessage,
        status: int,
        reason: str,
        *,
        to_tag: str | None = None,
        extra: list[tuple[str, str]] | None = None,
        body: bytes = b"",
        content_type: str | None = None,
    ) -> bytes:
        resp = response_for(req, status, reason, to_tag=to_tag)
        if peer.kind == "udp":  # RFC 3581: note where the request really came from
            for i, (name, value) in enumerate(resp.headers):
                if name == "Via":
                    resp.headers[i] = (name, _annotate_via(value, peer.addr))
                    break
        resp.add("Server", USER_AGENT)
        for k, v in extra or []:
            resp.add(k, v)
        if body:
            resp.add("Content-Type", content_type or "application/sdp")
            resp.body = body
        raw = resp.to_bytes()
        peer.send(raw)
        if status >= 200 and req.method in ("REGISTER", "INVITE", "BYE"):
            log.info("SIP %s from %s -> %s %s", req.method, peer.addr[0], status, reason)
        if status >= 200:
            seq, method = req.cseq
            self._recent[(req.call_id, seq, method)] = raw
            if len(self._recent) > 500:
                self._recent.pop(next(iter(self._recent)))
        return raw

    # -- authentication ---------------------------------------------------------------------

    def _locked_out(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(ip, []) if now - t < LOCKOUT_SECONDS]
        self._failures[ip] = recent
        return len(recent) >= self.config.max_auth_failures

    def _authorised(self, msg: SipMessage, peer: _Peer) -> bool:
        if self._locked_out(peer.addr[0]):
            self._reply(peer, msg, 403, "Forbidden")
            return False
        header = msg.get("Authorization")
        ok, stale = self.auth.verify(header, msg.method or "")
        if ok:
            self._failures.pop(peer.addr[0], None)
            return True
        if header and not stale:
            self._failures.setdefault(peer.addr[0], []).append(time.time())
            p = parse_auth(header)
            log.warning(
                "SIP auth failure from %s: username=%r realm=%r algorithm=%r (expected %r, %r)",
                peer.addr[0],
                p.get("username"),
                p.get("realm"),
                p.get("algorithm", "MD5"),
                self.config.username,
                self.config.realm,
            )
        elif not header:
            log.info(
                "SIP %s from %s without credentials: sending challenge", msg.method, peer.addr[0]
            )
        self._reply(
            peer,
            msg,
            401,
            "Unauthorized",
            extra=[("WWW-Authenticate", c) for c in self.auth.challenges(stale=stale)],
        )
        return False

    # -- methods -------------------------------------------------------------------------------

    def _on_options(self, msg: SipMessage, peer: _Peer) -> None:
        self._reply(peer, msg, 200, "OK", extra=[("Allow", ALLOW), ("Accept", "application/sdp")])

    def _on_register(self, msg: SipMessage, peer: _Peer) -> None:
        if not self._authorised(msg, peer):
            return
        contact = msg.get("Contact")
        expires_hdr = msg.get("Expires")
        expires = 3600
        if contact and "expires" in header_params(contact):
            expires = int(header_params(contact)["expires"] or 0)
        elif expires_hdr and expires_hdr.isdigit():
            expires = int(expires_hdr)
        expires = min(expires, 3600)
        if expires == 0 or contact in (None, "*"):
            self.registration = None
            self._reply(peer, msg, 200, "OK")
            return
        self.registration = (uri_of(contact), time.time() + expires)
        self._reg_peer = peer
        self._reply(
            peer,
            msg,
            200,
            "OK",
            extra=[
                ("Contact", f"<{uri_of(contact)}>;expires={expires}"),
                ("Expires", str(expires)),
            ],
        )

    def _on_invite(self, msg: SipMessage, peer: _Peer) -> None:
        if tag_of(msg.get("To")):  # re-INVITE inside a dialog: keep the existing session
            call = self.calls.get(msg.call_id)
            if call is None:
                self._reply(peer, msg, 481, "Call/Transaction Does Not Exist")
                return
            answer = call.answer_sdp  # same session, same keys
            self._reply(
                peer,
                msg,
                200,
                "OK",
                to_tag=call.local_tag,
                body=answer,
                extra=[("Contact", self._contact(peer))],
            )
            return
        if not self._authorised(msg, peer):
            return
        if self.calls:
            self._reply(peer, msg, 486, "Busy Here")
            return
        try:
            offer = sdp.parse_offer(msg.body)
        except sdp.SdpError as exc:
            self._reply(
                peer, msg, 488, "Not Acceptable Here", extra=[("Warning", f'399 avb "{exc}"')]
            )
            return
        codec = sdp.choose_codec(offer)
        srtp: tuple[SrtpContext, str] | None = None
        if offer.crypto:
            try:
                srtp = srtp_negotiate(offer.crypto)
            except SrtpError:
                srtp = None
        problem = None
        if codec is None:
            problem = "need PCMU or PCMA"
        elif srtp is None and ("SAVP" in offer.profile or self.config.require_srtp):
            problem = (
                "need SDES-SRTP with AES_CM_128_HMAC_SHA1_80/32 (Linphone: media encryption SRTP)"
            )
        if problem:
            self._reply(
                peer, msg, 488, "Not Acceptable Here", extra=[("Warning", f'399 avb "{problem}"')]
            )
            return
        assert codec is not None
        task = asyncio.get_running_loop().create_task(self._answer(msg, peer, offer, codec, srtp))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _contact(self, peer: _Peer) -> str:
        port = self.config.tls_port if peer.kind == "tls" else self.config.port
        transport = f";transport={peer.kind}" if peer.kind != "udp" else ""
        return f"<sip:avb@{peer.local_ip}:{port}{transport}>"

    async def _answer(
        self,
        msg: SipMessage,
        peer: _Peer,
        offer: sdp.AudioOffer,
        codec: int,
        srtp: tuple[SrtpContext, str] | None = None,
    ) -> None:
        local_tag = secrets.token_hex(6)
        self._reply(peer, msg, 100, "Trying")
        call: SipCall | None = None

        def on_dtmf(digit: str) -> None:
            if call is not None:
                call.dtmf.put_nowait(digit)

        bind = self.config.bind_host
        rtp, rtp_port = await RtpSession.open(
            bind,
            self.config.rtp_ports,
            codec=codec,
            remote=(offer.address, offer.port),
            telephone_event=offer.telephone_event,
            on_dtmf=on_dtmf,
            allowed_hosts={offer.address, peer.addr[0]},
            srtp=srtp[0] if srtp else None,
        )
        caller = user_of(uri_of(msg.get("From") or "")) or "unknown"
        call = SipCall(
            call_id=msg.call_id,
            caller=caller,
            peer=peer,
            rtp=rtp,
            local_tag=local_tag,
            remote_from=msg.get("From") or "",
            local_to=f"{msg.get('To')};tag={local_tag}",
            remote_contact=uri_of(msg.get("Contact") or msg.get("From") or ""),
            rtp_port=rtp_port,
            _server=self,
        )
        self.calls[msg.call_id] = call
        answer = sdp.build_answer(
            address=peer.local_ip,
            port=rtp_port,
            codec=codec,
            telephone_event=offer.telephone_event,
            profile=offer.profile if srtp else "RTP/AVP",
            crypto=srtp[1] if srtp else None,
        )
        call.answer_sdp = answer
        raw = self._reply(
            peer,
            msg,
            200,
            "OK",
            to_tag=local_tag,
            body=answer,
            extra=[("Contact", self._contact(peer)), ("Allow", ALLOW)],
        )
        if peer.kind == "udp":
            retransmit = asyncio.get_running_loop().create_task(
                self._retransmit(raw, peer, msg.call_id)
            )
            self._pending_ack[msg.call_id] = (msg, retransmit)
        else:
            self._start_call(call)

    async def _retransmit(self, raw: bytes, peer: _Peer, call_id: str) -> None:
        interval, waited = T1, 0.0
        while waited < 64 * T1:
            await asyncio.sleep(interval)
            waited += interval
            peer.send(raw)
            interval = min(interval * 2, 4.0)
        log.warning("no ACK for call %s; giving up", call_id)
        self._pending_ack.pop(call_id, None)
        call = self.calls.pop(call_id, None)
        if call is not None:
            await call.hangup()

    def _on_ack(self, msg: SipMessage, peer: _Peer) -> None:
        pending = self._pending_ack.pop(msg.call_id, None)
        if pending is not None:
            pending[1].cancel()
        call = self.calls.get(msg.call_id)
        if call is not None and pending is not None:
            self._start_call(call)

    def _start_call(self, call: SipCall) -> None:
        async def run() -> None:
            try:
                await self.on_call(call)
            except Exception:
                log.exception("call handler failed")
            finally:
                await call.hangup()
                self.calls.pop(call.call_id, None)
                for callback in call.ended_callbacks:
                    callback()

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_bye(self, msg: SipMessage, peer: _Peer) -> None:
        call = self.calls.pop(msg.call_id, None)
        if call is None:
            self._reply(peer, msg, 481, "Call/Transaction Does Not Exist")
            return
        self._reply(peer, msg, 200, "OK")
        call.ended.set()
        call.rtp.close()

    def _on_cancel(self, msg: SipMessage, peer: _Peer) -> None:
        self._reply(peer, msg, 200, "OK")
        call = self.calls.pop(msg.call_id, None)
        if call is not None and msg.call_id in self._pending_ack:
            call.ended.set()
            call.rtp.close()

    # -- calls we place -------------------------------------------------------------------

    def _request(
        self,
        peer: _Peer,
        method: str,
        uri: str,
        *,
        branch: str,
        from_: str,
        to: str,
        call_id: str,
        cseq: int,
        extra: list[tuple[str, str]] | None = None,
        body: bytes = b"",
    ) -> None:
        port = self.config.tls_port if peer.kind == "tls" else self.config.port
        req = SipMessage(method=method, uri=uri)
        req.add("Via", f"SIP/2.0/{peer.kind.upper()} {peer.local_ip}:{port};branch={branch};rport")
        req.add("Max-Forwards", "70")
        req.add("From", from_)
        req.add("To", to)
        req.add("Call-ID", call_id)
        req.add("CSeq", f"{cseq} {method}")
        req.add("User-Agent", USER_AGENT)
        for k, v in extra or []:
            req.add(k, v)
        if body:
            req.add("Content-Type", "application/sdp")
            req.body = body
        peer.send(req.to_bytes())

    async def dial(self, *, ring_seconds: float = 45) -> SipCall:
        """Ring the registered phone directly; return the call once answered.

        The call then runs ``on_call`` like an incoming one. Raises DialError if the
        phone isn't reachable, declines, or doesn't answer within ``ring_seconds``.
        """
        peer = self._reg_peer
        if not self.phone_registered or peer is None or peer.closed:
            raise DialError("the phone is not registered (open Linphone first)")
        if self.calls:
            raise DialError("a call is already in progress")
        assert self.registration is not None
        target = self.registration[0]
        call_id = f"{secrets.token_hex(12)}@avb"
        local_tag = secrets.token_hex(6)
        branch = f"z9hG4bK{secrets.token_hex(8)}"
        from_ = f"<sip:avb@{self.config.realm}>;tag={local_tag}"
        to = f"<sip:{self.config.username}@{self.config.realm}>"
        secure = self.config.require_srtp or peer.kind == "tls"
        suite = "AES_CM_128_HMAC_SHA1_80"
        key, crypto = make_offer(suite) if secure else (b"", None)
        call: SipCall | None = None

        def on_dtmf(digit: str) -> None:
            if call is not None:
                call.dtmf.put_nowait(digit)

        rtp, rtp_port = await RtpSession.open(
            self.config.bind_host,
            self.config.rtp_ports,
            codec=0,
            remote=("0.0.0.0", 0),  # noqa: S104 - set from the answer; nothing is sent before
            telephone_event=101,
            on_dtmf=on_dtmf,
        )
        offer = sdp.build_offer(
            address=peer.local_ip,
            port=rtp_port,
            profile="RTP/SAVP" if secure else "RTP/AVP",
            crypto=crypto,
        )
        queue: asyncio.Queue[SipMessage] = asyncio.Queue()
        self._client_tx[call_id] = queue
        contact = self._contact(peer)

        def send_invite() -> None:
            self._request(
                peer,
                "INVITE",
                target,
                branch=branch,
                from_=from_,
                to=to,
                call_id=call_id,
                cseq=1,
                extra=[("Contact", contact), ("Allow", ALLOW)],
                body=offer,
            )

        def ack(resp: SipMessage) -> None:
            ok = resp.status == 200
            self._request(
                peer,
                "ACK",
                uri_of(resp.get("Contact") or "") or target if ok else target,
                # A 2xx ACK is its own transaction; a non-2xx ACK reuses the INVITE branch.
                branch=f"z9hG4bK{secrets.token_hex(8)}" if ok else branch,
                from_=from_,
                to=resp.get("To") or to,
                call_id=call_id,
                cseq=1,
            )

        log.info("SIP calling the phone at %s over %s", target, peer.kind.upper())
        send_invite()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ring_seconds
        resend, interval = loop.time() + T1, T1
        provisional = False
        try:
            while True:
                now = loop.time()
                if now >= deadline:
                    self._request(
                        peer,
                        "CANCEL",
                        target,
                        branch=branch,
                        from_=from_,
                        to=to,
                        call_id=call_id,
                        cseq=1,
                    )
                    raise DialError(f"no answer within {ring_seconds:.0f}s")
                wait = deadline - now
                if peer.kind == "udp" and not provisional:
                    wait = min(wait, max(0.0, resend - now))
                try:
                    resp = await asyncio.wait_for(queue.get(), wait)
                except TimeoutError:
                    if peer.kind == "udp" and not provisional and loop.time() >= resend:
                        send_invite()  # UDP: retransmit until the phone answers at all
                        interval = min(interval * 2, 4.0)
                        resend = loop.time() + interval
                    continue
                status = resp.status or 0
                if status < 200:
                    provisional = True
                    if status == 180:
                        log.info("SIP phone is ringing")
                    continue
                ack(resp)
                if status >= 300:
                    raise DialError(f"phone answered {status} {resp.reason}")
                break
            answer = sdp.parse_offer(resp.body)
            codec = sdp.choose_codec(answer)
            if codec is None:
                raise DialError("phone answered without PCMU/PCMA")
            if secure:
                try:
                    rtp.srtp = from_answer(key, suite, answer.crypto)
                except SrtpError as exc:
                    raise DialError(str(exc)) from exc
            rtp.codec = codec
            rtp.telephone_event = answer.telephone_event
            rtp.allowed_hosts = {answer.address, peer.addr[0]}
            rtp.remote = (answer.address, answer.port)
        except BaseException:
            rtp.close()
            self._client_tx.pop(call_id, None)
            raise
        call = SipCall(
            call_id=call_id,
            caller=self.config.username,
            peer=peer,
            rtp=rtp,
            local_tag=local_tag,
            remote_from=resp.get("To") or to,
            local_to=from_,
            remote_contact=uri_of(resp.get("Contact") or "") or target,
            rtp_port=rtp_port,
            answer_sdp=offer,
            _server=self,
        )
        self.calls[call_id] = call
        log.info("SIP phone answered our call")

        async def re_ack() -> None:  # the 200 is retransmitted if our ACK was lost
            with contextlib.suppress(asyncio.CancelledError):
                while not call.ended.is_set():
                    r = await queue.get()
                    if r.status == 200:
                        ack(r)

        reack = loop.create_task(re_ack())
        call.ended_callbacks.append(reack.cancel)
        call.ended_callbacks.append(lambda: self._client_tx.pop(call_id, None))
        self._start_call(call)
        return call

    def _send_bye(self, call: SipCall) -> None:
        if self.calls.get(call.call_id) is call:
            self.calls.pop(call.call_id, None)
        call._cseq += 1
        peer = call.peer
        transport = peer.kind.upper()
        bye = SipMessage(method="BYE", uri=call.remote_contact)
        port = self.config.tls_port if peer.kind == "tls" else self.config.port
        bye.add(
            "Via",
            f"SIP/2.0/{transport} {peer.local_ip}:{port};branch=z9hG4bK{secrets.token_hex(8)};rport",
        )
        bye.add("Max-Forwards", "70")
        bye.add("From", call.local_to)
        bye.add("To", call.remote_from)
        bye.add("Call-ID", call.call_id)
        bye.add("CSeq", f"{call._cseq} BYE")
        bye.add("User-Agent", USER_AGENT)
        with contextlib.suppress(Exception):
            peer.send(bye.to_bytes())


def _annotate_via(via: str, addr: tuple[str, int]) -> str:
    params = header_params(via)
    out = via
    if "rport" in params:
        out = (
            out.replace(f";rport={params['rport']}", "")
            if params["rport"]
            else out.replace(";rport", "")
        )
        out += f";rport={addr[1]}"
    if "received" not in params:
        out += f";received={addr[0]}"
    return out


def is_private_address(host: str) -> bool:
    """True for loopback, RFC 1918, link-local and CGNAT (Tailscale uses 100.64.0.0/10)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_unspecified:  # 0.0.0.0 / :: listen on every interface, public ones included
        return False
    return ip.is_private or ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")
