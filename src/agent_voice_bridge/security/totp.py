"""RFC 6238 TOTP (SHA-1, 6 digits, 30 s), standard library only.

Used as the second factor for high-risk approvals: the user reads the current code
from an authenticator app on the phone and says it during the call. Voice alone is
not proof of identity; the code is.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret_b32: str) -> bytes:
    s = secret_b32.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret_b32: str, counter: int) -> str:
    digest = hmac.new(_key(secret_b32), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_counter(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def verify(secret_b32: str, code: str, *, now: float | None = None, window: int = 1) -> int | None:
    """Return the matching time-step counter, or None. Callers must reject reused counters."""
    if not code.isdigit() or len(code) != DIGITS:
        return None
    base = current_counter(now)
    for delta in range(-window, window + 1):
        counter = base + delta
        if hmac.compare_digest(code_at(secret_b32, counter), code):
            return counter
    return None


def provisioning_uri(
    secret_b32: str, account: str = "approvals", issuer: str = "AgentVoiceBridge"
) -> str:
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}"
        f"?secret={secret_b32}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP_SECONDS}"
    )
