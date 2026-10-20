"""Server-side SIP digest authentication (RFC 3261 section 22, RFC 8760 SHA-256), qop=auth.

Nonces are self-validating (timestamp + HMAC), so they survive without state, and
expire after ``NONCE_TTL`` seconds. Nonce counts must increase per nonce, which stops
a captured Authorization header from being replayed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

NONCE_TTL = 300
ALGORITHMS = {"SHA-256": hashlib.sha256, "MD5": hashlib.md5}

_KV = re.compile(r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')


def parse_auth(value: str) -> dict[str, str]:
    scheme, _, rest = value.strip().partition(" ")
    if scheme.lower() != "digest":
        return {}
    return {
        m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3))
        for m in _KV.finditer(rest)
    }


def _h(algorithm: str, text: str) -> str:
    return ALGORITHMS[algorithm](text.encode("utf-8")).hexdigest()


def compute_response(
    *,
    algorithm: str,
    username: str,
    realm: str,
    password: str,
    method: str,
    uri: str,
    nonce: str,
    nc: str | None,
    cnonce: str | None,
    qop: str | None,
) -> str:
    ha1 = _h(algorithm, f"{username}:{realm}:{password}")
    ha2 = _h(algorithm, f"{method}:{uri}")
    if qop:
        return _h(algorithm, f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    return _h(algorithm, f"{ha1}:{nonce}:{ha2}")


class DigestAuthenticator:
    def __init__(
        self, realm: str, username: str, password: str, *, clock: object = time.time
    ) -> None:
        self.realm = realm
        self.username = username
        self.password = password
        self._key = secrets.token_bytes(32)
        self._clock = clock
        self._nc: dict[str, int] = {}

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    def new_nonce(self) -> str:
        # Unique per challenge (random part), so nonce counts are tracked per client.
        stamp = f"{int(self._now())}.{secrets.token_hex(8)}"
        mac = hmac.new(self._key, stamp.encode(), hashlib.sha256).hexdigest()[:32]
        return base64.urlsafe_b64encode(f"{stamp}:{mac}".encode()).decode().rstrip("=")

    def _nonce_valid(self, nonce: str) -> bool | None:
        """True = valid, False = forged, None = expired (stale)."""
        try:
            raw = base64.urlsafe_b64decode(nonce + "=" * (-len(nonce) % 4)).decode()
            stamp, mac = raw.split(":", 1)
            issued = int(stamp.split(".", 1)[0])
        except (ValueError, UnicodeDecodeError):
            return False
        expected = hmac.new(self._key, stamp.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(mac, expected):
            return False
        return None if self._now() - issued > NONCE_TTL else True

    def challenges(self, *, stale: bool = False) -> list[str]:
        nonce = self.new_nonce()
        extra = ", stale=true" if stale else ""
        return [
            f'Digest realm="{self.realm}", nonce="{nonce}", algorithm={alg}, qop="auth"{extra}'
            for alg in ALGORITHMS
        ]

    def verify(self, header: str | None, method: str) -> tuple[bool, bool]:
        """Return (authorised, stale)."""
        if not header:
            return False, False
        p = parse_auth(header)
        algorithm = (p.get("algorithm") or "MD5").upper()
        if (
            algorithm not in ALGORITHMS
            or p.get("username") != self.username
            or p.get("realm") != self.realm
        ):
            return False, False
        nonce = p.get("nonce", "")
        state = self._nonce_valid(nonce)
        if state is False:
            return False, False
        if state is None:
            return False, True
        qop = p.get("qop")
        if qop not in (None, "auth"):
            return False, False
        if qop:
            try:
                nc = int(p.get("nc", ""), 16)
            except ValueError:
                return False, False
            if nc <= self._nc.get(nonce, 0):
                return False, False  # replayed or reordered
        expected = compute_response(
            algorithm=algorithm,
            username=self.username,
            realm=self.realm,
            password=self.password,
            method=method,
            uri=p.get("uri", ""),
            nonce=nonce,
            nc=p.get("nc"),
            cnonce=p.get("cnonce"),
            qop=qop,
        )
        if not hmac.compare_digest(expected, p.get("response", "")):
            return False, False
        if qop:
            self._nc[nonce] = nc
            if len(self._nc) > 1000:
                self._nc.pop(next(iter(self._nc)))
        return True, False
