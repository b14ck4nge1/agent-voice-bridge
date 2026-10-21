"""FastAPI application exposing the approval bridge."""

# No `from __future__ import annotations`: FastAPI must evaluate the dependency
# annotations below, which refer to local variables.

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from agent_voice_bridge import __version__
from agent_voice_bridge.approvals.service import ApprovalService, ServiceError
from agent_voice_bridge.assistant.worker_hub import WorkerHub
from agent_voice_bridge.config import Settings
from agent_voice_bridge.database.store import Store
from agent_voice_bridge.models import (
    ApprovalCreate,
    ApprovalView,
    CallEvent,
    DecisionSubmit,
    WaitResult,
)
from agent_voice_bridge.security.auth import Role, role_for_token
from agent_voice_bridge.sip.adapter.base import CallDispatcher, LoggingDispatcher

log = logging.getLogger(__name__)

MAX_WAIT_SECONDS = 55.0


class CreatedResponse(BaseModel):
    request: ApprovalView
    created: bool


class CancelBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class WorkerResult(BaseModel):
    result: dict[str, Any]


class WorkerEvent(BaseModel):
    task_id: str = Field(max_length=200)
    status: str = Field(max_length=50)
    title: str | None = Field(default=None, max_length=500)
    summary: str | None = Field(default=None, max_length=8000)


def create_app(
    settings: Settings,
    *,
    dispatcher: CallDispatcher | None = None,
    store: Store | None = None,
    extension: Callable[[ApprovalService], AbstractAsyncContextManager[None]] | None = None,
    worker_hub: WorkerHub | None = None,
) -> FastAPI:
    """``extension`` runs for the app's lifetime with the service, e.g. the SIP server."""
    settings.validate()
    store = store or Store(settings.db_path)
    service = ApprovalService(store, settings, dispatcher or LoggingDispatcher())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await service.recover_after_restart()

        async def sweeper() -> None:
            while True:
                await asyncio.sleep(settings.sweep_interval_seconds)
                try:
                    await service.sweep_expired()
                except Exception:
                    log.exception("expiry sweep failed")

        task = asyncio.create_task(sweeper())
        try:
            if extension is None:
                yield
            else:
                async with extension(service):
                    yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await service.shutdown()

    app = FastAPI(
        title="Agent Voice Bridge",
        version=__version__,
        description="Phone-call approvals for AI coding agents. All endpoints except /healthz "
        "require a bearer token.",
        lifespan=lifespan,
    )
    app.state.service = service
    bearer = HTTPBearer(auto_error=False)

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error": exc.code, "detail": exc.message}
        )

    def require(role: Role) -> Callable[[HTTPAuthorizationCredentials | None], Role]:
        def dep(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Role:
            if creds is None or creds.scheme.lower() != "bearer":
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
            found = role_for_token(
                creds.credentials,
                agent_token=settings.agent_token,
                voice_token=settings.voice_token,
            )
            if found is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
            if found != role:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, "token not allowed for this endpoint"
                )
            return found

        return dep

    agent_auth = Depends(require(Role.AGENT))
    voice_auth = Depends(require(Role.VOICE))
    SecretHeader = Annotated[str, Header(alias="X-AVB-Request-Secret", min_length=32)]

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # -- agent endpoints ---------------------------------------------------------

    @app.post(
        "/v1/approvals", response_model=CreatedResponse, tags=["agent"], dependencies=[agent_auth]
    )
    async def create_approval(body: ApprovalCreate, response: Response) -> CreatedResponse:
        view, created = await service.create(body)
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return CreatedResponse(request=view, created=created)

    @app.get(
        "/v1/approvals/{request_id}/wait",
        response_model=WaitResult,
        tags=["agent"],
        dependencies=[agent_auth],
    )
    async def wait_approval(
        request_id: str,
        secret: SecretHeader,
        timeout: Annotated[float, Query(ge=0, le=MAX_WAIT_SECONDS)] = 25.0,
    ) -> WaitResult:
        return await service.wait(request_id, secret, timeout)

    @app.post(
        "/v1/approvals/{request_id}/cancel",
        response_model=ApprovalView,
        tags=["agent"],
        dependencies=[agent_auth],
    )
    async def cancel_approval(
        request_id: str, secret: SecretHeader, body: CancelBody | None = None
    ) -> ApprovalView:
        return await service.cancel(request_id, secret, body.reason if body else None)

    # -- voice endpoints ---------------------------------------------------------

    @app.get(
        "/v1/voice/pending",
        response_model=list[ApprovalView],
        tags=["voice"],
        dependencies=[voice_auth],
    )
    async def list_pending() -> list[ApprovalView]:
        return service.pending()

    @app.get(
        "/v1/voice/approvals/{request_id}",
        response_model=ApprovalView,
        tags=["voice"],
        dependencies=[voice_auth],
    )
    async def get_approval(request_id: str) -> ApprovalView:
        return service.get(request_id)

    @app.post(
        "/v1/voice/approvals/{request_id}/call-events",
        response_model=ApprovalView,
        tags=["voice"],
        dependencies=[voice_auth],
    )
    async def post_call_event(request_id: str, body: CallEvent) -> ApprovalView:
        return await service.call_event(request_id, body)

    @app.post(
        "/v1/voice/approvals/{request_id}/decision",
        response_model=ApprovalView,
        tags=["voice"],
        dependencies=[voice_auth],
    )
    async def post_decision(request_id: str, body: DecisionSubmit) -> ApprovalView:
        return await service.decide(request_id, body)

    # -- PC helper (avb-worker) endpoints: the agent token, like the hooks --------------

    if worker_hub is not None:
        hub = worker_hub

        @app.post("/v1/worker/poll", tags=["worker"], dependencies=[agent_auth])
        async def worker_poll(
            wait: Annotated[float, Query(ge=0, le=MAX_WAIT_SECONDS)] = 25.0,
        ) -> dict[str, Any]:
            return {"jobs": await hub.poll(wait)}

        @app.post("/v1/worker/jobs/{job_id}/result", tags=["worker"], dependencies=[agent_auth])
        async def worker_result(job_id: str, body: WorkerResult) -> dict[str, bool]:
            return {"accepted": hub.complete(job_id, body.result)}

        @app.post("/v1/worker/events", tags=["worker"], dependencies=[agent_auth])
        async def worker_event(body: WorkerEvent) -> dict[str, bool]:
            hub.task_finished(body.model_dump())
            return {"accepted": True}

    return app
