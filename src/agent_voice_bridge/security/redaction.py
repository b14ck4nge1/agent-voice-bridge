"""Best-effort removal of secrets before anything is stored, logged or spoken.

Redaction is defence in depth, not a guarantee: a secret in an unusual format can
slip through. The bridge therefore also avoids storing the unredacted tool input at
all (only its fingerprint).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|auth(orization)?|cookie|private[_-]?key|credential)",
    re.IGNORECASE,
)

_Replacer = Callable[[re.Match[str]], str]


def _whole(_: re.Match[str]) -> str:
    return REDACTED


def _keep(*groups: int, sep: str = "") -> _Replacer:
    """Keep the listed groups (e.g. the key name) and replace the rest with REDACTED."""

    def repl(m: re.Match[str]) -> str:
        return sep.join(m.group(g) for g in groups) + REDACTED

    return repl


_RULES: list[tuple[re.Pattern[str], _Replacer]] = [
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        _whole,
    ),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"), _whole),  # Anthropic / OpenAI keys
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), _whole),  # GitHub tokens
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), _whole),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _whole),  # AWS access key id
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), _whole),  # Slack
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), _whole),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-~+/]{16,}=*"), _keep(1)),
    # KEY=value, KEY: value
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY)[A-Z0-9_]*\s*[=:]\s*)"
            r"(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        _keep(1),
    ),
    # --password value, --token=value
    (re.compile(r"(?i)(--?(?:password|passwd|token|secret|api-key)(?:\s+|=))(\S+)"), _keep(1)),
    # scheme://user:password@host
    (
        re.compile(r"(://[^/\s:@]+:)([^@\s/]+)(@)"),
        lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}",
    ),
]


def redact_text(text: str) -> str:
    out = text
    for pattern, repl in _RULES:
        out = pattern.sub(repl, out)
    return out


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact strings and values under sensitive-looking keys."""
    if _depth > 20:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if _SENSITIVE_KEY.search(key) and isinstance(v, str | int | float) and v != "":
                result[key] = REDACTED
            else:
                result[key] = redact(v, _depth=_depth + 1)
        return result
    if isinstance(value, list | tuple):
        return [redact(v, _depth=_depth + 1) for v in value]
    return value
