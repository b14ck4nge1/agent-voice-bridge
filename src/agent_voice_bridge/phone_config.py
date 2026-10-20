"""Settings for the "bark-sip" phone path: Bark wakes the phone, Linphone calls the PC.

All values come from the environment (or ``bridge.env``):

==========================  =========================================================
AVB_BARK_DEVICE_KEY         Key shown in the Bark app (required)
AVB_BARK_SERVER             Default ``https://api.day.app``; or your own bark-server
AVB_BARK_ENCRYPTION_KEY     32 ASCII chars; set the same in Bark (AES256 / GCM)
AVB_BARK_LEVEL              ``critical`` (default) or ``timeSensitive``
AVB_SIP_BIND                Address to listen on, e.g. this PC's Tailscale IP (required)
AVB_SIP_PORT                Default 5060
AVB_SIP_USERNAME            Account name the iPhone uses (default ``phone``)
AVB_SIP_PASSWORD            >= 16 chars (required); ``avb-admin init`` generates one
AVB_SIP_PUBLIC_HOST         Host the phone dials, if different from AVB_SIP_BIND
AVB_SIP_REALM               Digest realm (default: the public host)
AVB_SIP_TRANSPORT           ``udp`` (default), ``tcp`` or ``tls``, used in the dial link
AVB_SIP_TLS_CERT / _KEY     Certificate + key files; enables SIP over TLS (port 5061)
AVB_SIP_TLS_PORT            Default 5061
AVB_SIP_REQUIRE_SRTP        ``1`` refuses unencrypted audio (default: 1 when TLS is on)
AVB_SIP_PLAIN               ``0`` disables UDP/TCP listeners (default: 0 when TLS is on)
AVB_SIP_MEDIA_IP            Public IP to put in SDP if the server sits behind 1:1 NAT
AVB_RTP_PORTS               Default ``40000-40200``
AVB_TTS_VOICE / AVB_TTS_RATE  Windows SAPI voice name and speed (-10..10)
==========================  =========================================================
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from agent_voice_bridge.notify.bark import DEFAULT_SERVER, BarkConfig
from agent_voice_bridge.sip.adapter.bark_wake import dial_url
from agent_voice_bridge.sip.server import SipServerConfig, is_private_address


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class PhoneSettings:
    bark: BarkConfig
    sip: SipServerConfig
    public_host: str
    transport: str
    tts_voice: str | None
    tts_rate: int

    @property
    def dial_url(self) -> str:
        port = self.sip.tls_port if self.transport == "tls" else self.sip.port
        return dial_url(self.public_host, port, self.transport)

    @classmethod
    def from_env(cls) -> PhoneSettings:
        missing = [
            n for n in ("AVB_BARK_DEVICE_KEY", "AVB_SIP_BIND", "AVB_SIP_PASSWORD") if not _get(n)
        ]
        if missing:
            raise ValueError(f"AVB_CALL_DISPATCHER=bark-sip needs: {', '.join(missing)}")
        lo, _, hi = (_get("AVB_RTP_PORTS", "40000-40200") or "").partition("-")
        bind = _get("AVB_SIP_BIND") or ""
        cert, key = _get("AVB_SIP_TLS_CERT"), _get("AVB_SIP_TLS_KEY")
        tls = bool(cert and key)
        transport = (_get("AVB_SIP_TRANSPORT", "tls" if tls else "udp") or "udp").lower()
        if transport not in ("udp", "tcp", "tls"):
            raise ValueError("AVB_SIP_TRANSPORT must be udp, tcp or tls")
        if transport == "tls" and not tls:
            raise ValueError("AVB_SIP_TRANSPORT=tls needs AVB_SIP_TLS_CERT and AVB_SIP_TLS_KEY")
        plain = (_get("AVB_SIP_PLAIN", "0" if tls else "1") or "1") == "1"
        require_srtp = (_get("AVB_SIP_REQUIRE_SRTP", "1" if tls else "0") or "0") == "1"
        settings = cls(
            bark=BarkConfig(
                device_key=_get("AVB_BARK_DEVICE_KEY") or "",
                server=_get("AVB_BARK_SERVER", DEFAULT_SERVER) or DEFAULT_SERVER,
                encryption_key=_get("AVB_BARK_ENCRYPTION_KEY"),
                level=_get("AVB_BARK_LEVEL", "critical") or "critical",
            ),
            sip=SipServerConfig(
                bind_host=bind,
                port=int(_get("AVB_SIP_PORT", "5060") or 5060),
                username=_get("AVB_SIP_USERNAME", "phone") or "phone",
                password=_get("AVB_SIP_PASSWORD") or "",
                # Phones match saved passwords by realm; the domain is what they expect.
                realm=_get("AVB_SIP_REALM", _get("AVB_SIP_PUBLIC_HOST", bind) or bind) or bind,
                rtp_ports=(int(lo), int(hi or lo)),
                udp=plain,
                tcp=plain,
                tls_cert=cert,
                tls_key=key,
                tls_port=int(_get("AVB_SIP_TLS_PORT", "5061") or 5061),
                require_srtp=require_srtp,
                media_ip=_get("AVB_SIP_MEDIA_IP"),
            ),
            public_host=_get("AVB_SIP_PUBLIC_HOST", bind) or bind,
            transport=transport,
            tts_voice=_get("AVB_TTS_VOICE"),
            tts_rate=int(_get("AVB_TTS_RATE", "0") or 0),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        self.bark.validate()
        self.sip.validate()
        secured = (
            bool(self.sip.tls_cert) and self.sip.require_srtp and not (self.sip.udp or self.sip.tcp)
        )
        if (
            not is_private_address(self.sip.bind_host)
            and not secured
            and os.environ.get("AVB_ALLOW_PUBLIC_SIP") != "1"
        ):
            raise ValueError(
                f"AVB_SIP_BIND={self.sip.bind_host} is not a private address. On a public address "
                "use TLS only (AVB_SIP_TLS_CERT/_KEY, AVB_SIP_PLAIN=0, AVB_SIP_REQUIRE_SRTP=1), or "
                "bind to your Tailscale (100.x) or LAN address."
            )
