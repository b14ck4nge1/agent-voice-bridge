"""Bearer-token authentication with separate roles.

* ``agent``: hooks and agent adapters. May create requests and wait on/cancel the
  ones they created (which additionally requires the per-request secret).
* ``voice``: the voice/call service. May read pending requests, post call events and
  submit decisions. It cannot create requests, so the voice assistant cannot invent
  an approval request.
"""

from __future__ import annotations

import hashlib
import hmac
from enum import StrEnum


class Role(StrEnum):
    AGENT = "agent"
    VOICE = "voice"


def _matches(presented: str, expected: str | None) -> bool:
    if not expected:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())


def role_for_token(token: str, *, agent_token: str | None, voice_token: str | None) -> Role | None:
    if _matches(token, agent_token):
        return Role.AGENT
    if _matches(token, voice_token):
        return Role.VOICE
    return None


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def secret_matches(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), stored_hash)
