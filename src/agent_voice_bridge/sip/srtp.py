"""SDES-SRTP (RFC 4568 key exchange, RFC 3711 SRTP) using libsrtp via ``pylibsrtp``.

The caller's key arrives in its SDP offer (``a=crypto:``) over TLS-protected SIP; we
answer with a fresh key of our own. Linphone offers SDES when its media encryption
is set to SRTP. Supported suites: AES_CM_128_HMAC_SHA1_80 and _32.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from pylibsrtp import Error as SrtpLibError
from pylibsrtp import Policy, Session

SUITES = {
    "AES_CM_128_HMAC_SHA1_80": Policy.SRTP_PROFILE_AES128_CM_SHA1_80,
    "AES_CM_128_HMAC_SHA1_32": Policy.SRTP_PROFILE_AES128_CM_SHA1_32,
}
KEY_SALT_LEN = 30  # 16-byte key + 14-byte salt


class SrtpError(ValueError):
    pass


@dataclass
class SrtpContext:
    inbound: Session
    outbound: Session
    suite: str

    def protect(self, packet: bytes) -> bytes:
        return bytes(self.outbound.protect(packet))

    def unprotect(self, packet: bytes) -> bytes | None:
        """Decrypted RTP, or None if authentication fails (forged or corrupted packet)."""
        try:
            return bytes(self.inbound.unprotect(packet))
        except SrtpLibError:
            return None


def _parse(attr: str) -> tuple[str, str, bytes] | None:
    # "1 AES_CM_128_HMAC_SHA1_80 inline:base64key|2^31|1:1 [session params]"
    parts = attr.split()
    if len(parts) < 3 or parts[1] not in SUITES or not parts[2].startswith("inline:"):
        return None
    key_b64 = parts[2][len("inline:") :].split("|", 1)[0]
    try:
        key = base64.b64decode(key_b64 + "=" * (-len(key_b64) % 4), validate=True)
    except ValueError:
        return None
    if (
        len(key) != KEY_SALT_LEN
        or len(parts) > 3
        and any(p.startswith("UNENCRYPTED") for p in parts[3:])
    ):
        return None
    return parts[0], parts[1], key


def negotiate(offers: list[str]) -> tuple[SrtpContext, str]:
    """Pick the first supported offer. Returns the context and our ``a=crypto`` value."""
    for attr in offers:
        parsed = _parse(attr)
        if parsed is None:
            continue
        tag, suite, remote_key = parsed
        local_key = os.urandom(KEY_SALT_LEN)
        profile = SUITES[suite]
        ctx = SrtpContext(
            inbound=Session(
                Policy(key=remote_key, ssrc_type=Policy.SSRC_ANY_INBOUND, srtp_profile=profile)
            ),
            outbound=Session(
                Policy(key=local_key, ssrc_type=Policy.SSRC_ANY_OUTBOUND, srtp_profile=profile)
            ),
            suite=suite,
        )
        return ctx, f"{tag} {suite} inline:{base64.b64encode(local_key).decode()}"
    raise SrtpError("no supported SRTP crypto suite offered")
