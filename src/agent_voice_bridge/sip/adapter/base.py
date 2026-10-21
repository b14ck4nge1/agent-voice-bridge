"""The boundary between approval logic and whatever places phone calls.

The approval service only knows this interface. A SIP user agent, an Asterisk ARI
controller or a test double can sit behind it without changing approval logic.
Implementations report progress back through the bridge's call-event API
(``ApprovalService.call_event``), never by changing request state directly.
"""

from __future__ import annotations

import logging
from typing import Protocol

from agent_voice_bridge.models import ApprovalView

log = logging.getLogger(__name__)


class CallDispatcher(Protocol):
    async def request_call(self, request: ApprovalView) -> None:
        """Start (or queue) a call about ``request``. Must return quickly."""

    async def cancel_call(self, request_id: str, call_id: str | None) -> None:
        """Hang up / stop dialling for a request that no longer needs a decision."""


class LoggingDispatcher:
    """Places no calls. Requests stay in PENDING_CALL until they expire or are cancelled.

    Useful for exercising the API and agent hooks before SIP is configured.
    """

    async def request_call(self, request: ApprovalView) -> None:
        log.warning(
            "No call transport configured; request %s (%s, risk %s) is waiting for a decision",
            request.id,
            request.tool_name,
            request.risk,
        )

    async def cancel_call(self, request_id: str, call_id: str | None) -> None:
        log.info("Cancel call for %s (call %s): nothing to do", request_id, call_id)
