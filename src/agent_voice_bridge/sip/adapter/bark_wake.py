"""CallDispatcher that wakes the phone with Bark; the phone then calls the bridge.

"Placing a call" here means: send a ringing Bark notification whose tap action is a
``linphone-sip:`` link to the bridge's SIP server. When you tap it, Linphone dials
in and ``PhoneCallHandler`` runs the conversation. Reminders repeat the notification
while the request is still waiting for a call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from agent_voice_bridge.models import AgentKind, ApprovalState, ApprovalView
from agent_voice_bridge.notify.bark import BarkError, BarkNotifier

log = logging.getLogger(__name__)

AGENT_NAMES = {
    AgentKind.CLAUDE_CODE: "Claude Code",
    AgentKind.CODEX: "Codex",
    AgentKind.TEST: "Test agent",
}


def dial_url(host: str, port: int = 5060, transport: str = "udp", user: str = "avb") -> str:
    default = 5061 if transport == "tls" else 5060
    hostport = host if port == default else f"{host}:{port}"
    suffix = f";transport={transport}" if transport in ("tcp", "tls") else ""
    return f"linphone-sip:{user}@{hostport}{suffix}"


class BarkWakeDispatcher:
    def __init__(
        self,
        notifier: BarkNotifier,
        url: str,
        *,
        reminder_seconds: float = 60.0,
        max_reminders: int = 2,
        coalesce_seconds: float = 10.0,
        send_attempts: int = 3,
    ) -> None:
        self.notifier = notifier
        self.url = url
        self.reminder_seconds = reminder_seconds
        self.max_reminders = max_reminders
        self.coalesce_seconds = coalesce_seconds
        self.send_attempts = send_attempts
        self.state_of: Callable[[str], ApprovalState | None] = lambda _: None
        self._last_sent = 0.0
        self._reminders: dict[str, asyncio.Task[None]] = {}

    def attach(self, state_of: Callable[[str], ApprovalState | None]) -> None:
        self.state_of = state_of

    async def _send(self, request: ApprovalView, *, reminder: bool) -> None:
        agent = AGENT_NAMES.get(request.agent, str(request.agent))
        title = "Reminder: approval waiting" if reminder else "Approval needed"
        body = f"{agent} · {request.project_name} · {request.risk.value} risk. Tap to call."
        last: BarkError | None = None
        for attempt in range(self.send_attempts):
            try:
                await self.notifier.send(title, body, self.url)
                self._last_sent = time.monotonic()
                return
            except BarkError as exc:
                last = exc
                await asyncio.sleep(0.5 * 2**attempt)
        assert last is not None
        raise last

    async def request_call(self, request: ApprovalView) -> None:
        if time.monotonic() - self._last_sent >= self.coalesce_seconds:
            # Failures propagate, so the request fails (and the agent is denied).
            await self._send(request, reminder=False)
        else:
            log.info(
                "Bark sent %.0fs ago; one call covers %s too",
                time.monotonic() - self._last_sent,
                request.id,
            )
        old = self._reminders.pop(request.id, None)
        if old:
            old.cancel()
        if self.max_reminders > 0:
            self._reminders[request.id] = asyncio.get_running_loop().create_task(
                self._remind(request)
            )

    async def _remind(self, request: ApprovalView) -> None:
        for _ in range(self.max_reminders):
            await asyncio.sleep(self.reminder_seconds)
            if self.state_of(request.id) != ApprovalState.PENDING_CALL:
                return
            if time.monotonic() - self._last_sent < self.coalesce_seconds:
                continue
            try:
                await self._send(request, reminder=True)
            except BarkError as exc:
                log.warning("Bark reminder failed: %s", exc)
        self._reminders.pop(request.id, None)

    async def cancel_call(self, request_id: str, call_id: str | None) -> None:
        task = self._reminders.pop(request_id, None)
        if task:
            task.cancel()

    def close(self) -> None:
        for task in self._reminders.values():
            task.cancel()
        self._reminders.clear()
