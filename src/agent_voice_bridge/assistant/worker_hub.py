"""Server side of the PC helper (``avb-worker``): a job queue the helper long-polls.

The PC connects out to the server (no inbound ports on the PC). The phone assistant
queues a job (start a Claude Code task, report on one, ...) and awaits its result.
Finished background tasks are reported back as events, so the assistant can call
you with the result.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

ONLINE_SECONDS = 75.0  # the helper polls at least every ~25 s
JOB_KINDS = {"start_task", "continue_task", "list_tasks", "task_report"}


class WorkerOffline(Exception):
    pass


@dataclass
class _Job:
    id: str
    kind: str
    args: dict[str, Any]
    result: asyncio.Future[dict[str, Any]]
    created: float = field(default_factory=time.monotonic)
    sent: bool = False


class WorkerHub:
    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._last_poll = 0.0
        self.on_task_finished: Callable[[dict[str, Any]], None] | None = None

    @property
    def online(self) -> bool:
        return time.monotonic() - self._last_poll < ONLINE_SECONDS

    async def run(self, kind: str, args: dict[str, Any], *, timeout: float = 30) -> dict[str, Any]:
        """Queue a job for the PC and wait for its result."""
        if kind not in JOB_KINDS:
            raise ValueError(kind)
        if not self.online:
            raise WorkerOffline("the helper on your PC is not connected")
        job = _Job(
            f"job_{secrets.token_hex(8)}", kind, args, asyncio.get_running_loop().create_future()
        )
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        try:
            return await asyncio.wait_for(asyncio.shield(job.result), timeout)
        except TimeoutError as exc:
            raise WorkerOffline("the helper on your PC did not answer in time") from exc
        finally:
            if job.result.done():
                self._jobs.pop(job.id, None)

    async def poll(self, wait: float = 25) -> list[dict[str, Any]]:
        """Called by the PC helper. Returns queued jobs (waits up to ``wait`` s for one)."""
        self._last_poll = time.monotonic()
        out: list[dict[str, Any]] = []
        try:
            first = await asyncio.wait_for(self._queue.get(), wait)
        except TimeoutError:
            self._last_poll = time.monotonic()
            return out
        ids = [first]
        while not self._queue.empty():
            ids.append(self._queue.get_nowait())
        for jid in ids:
            job = self._jobs.get(jid)
            if job is None or job.result.done():
                continue
            job.sent = True
            out.append({"id": job.id, "kind": job.kind, "args": job.args})
        self._last_poll = time.monotonic()
        return out

    def complete(self, job_id: str, result: dict[str, Any]) -> bool:
        job = self._jobs.pop(job_id, None)
        if job is None or job.result.done():
            return False
        job.result.set_result(result)
        return True

    def task_finished(self, event: dict[str, Any]) -> None:
        """The helper reports that a background task ended (done, failed, needs input)."""
        log.info("PC task %s finished: %s", event.get("task_id"), event.get("status"))
        if self.on_task_finished is not None:
            try:
                self.on_task_finished(event)
            except Exception:
                log.exception("task-finished handler failed")
