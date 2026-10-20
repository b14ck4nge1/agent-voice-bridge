"""Runtime configuration, loaded only from environment variables.

Secrets are never given defaults. A missing token disables the role that needs it
instead of falling back to something guessable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class HighRiskPolicy(StrEnum):
    """How to treat approvals classified as high or critical risk."""

    TOTP = "totp"  # approve only with a valid spoken TOTP code
    DENY = "deny"  # high-risk requests can be rejected by voice but never approved
    VOICE = "voice"  # voice confirmation alone (not recommended)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return default if raw is None else int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "AgentVoiceBridge"
    return (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "agent-voice-bridge"
    )


def load_env_file(path: Path) -> bool:
    """Set variables from a KEY=VALUE file without overriding the real environment."""
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return True


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    db_path: Path = field(default_factory=lambda: default_data_dir() / "bridge.sqlite3")
    agent_token: str | None = None
    voice_token: str | None = None
    default_ttl_seconds: int = 300
    max_ttl_seconds: int = 3600
    max_call_attempts: int = 2
    redial_delay_seconds: float = 20.0
    high_risk_policy: HighRiskPolicy = HighRiskPolicy.TOTP
    totp_secret: str | None = None
    sweep_interval_seconds: float = 2.0
    call_dispatcher: str = "log"  # "log", "simulated" (tests), later "sip"
    sip_target_uri: str | None = None
    allow_remote_bind: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        db = _env("AVB_DB_PATH")
        return cls(
            host=_env("AVB_HOST", "127.0.0.1") or "127.0.0.1",
            port=_env_int("AVB_PORT", 8765),
            db_path=Path(db) if db else default_data_dir() / "bridge.sqlite3",
            agent_token=_env("AVB_AGENT_TOKEN"),
            voice_token=_env("AVB_VOICE_TOKEN"),
            default_ttl_seconds=_env_int("AVB_DEFAULT_TTL_SECONDS", 300),
            max_ttl_seconds=_env_int("AVB_MAX_TTL_SECONDS", 3600),
            max_call_attempts=_env_int("AVB_MAX_CALL_ATTEMPTS", 2),
            redial_delay_seconds=float(_env("AVB_REDIAL_DELAY_SECONDS", "20") or "20"),
            high_risk_policy=HighRiskPolicy(_env("AVB_HIGH_RISK_POLICY") or "totp"),
            totp_secret=_env("AVB_TOTP_SECRET"),
            call_dispatcher=_env("AVB_CALL_DISPATCHER", "log") or "log",
            sip_target_uri=_env("AVB_SIP_TARGET_URI"),
            allow_remote_bind=_env_bool("AVB_ALLOW_REMOTE_BIND", False),
        )

    def validate(self) -> None:
        if not self.agent_token or not self.voice_token:
            raise ValueError("AVB_AGENT_TOKEN and AVB_VOICE_TOKEN must both be set")
        if self.agent_token == self.voice_token:
            raise ValueError("AVB_AGENT_TOKEN and AVB_VOICE_TOKEN must differ")
        for name, tok in (
            ("AVB_AGENT_TOKEN", self.agent_token),
            ("AVB_VOICE_TOKEN", self.voice_token),
        ):
            if len(tok) < 32:
                raise ValueError(f"{name} must be at least 32 characters")
        if self.host not in {"127.0.0.1", "::1", "localhost"} and not self.allow_remote_bind:
            raise ValueError(
                "Refusing to bind to a non-loopback address without AVB_ALLOW_REMOTE_BIND=1; "
                "put HTTPS in front of the bridge first (see docs/security.md)"
            )
        if not (1 <= self.default_ttl_seconds <= self.max_ttl_seconds):
            raise ValueError("AVB_DEFAULT_TTL_SECONDS must be between 1 and AVB_MAX_TTL_SECONDS")
