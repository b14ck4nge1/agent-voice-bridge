"""Implementations of :class:`BridgeOps`: in-process and over the voice HTTP API."""

from __future__ import annotations

from typing import Any

import httpx

from agent_voice_bridge.approvals.service import ApprovalService, ServiceError
from agent_voice_bridge.models import ApprovalView, CallEvent, DecisionSubmit


class InProcessOps:
    def __init__(self, service: ApprovalService) -> None:
        self.service = service

    async def get(self, request_id: str) -> ApprovalView:
        return self.service.get(request_id)

    async def call_event(self, request_id: str, event: CallEvent) -> ApprovalView:
        return await self.service.call_event(request_id, event)

    async def decide(self, request_id: str, decision: DecisionSubmit) -> ApprovalView:
        return await self.service.decide(request_id, decision)


class HttpOps:
    """Talks to a running bridge with the voice token."""

    def __init__(
        self, base_url: str, voice_token: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=15)
        self.headers = {"Authorization": f"Bearer {voice_token}"}

    async def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        resp = await self.client.request(method, path, json=body, headers=self.headers)
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            raise ServiceError(
                resp.status_code, data.get("error", "http_error"), data.get("detail", resp.text)
            )
        return resp.json()

    async def pending(self) -> list[ApprovalView]:
        return [
            ApprovalView.model_validate(r) for r in await self._send("GET", "/v1/voice/pending")
        ]

    async def get(self, request_id: str) -> ApprovalView:
        return ApprovalView.model_validate(
            await self._send("GET", f"/v1/voice/approvals/{request_id}")
        )

    async def call_event(self, request_id: str, event: CallEvent) -> ApprovalView:
        data = await self._send(
            "POST", f"/v1/voice/approvals/{request_id}/call-events", event.model_dump()
        )
        return ApprovalView.model_validate(data)

    async def decide(self, request_id: str, decision: DecisionSubmit) -> ApprovalView:
        data = await self._send(
            "POST", f"/v1/voice/approvals/{request_id}/decision", decision.model_dump()
        )
        return ApprovalView.model_validate(data)

    async def aclose(self) -> None:
        await self.client.aclose()
