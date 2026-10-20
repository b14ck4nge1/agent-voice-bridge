"""Wake the phone with a Bark push notification (https://github.com/Finb/Bark).

Bark is a free, MIT-licensed iOS app. Its server (also open source, self-hostable)
can push to the App Store app. We use it because iOS won't let a self-hosted SIP
server wake a sleeping softphone. The notification:

* rings like a call (``call=1`` repeats the ringtone for about 30 s, per Bark's docs),
* can break through silent mode and Focus (``level=critical``; you allow critical
  alerts for Bark once in iOS),
* opens ``url`` when tapped. With ``linphone-sip:user@host``, Linphone starts a call
  to the bridge right away (Linphone iOS ``URIHandler``).

When an encryption key is configured, everything (including the URL) goes inside
Bark's AES-256-GCM ``ciphertext`` (key: 32 ASCII characters, IV: 12 ASCII
characters per message, ciphertext||tag in Base64), which the Bark app decrypts on
the phone. Set the same key in Bark under *Encryption Settings*: AES256, GCM. The
notification text never includes the command itself, only which agent and project
is waiting.
"""

from __future__ import annotations

import base64
import json
import secrets
import string
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_SERVER = "https://api.day.app"
_IV_ALPHABET = string.ascii_letters + string.digits


class BarkError(Exception):
    pass


@dataclass(frozen=True)
class BarkConfig:
    device_key: str
    server: str = DEFAULT_SERVER
    encryption_key: str | None = None  # 32 ASCII chars => AES-256-GCM
    level: str = "critical"  # critical | timeSensitive | active | passive
    volume: int = 8  # 0-10, critical alerts only
    call: bool = True
    group: str = "agent-voice-bridge"
    sound: str | None = None

    def validate(self) -> None:
        if not self.device_key or "/" in self.device_key:
            raise BarkError("Bark device key is missing or malformed")
        if not self.server.startswith("https://"):
            raise BarkError("Bark server must be https://")
        if self.encryption_key is not None and (
            len(self.encryption_key) != 32 or not self.encryption_key.isascii()
        ):
            raise BarkError("Bark encryption key must be exactly 32 ASCII characters (AES-256)")
        if self.level not in {"critical", "timeSensitive", "active", "passive"}:
            raise BarkError(f"unknown Bark level {self.level!r}")


def encrypt_gcm(key: str, plaintext: str, iv: str | None = None) -> tuple[str, str]:
    """Encrypt the way Bark's ``AESCryptoModel`` decrypts AES-GCM (CryptoSwift, combined mode)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    iv = iv or "".join(secrets.choice(_IV_ALPHABET) for _ in range(12))
    sealed = AESGCM(key.encode("ascii")).encrypt(
        iv.encode("ascii"), plaintext.encode("utf-8"), None
    )
    return base64.b64encode(sealed).decode("ascii"), iv


def decrypt_gcm(key: str, ciphertext: str, iv: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    data = AESGCM(key.encode("ascii")).decrypt(
        iv.encode("ascii"), base64.b64decode(ciphertext), None
    )
    return data.decode("utf-8")


class BarkNotifier:
    def __init__(self, config: BarkConfig, *, client: httpx.AsyncClient | None = None) -> None:
        config.validate()
        self.config = config
        self.client = client

    def build_payload(self, title: str, body: str, url: str | None) -> dict[str, Any]:
        c = self.config
        content: dict[str, Any] = {"title": title, "body": body, "group": c.group, "level": c.level}
        if c.level == "critical":
            content["volume"] = str(max(0, min(10, c.volume)))
        if c.call:
            content["call"] = "1"
        if c.sound:
            content["sound"] = c.sound
        if url:
            content["url"] = url
        if c.encryption_key is None:
            return {"device_key": c.device_key, **content}
        ciphertext, iv = encrypt_gcm(c.encryption_key, json.dumps(content))
        # The app replaces the payload with the decrypted map, so all options live inside it.
        return {"device_key": c.device_key, "ciphertext": ciphertext, "iv": iv}

    async def send(self, title: str, body: str, url: str | None = None) -> None:
        payload = self.build_payload(title, body, url)
        client = self.client or httpx.AsyncClient(timeout=10)
        try:
            resp = await client.post(self.config.server.rstrip("/") + "/push", json=payload)
        except httpx.HTTPError as exc:
            raise BarkError(f"Bark server unreachable: {exc}") from exc
        finally:
            if self.client is None:
                await client.aclose()
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or data.get("code", 200) != 200:
            raise BarkError(
                f"Bark push failed ({resp.status_code}): {data.get('message', resp.text[:200])}"
            )
