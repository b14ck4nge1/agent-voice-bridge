"""SIP account client (UAC): sign in to a SIP provider and call a phone through it.

Used for the "linphone" path: the bridge signs in to sip.linphone.org with its own
free account and calls your Linphone account. Linphone's push service (Flexisip)
wakes the iPhone, so the call rings even when the phone is locked, with no Bark.

Scope: one TLS connection to the provider's proxy, digest auth (401 and 407),
REGISTER with refresh and reconnect, outgoing INVITE with SDES-SRTP, the dialog's
route set (Record-Route), CANCEL, BYE in both directions. No ICE: the server must
have a public address (or AVB_SIP_MEDIA_IP), and the phone's media is latched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import ssl
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agent_voice_bridge.sip import sdp
from agent_voice_bridge.sip.digest import compute_response
from agent_voice_bridge.sip.message import (
    SipMessage,
    SipParseError,
    frame_length,
    parse,
    response_for,
    tag_of,
    uri_of,
)
from agent_voice_bridge.sip.rtp import RtpSession
from agent_voice_bridge.sip.server import ALLOW, USER_AGENT, DialError, SipCall, _Peer
from agent_voice_bridge.sip.srtp import SrtpError, from_answer, make_offer

log = logging.getLogger(__name__)

SUITE = "AES_CM_128_HMAC_SHA1_80"
KEEPALIVE_SECONDS = 30


class AccountError(Exception):
    """Signing in to the SIP provider failed."""


@dataclass(frozen=True)
class AccountConfig:
    username: str
    password: str
    domain: str = "sip.linphone.org"
    proxy_host: str | None = None  # default: the domain
    proxy_port: int = 5061
    auth_username: str | None = None  # default: the username
    media_ip: str | None = None  # public address for SDP (default: our local address)
    bind_host: str = "0.0.0.0"  # noqa: S104 - RTP sockets; media is SRTP-authenticated
    rtp_ports: tuple[int, int] = (40000, 40200)
    require_srtp: bool = True
    register_expires: int = 600
    ca_file: str | None = None  # tests: trust this CA instead of the system store

    @property
    def aor(self) -> str:
        return f"sip:{self.username}@{self.domain}"

    def target_uri(self, target: str) -> str:
        """``alice`` -> ``sip:alice@<domain>``; full SIP URIs are kept."""
        target = target.strip()
        if target.startswith(("sip:", "sips:")):
            return target
        return f"sip:{target}@{self.domain}" if "@" not in target else f"sip:{target}"


def split_header_list(values: list[str]) -> list[str]:
    """Split comma-joined header values (Record-Route), ignoring commas inside <> or quotes."""
    out: list[str] = []
    for value in values:
        depth, quoted, start = 0, False, 0
        for i, ch in enumerate(value):
            if ch == '"':
                quoted = not quoted
            elif not quoted and ch == "<":
                depth += 1
            elif not quoted and ch == ">":
                depth -= 1
            elif not quoted and depth == 0 and ch == ",":
                out.append(value[start:i].strip())
                start = i + 1
        out.append(value[start:].strip())
    return [v for v in out if v]


class _DigestClient:
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self._nc: dict[str, int] = {}

    def answer(self, resp: SipMessage, method: str, uri: str) -> tuple[str, str] | None:
        """(header name, value) answering a 401/407, or None if there's no usable challenge."""
        name = "WWW-Authenticate" if resp.status == 401 else "Proxy-Authenticate"
        challenges = [c for c in resp.get_all(name) if c.lower().startswith("digest")]
        params = [dict(_parse_challenge(c)) for c in challenges]
        usable = [p for p in params if (p.get("algorithm") or "MD5").upper() in ("MD5", "SHA-256")]
        if not usable:
            return None
        # Prefer SHA-256 when offered (some providers store only SHA-256 password hashes).
        p = next((p for p in usable if (p.get("algorithm") or "").upper() == "SHA-256"), usable[0])
        algorithm = (p.get("algorithm") or "MD5").upper()
        realm, nonce = p.get("realm", ""), p.get("nonce", "")
        qop = "auth" if "auth" in (p.get("qop") or "").split(",") else None
        nc_value = self._nc.get(nonce, 0) + 1
        self._nc[nonce] = nc_value
        nc, cnonce = f"{nc_value:08x}", secrets.token_hex(8)
        response = compute_response(
            algorithm=algorithm,
            username=self.username,
            realm=realm,
            password=self.password,
            method=method,
            uri=uri,
            nonce=nonce,
            nc=nc if qop else None,
            cnonce=cnonce if qop else None,
            qop=qop,
        )
        value = (
            f'Digest username="{self.username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}", algorithm={algorithm}'
        )
        if qop:
            value += f', qop={qop}, nc={nc}, cnonce="{cnonce}"'
        if "opaque" in p:
            value += f', opaque="{p["opaque"]}"'
        header = "Authorization" if resp.status == 401 else "Proxy-Authorization"
        return header, value


_CHALLENGE_KV = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^\s,]+))')


def _parse_challenge(value: str) -> list[tuple[str, str]]:
    rest = value.split(" ", 1)[1] if " " in value else ""
    return [
        (m.group(1).lower(), m.group(2) if m.group(2) is not None else m.group(3))
        for m in _CHALLENGE_KV.finditer(rest)
    ]


class SipAccountClient:
    def __init__(
        self, config: AccountConfig, on_call: Callable[[SipCall], Awaitable[None]]
    ) -> None:
        if not config.username or not config.password:
            raise ValueError("SIP account username and password are required")
        self.config = config
        self.on_call = on_call
        self.auth = _DigestClient(config.auth_username or config.username, config.password)
        self.instance = f"<urn:uuid:{uuid.uuid4()}>"
        self.calls: dict[str, SipCall] = {}
        self.registered = asyncio.Event()
        self.last_error: str | None = None
        self._peer: _Peer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tx: dict[tuple[str, str], asyncio.Queue[SipMessage]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._runner: asyncio.Task[None] | None = None
        self._reg_call_id = f"{secrets.token_hex(10)}@avb"
        self._reg_tag = secrets.token_hex(6)
        self._reg_cseq = 0
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------------------

    async def start(self, *, timeout: float = 20) -> None:
        """Connect and sign in; raises AccountError if the first sign-in fails."""
        first: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._runner = asyncio.get_running_loop().create_task(self._run(first))
        try:
            await asyncio.wait_for(asyncio.shield(first), timeout)
        except TimeoutError as exc:
            await self.stop()
            raise AccountError(
                f"could not sign in to {self.config.domain} within {timeout:.0f}s"
            ) from exc
        except AccountError:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._stopping = True
        for call in list(self.calls.values()):
            await call.hangup()
        if self._writer is not None and self.registered.is_set():
            with contextlib.suppress(Exception):
                self._send_register(expires=0)
                await asyncio.sleep(0.1)
        if self._runner:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._runner
        for task in list(self._tasks):
            task.cancel()
        if self._writer is not None:
            self._writer.close()

    async def _run(self, first: asyncio.Future[None]) -> None:
        backoff = 2.0
        while not self._stopping:
            try:
                await self._connect_and_serve(first)
                backoff = 2.0
            except AccountError as exc:
                self.last_error = str(exc)
                log.warning("SIP account %s: %s", self.config.aor, exc)
                if not first.done():
                    first.set_exception(exc)
                    return
            except (OSError, ssl.SSLError, SipParseError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("SIP account connection problem: %s", self.last_error)
                if not first.done():
                    first.set_exception(AccountError(self.last_error))
                    return
            self.registered.clear()
            self._peer = None
            if self._stopping:
                return
            log.info("SIP account: reconnecting in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _connect_and_serve(self, first: asyncio.Future[None]) -> None:
        host = self.config.proxy_host or self.config.domain
        ctx = ssl.create_default_context(cafile=self.config.ca_file)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host, self.config.proxy_port, ssl=ctx, server_hostname=self.config.domain
            ),
            15,
        )
        self._writer = writer
        local = writer.get_extra_info("sockname")
        remote = writer.get_extra_info("peername")
        self._peer = _Peer("tls", remote[:2], self.config.media_ip or local[0], writer.write)
        self._local_port = int(local[1])
        log.info("SIP account: connected to %s:%s over TLS", *remote[:2])
        reading = asyncio.get_running_loop().create_task(self._read(reader))
        try:
            await self._register_loop(first, reading)
        finally:
            reading.cancel()
            writer.close()

    async def _read(self, reader: asyncio.StreamReader) -> None:
        buffer = b""
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                raise ConnectionError("provider closed the connection")
            buffer += chunk
            while True:
                buffer = buffer.lstrip(b"\r\n")  # keep-alive pongs
                n = frame_length(buffer)
                if n is None:
                    break
                data, buffer = buffer[:n], buffer[n:]
                try:
                    self._dispatch(parse(data))
                except SipParseError as exc:
                    log.debug("ignoring malformed SIP from provider: %s", exc)

    async def _register_loop(
        self, first: asyncio.Future[None], reading: asyncio.Task[None]
    ) -> None:
        while True:
            expires = await self._register()
            self.registered.set()
            self.last_error = None
            if not first.done():
                first.set_result(None)
            log.info("SIP account %s signed in (expires %ss)", self.config.aor, expires)
            refresh_at = time.monotonic() + max(30.0, expires * 0.8)
            while time.monotonic() < refresh_at:
                wait = min(KEEPALIVE_SECONDS, refresh_at - time.monotonic())
                done, _ = await asyncio.wait({reading}, timeout=max(0.0, wait))
                if done:
                    reading.result()  # raises the connection error
                    raise ConnectionError("connection closed")
                assert self._writer is not None
                self._writer.write(b"\r\n\r\n")  # RFC 5626 keep-alive ping

    # -- sending ----------------------------------------------------------------------

    def _contact(self) -> str:
        assert self._peer is not None
        return (
            f"<sip:{self.config.username}@{self._peer.local_ip}:{self._local_port};"
            f'transport=tls;ob>;+sip.instance="{self.instance}";reg-id=1'
        )

    def _via(self, branch: str) -> str:
        assert self._peer is not None
        return f"SIP/2.0/TLS {self._peer.local_ip}:{self._local_port};branch={branch};rport;alias"

    def _send(self, msg: SipMessage) -> None:
        if self._peer is None:
            raise ConnectionError("not connected to the SIP provider")
        self._peer.send(msg.to_bytes())

    def _build(
        self,
        method: str,
        uri: str,
        *,
        branch: str,
        from_: str,
        to: str,
        call_id: str,
        cseq: int,
        route: list[str] | None = None,
        extra: list[tuple[str, str]] | None = None,
        body: bytes = b"",
    ) -> SipMessage:
        req = SipMessage(method=method, uri=uri)
        req.add("Via", self._via(branch))
        req.add("Max-Forwards", "70")
        for r in route or []:
            req.add("Route", r)
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
        return req

    def _send_register(self, *, expires: int, auth: tuple[str, str] | None = None) -> None:
        self._reg_cseq += 1
        extra = [
            ("Contact", self._contact()),
            ("Expires", str(expires)),
            ("Supported", "outbound, path"),
            ("Allow", ALLOW),
        ]
        if auth:
            extra.append(auth)
        self._send(
            self._build(
                "REGISTER",
                f"sip:{self.config.domain}",
                branch=f"z9hG4bK{secrets.token_hex(8)}",
                from_=f"<{self.config.aor}>;tag={self._reg_tag}",
                to=f"<{self.config.aor}>",
                call_id=self._reg_call_id,
                cseq=self._reg_cseq,
                extra=extra,
            )
        )

    async def _register(self) -> int:
        queue: asyncio.Queue[SipMessage] = asyncio.Queue()
        self._tx[(self._reg_call_id, "REGISTER")] = queue
        try:
            auth: tuple[str, str] | None = None
            for _ in range(3):
                self._send_register(expires=self.config.register_expires, auth=auth)
                resp = await self._final(queue, 15)
                if resp.status in (401, 407):
                    if auth is not None and not _stale(resp):
                        raise AccountError("sign-in refused: wrong username or password")
                    auth = self.auth.answer(resp, "REGISTER", f"sip:{self.config.domain}")
                    if auth is None:
                        raise AccountError("provider sent no usable digest challenge")
                    continue
                if resp.status == 200:
                    return _granted_expires(resp, self.config.register_expires)
                raise AccountError(f"sign-in refused: {resp.status} {resp.reason}")
            raise AccountError("sign-in refused after repeated challenges")
        finally:
            self._tx.pop((self._reg_call_id, "REGISTER"), None)

    @staticmethod
    async def _final(queue: asyncio.Queue[SipMessage], timeout: float) -> SipMessage:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_running_loop().time()
            try:
                resp = await asyncio.wait_for(queue.get(), max(0.0, left))
            except TimeoutError as exc:
                raise AccountError("no answer from the provider") from exc
            if (resp.status or 0) >= 200:
                return resp

    # -- receiving ----------------------------------------------------------------------

    def _dispatch(self, msg: SipMessage) -> None:
        if not msg.is_request:
            try:
                _, method = msg.cseq
            except SipParseError:
                return
            queue = self._tx.get((msg.call_id, method))
            if queue is not None:
                queue.put_nowait(msg)
            return
        assert self._peer is not None
        peer = self._peer
        method = msg.method or ""
        call = self.calls.get(msg.call_id)
        if method == "ACK":
            return
        if method == "BYE":
            if call is None:
                self._reply(msg, 481, "Call/Transaction Does Not Exist")
                return
            self._reply(msg, 200, "OK")
            log.info("SIP account: the phone hung up")
            self.calls.pop(msg.call_id, None)
            call.ended.set()
            call.rtp.close()
        elif method == "INVITE" and call is not None:  # re-INVITE: same session
            resp = response_for(msg, 200, "OK")
            resp.add("Contact", self._contact())
            resp.add("Content-Type", "application/sdp")
            resp.body = call.answer_sdp
            peer.send(resp.to_bytes())
        elif method == "INVITE":
            self._reply(msg, 486, "Busy Here")  # nobody calls the bridge
        elif method in ("OPTIONS", "NOTIFY", "UPDATE", "INFO"):
            self._reply(msg, 200, "OK")
        elif method == "CANCEL":
            self._reply(msg, 481, "Call/Transaction Does Not Exist")
        else:
            self._reply(msg, 405, "Method Not Allowed")

    def _reply(self, req: SipMessage, status: int, reason: str) -> None:
        resp = response_for(req, status, reason, to_tag=secrets.token_hex(4))
        resp.add("Server", USER_AGENT)
        with contextlib.suppress(ConnectionError):
            self._send(resp)

    # -- calls ----------------------------------------------------------------------------

    async def dial(self, target: str, *, ring_seconds: float = 60) -> SipCall:
        """Call ``target`` (a username on the same domain, or a SIP URI).

        Returns once answered; the call then runs ``on_call``. Raises DialError.
        """
        if not self.registered.is_set() or self._peer is None:
            raise DialError(f"not signed in to {self.config.domain}: {self.last_error or '...'}")
        if self.calls:
            raise DialError("a call is already in progress")
        peer = self._peer
        uri = self.config.target_uri(target)
        call_id = f"{secrets.token_hex(12)}@avb"
        local_tag = secrets.token_hex(6)
        from_ = f"<{self.config.aor}>;tag={local_tag}"
        to = f"<{uri}>"
        key, crypto = make_offer(SUITE)
        call: SipCall | None = None

        def on_dtmf(digit: str) -> None:
            if call is not None:
                call.dtmf.put_nowait(digit)

        rtp, rtp_port = await RtpSession.open(
            self.config.bind_host,
            self.config.rtp_ports,
            codec=0,
            remote=("0.0.0.0", 0),  # noqa: S104 - set from the answer
            telephone_event=101,
            on_dtmf=on_dtmf,
            allow_any_host=True,  # the phone's NAT address is unknown; SRTP authenticates
        )
        media_ip = self.config.media_ip or peer.local_ip
        profiles = ["RTP/SAVP"] if self.config.require_srtp else ["RTP/SAVP", "RTP/AVP"]
        queue: asyncio.Queue[SipMessage] = asyncio.Queue()
        self._tx[(call_id, "INVITE")] = queue
        self._tx[(call_id, "BYE")] = asyncio.Queue()
        self._tx[(call_id, "CANCEL")] = asyncio.Queue()
        cseq = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ring_seconds
        challenge: SipMessage | None = None  # the provider's 407, answered on every INVITE
        offer = b""
        try:
            resp: SipMessage | None = None
            while profiles:
                profile = profiles[0]
                offer = sdp.build_offer(
                    address=media_ip, port=rtp_port, profile=profile, crypto=crypto
                )
                cseq += 1
                branch = f"z9hG4bK{secrets.token_hex(8)}"
                extra = [("Contact", self._contact()), ("Allow", ALLOW)]
                if challenge is not None:
                    auth = self.auth.answer(challenge, "INVITE", uri)
                    if auth is None:
                        raise DialError("provider sent no usable digest challenge")
                    extra.append(auth)
                invite = self._build(
                    "INVITE",
                    uri,
                    branch=branch,
                    from_=from_,
                    to=to,
                    call_id=call_id,
                    cseq=cseq,
                    extra=extra,
                    body=offer,
                )
                log.info("SIP account: calling %s", uri)
                self._send(invite)
                resp = await self._invite_outcome(queue, deadline, invite)
                status = resp.status or 0
                if status >= 300:
                    # Non-2xx ACK: same branch as the INVITE, hop-by-hop.
                    self._send(
                        self._build(
                            "ACK",
                            uri,
                            branch=branch,
                            from_=from_,
                            to=resp.get("To") or to,
                            call_id=call_id,
                            cseq=cseq,
                        )
                    )
                if status in (401, 407):
                    if challenge is not None and not _stale(resp):
                        raise DialError("the provider refused the bridge account's password")
                    challenge = resp
                    continue
                if status == 488 and len(profiles) > 1:
                    profiles.pop(0)  # phone wants plain RTP/AVP (+ optional a=crypto)
                    continue
                if status >= 300:
                    raise DialError(_explain(status, resp.reason))
                break
            assert resp is not None and resp.status == 200
            route = list(reversed(split_header_list(resp.get_all("Record-Route"))))
            remote_target = uri_of(resp.get("Contact") or "") or uri
            remote_to = resp.get("To") or to

            def ack(r: SipMessage) -> None:
                self._send(
                    self._build(
                        "ACK",
                        remote_target,
                        branch=f"z9hG4bK{secrets.token_hex(8)}",
                        from_=from_,
                        to=r.get("To") or remote_to,
                        call_id=call_id,
                        cseq=cseq,
                        route=route,
                    )
                )

            ack(resp)
            answer = sdp.parse_offer(resp.body)
            codec = sdp.choose_codec(answer)
            problem = None
            if codec is None:
                problem = "phone answered without PCMU/PCMA"
            elif answer.crypto:
                try:
                    rtp.srtp = from_answer(key, SUITE, answer.crypto)
                except SrtpError as exc:
                    problem = str(exc)
            elif self.config.require_srtp or "SAVP" in answer.profile:
                problem = "phone answered without SRTP (set Linphone media encryption to SRTP)"
            if problem:
                self._send_bye_raw(remote_target, route, from_, remote_to, call_id, cseq + 1)
                raise DialError(problem)
            assert codec is not None
            rtp.codec = codec
            rtp.telephone_event = answer.telephone_event
            rtp.remote = (answer.address, answer.port)
            if rtp.srtp is None:
                log.warning("SIP account: call audio is NOT encrypted (phone declined SRTP)")
        except BaseException:
            rtp.close()
            for method in ("INVITE", "BYE", "CANCEL"):
                self._tx.pop((call_id, method), None)
            raise
        call = SipCall(
            call_id=call_id,
            caller=uri,
            peer=peer,
            rtp=rtp,
            local_tag=local_tag,
            remote_from=remote_to,
            local_to=from_,
            remote_contact=remote_target,
            rtp_port=rtp_port,
            answer_sdp=offer,
            _server=self,
            _cseq=cseq,
        )
        call.route = route
        self.calls[call_id] = call
        log.info("SIP account: %s answered", uri)

        async def re_ack() -> None:  # a lost ACK makes the phone resend its 200
            with contextlib.suppress(asyncio.CancelledError):
                while True:
                    r = await queue.get()
                    if r.status == 200 and tag_of(r.get("To")) == tag_of(remote_to):
                        ack(r)
                    elif r.status == 200:  # a second device answered too: drop it
                        ack(r)
                        self._send_bye_raw(
                            uri_of(r.get("Contact") or "") or uri,
                            list(reversed(split_header_list(r.get_all("Record-Route")))),
                            from_,
                            r.get("To") or to,
                            call_id,
                            cseq + 1,
                        )

        reack = loop.create_task(re_ack())
        self._tasks.add(reack)

        def cleanup() -> None:
            reack.cancel()
            self._tasks.discard(reack)
            for method in ("INVITE", "BYE", "CANCEL"):
                self._tx.pop((call_id, method), None)

        call.ended_callbacks.append(cleanup)
        self._start_call(call)
        return call

    async def _invite_outcome(
        self, queue: asyncio.Queue[SipMessage], deadline: float, invite: SipMessage
    ) -> SipMessage:
        loop = asyncio.get_running_loop()
        while True:
            left = deadline - loop.time()
            if left <= 0:
                self._send_cancel(invite)
                # The phone answers 487 to the INVITE; wait briefly to ACK it.
                with contextlib.suppress(TimeoutError):
                    while True:
                        r = await asyncio.wait_for(queue.get(), 4)
                        if (r.status or 0) >= 200:
                            return r
                raise DialError("no answer")
            try:
                resp = await asyncio.wait_for(queue.get(), left)
            except TimeoutError:
                continue
            except asyncio.CancelledError:  # nothing left to decide: stop the phone ringing
                self._send_cancel(invite)
                raise
            status = resp.status or 0
            if status == 180:
                log.info("SIP account: the phone is ringing")
            if status >= 200:
                return resp

    def _send_cancel(self, invite: SipMessage) -> None:
        cancel = SipMessage(method="CANCEL", uri=invite.uri)
        for name in ("Via", "Route", "From", "To", "Call-ID"):
            for v in invite.get_all(name):
                cancel.add(name, v)
        cancel.add("CSeq", f"{invite.cseq[0]} CANCEL")
        cancel.add("Max-Forwards", "70")
        with contextlib.suppress(ConnectionError):
            self._send(cancel)

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

    def _send_bye_raw(
        self, target: str, route: list[str], from_: str, to: str, call_id: str, cseq: int
    ) -> None:
        with contextlib.suppress(ConnectionError):
            self._send(
                self._build(
                    "BYE",
                    target,
                    branch=f"z9hG4bK{secrets.token_hex(8)}",
                    from_=from_,
                    to=to,
                    call_id=call_id,
                    cseq=cseq,
                    route=route,
                )
            )

    def _send_bye(self, call: SipCall) -> None:
        """Called by ``SipCall.hangup``."""
        self.calls.pop(call.call_id, None)
        call._cseq += 1
        self._send_bye_raw(
            call.remote_contact,
            call.route,
            call.local_to,
            call.remote_from,
            call.call_id,
            call._cseq,
        )


def _granted_expires(resp: SipMessage, asked: int) -> int:
    for contact in split_header_list(resp.get_all("Contact")):
        m = re.search(r";\s*expires=(\d+)", contact)
        if m:
            return int(m.group(1))
    value = resp.get("Expires")
    return int(value) if value and value.isdigit() else asked


def _stale(resp: SipMessage) -> bool:
    name = "WWW-Authenticate" if resp.status == 401 else "Proxy-Authenticate"
    return any("stale=true" in c.lower().replace(" ", "") for c in resp.get_all(name))


def _explain(status: int, reason: str) -> str:
    hints = {
        403: "the provider refused the call",
        404: "that Linphone account doesn't exist",
        408: "the phone didn't wake up (is Linphone signed in on it, with push allowed?)",
        480: "the phone is unavailable (Linphone not signed in, or push failed)",
        486: "the phone is busy",
        487: "no answer",
        603: "you declined the call",
    }
    return f"{status} {reason}" + (f": {hints[status]}" if status in hints else "")
