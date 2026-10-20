"""Request fingerprinting.

The fingerprint binds a decision to the exact operation that was presented: agent,
session, workspace, tool and full (unredacted) tool input. A decision carrying a
different fingerprint is rejected, so a decision cannot be replayed onto another
request or onto modified arguments.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def request_fingerprint(
    *,
    agent: str,
    session_id: str,
    workspace: str,
    tool_name: str,
    tool_input: dict[str, Any],
    kind: str,
) -> str:
    payload = {
        "v": 1,
        "agent": agent,
        "kind": kind,
        "session_id": session_id,
        "workspace": workspace,
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
