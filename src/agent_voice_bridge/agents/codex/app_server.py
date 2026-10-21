"""Minimal client for ``codex app-server`` (JSON-RPC-like messages, one JSON object per line).

Protocol facts this relies on, checked against openai/codex ``codex-rs/app-server-protocol``:

* Messages omit the ``"jsonrpc": "2.0"`` field.
* Handshake: ``initialize`` request with ``clientInfo``, then ``initialized`` notification.
* The server asks for approvals with server-to-client requests
  ``item/commandExecution/requestApproval`` (params include ``threadId``, ``turnId``,
  ``itemId``, ``command``, ``cwd``, ``reason``) and ``item/fileChange/requestApproval``
  (``threadId``, ``turnId``, ``itemId``, ``reason``, ``grantRoot``). The client answers
  with ``{"decision": "accept" | "decline" | "cancel" | ...}``.
* File-change details arrive earlier in an ``item/started`` notification whose item has
  ``type: "fileChange"`` and ``changes: [{path, kind, diff}]``.
* ``serverRequest/resolved`` tells the client a pending request was settled elsewhere.

This client only sees approvals for threads it started itself. It cannot intercept
approvals owned by another app-server process, such as the one the VS Code
extension runs; use the PermissionRequest hook (``avb-hook --agent codex``) for those.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

log = logging.getLogger(__name__)

ServerRequestHandler = Callable[[str, dict[str, Any], "CodexAppServer"], Awaitable[dict[str, Any]]]
NotificationHandler = Callable[[str, dict[str, Any]], None]

APPROVAL_METHODS = ("item/commandExecution/requestApproval", "item/fileChange/requestApproval")


class AppServerError(Exception):
    pass


class CodexAppServer:
    def __init__(
        self,
        command: Sequence[str] = ("codex", "app-server"),
        *,
        on_request: ServerRequestHandler,
        on_notification: NotificationHandler | None = None,
        cwd: str | None = None,
    ) -> None:
        self.command = list(command)
        self.on_request = on_request
        self.on_notification = on_notification
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._handlers: dict[Any, asyncio.Task[None]] = {}
        self._write_lock = asyncio.Lock()
        self.file_changes: dict[str, list[dict[str, Any]]] = {}  # itemId -> changes
        self.commands: dict[str, dict[str, Any]] = {}  # itemId -> commandExecution item
        self.resolved: set[str] = set()  # str(requestId) settled by the server

    async def start(
        self, client_name: str = "agent_voice_bridge", version: str = "0.1.0"
    ) -> dict[str, Any]:
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            cwd=self.cwd,
            limit=16 * 1024 * 1024,
        )
        self._reader = asyncio.create_task(self._read_loop())
        result = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": client_name,
                    "title": "Agent Voice Bridge",
                    "version": version,
                }
            },
        )
        await self.notify("initialized", {})
        return result  # type: ignore[no-any-return]

    async def close(self) -> None:
        for task in list(self._handlers.values()):
            task.cancel()
        if self.proc and self.proc.returncode is None:
            if self.proc.stdin:
                self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader

    async def _send(self, message: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise AppServerError("app-server is not running")
        async with self._write_lock:
            self.proc.stdin.write((json.dumps(message) + "\n").encode())
            await self.proc.stdin.drain()

    async def request(
        self, method: str, params: dict[str, Any], *, timeout: float | None = 120
    ) -> Any:
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"method": method, "params": params})

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while line := await self.proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("ignoring non-JSON line from app-server")
                    continue
                if not isinstance(msg, dict):
                    continue
                self._dispatch(msg)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AppServerError("app-server exited"))

    def _dispatch(self, msg: dict[str, Any]) -> None:
        method, mid = msg.get("method"), msg.get("id")
        if method is None and mid is not None:  # response to one of our requests
            fut = self._pending.get(mid) if isinstance(mid, int) else None
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(AppServerError(json.dumps(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
            return
        params = msg.get("params") or {}
        if not isinstance(method, str):
            return
        if mid is not None:  # server-to-client request
            self._handlers[mid] = asyncio.create_task(self._handle_request(mid, method, params))
            return
        self._track(method, params)
        if self.on_notification is not None:
            self.on_notification(method, params)

    def _track(self, method: str, params: dict[str, Any]) -> None:
        if method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            if item.get("type") == "fileChange" and isinstance(item.get("changes"), list):
                self.file_changes[str(item.get("id"))] = item["changes"]
            elif item.get("type") == "commandExecution":
                self.commands[str(item.get("id"))] = item
        elif method == "serverRequest/resolved":
            rid = params.get("requestId")
            self.resolved.add(str(rid))
            task = self._handlers.get(rid)
            if task is not None and not task.done():
                task.cancel()

    async def _handle_request(self, mid: Any, method: str, params: dict[str, Any]) -> None:
        try:
            result = await self.on_request(method, params, self)
            await self._send({"id": mid, "result": result})
        except asyncio.CancelledError:
            log.info("server request %s was resolved elsewhere", mid)
        except Exception as exc:
            log.exception("handler for %s failed", method)
            with contextlib.suppress(AppServerError):
                await self._send({"id": mid, "error": {"code": -32603, "message": str(exc)}})
        finally:
            self._handlers.pop(mid, None)
