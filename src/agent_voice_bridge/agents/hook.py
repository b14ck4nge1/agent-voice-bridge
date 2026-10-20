"""``avb-hook``: PermissionRequest hook for Claude Code and OpenAI Codex.

Both agents send the same core fields on stdin (``hook_event_name``,
``session_id``, ``cwd``, ``tool_name``, ``tool_input``) and accept the same
answer on stdout::

    {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                            "decision": {"behavior": "allow" | "deny", "message": "..."}}}

The hook never sends ``updatedInput`` or ``updatedPermissions``: an approval covers
this one call, unmodified, and adds no rules for future calls. (Codex also fails
closed if those fields are present.)

On any failure (bridge down, timeout, bad input) the default is to deny. With
``--on-failure ask`` the hook prints nothing instead, so the agent's own local
permission prompt appears; that is never an approval either.

Standard library only, so it starts fast and has nothing to break.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from agent_voice_bridge.config import default_data_dir, load_env_file

AGENTS = {"claude-code": "claude_code", "codex": "codex"}
DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_WAIT_SECONDS = 300
POLL_SECONDS = 25


class BridgeError(Exception):
    pass


@dataclass
class Outcome:
    behavior: str | None  # "allow", "deny", or None for "no decision; let the agent ask locally"
    message: str | None = None


def _check_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return url.rstrip("/")
    if parsed.scheme == "http":
        host = parsed.hostname or ""
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if loopback:
            return url.rstrip("/")
    raise BridgeError("AVB_BRIDGE_URL must be https://, or http:// on a loopback address")


class BridgeClient:
    def __init__(self, base_url: str, token: str, *, timeout_margin: float = 10.0) -> None:
        self.base = _check_url(base_url)
        self.token = token
        self.margin = timeout_margin

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        timeout: float,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 - scheme validated in _check_url
            self.base + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                **headers,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                payload = {}
            return exc.code, payload

    def create(self, payload: dict[str, Any], *, attempts: int = 3) -> dict[str, Any]:
        last: Exception | None = None
        for i in range(attempts):
            try:
                status, body = self._call("POST", "/v1/approvals", payload, timeout=10, headers={})
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last = exc
                time.sleep(0.5 * (2**i))
                continue
            if status in (200, 201):
                return body["request"]  # type: ignore[no-any-return]
            raise BridgeError(
                f"bridge refused the request ({status}): {body.get('detail') or body.get('error')}"
            )
        raise BridgeError(f"bridge unreachable: {last}")

    def wait(self, request_id: str, secret: str, seconds: float) -> dict[str, Any]:
        path = f"/v1/approvals/{urllib.parse.quote(request_id)}/wait?timeout={seconds:.0f}"
        status, body = self._call(
            "GET",
            path,
            None,
            timeout=seconds + self.margin,
            headers={"X-AVB-Request-Secret": secret},
        )
        if status != 200:
            raise BridgeError(f"wait failed ({status}): {body.get('detail') or body.get('error')}")
        return body

    def cancel(self, request_id: str, secret: str, reason: str) -> None:
        path = f"/v1/approvals/{urllib.parse.quote(request_id)}/cancel"
        self._call(
            "POST", path, {"reason": reason}, timeout=5, headers={"X-AVB-Request-Secret": secret}
        )


def build_request(
    hook_input: dict[str, Any], agent: str, wait_seconds: int
) -> tuple[dict[str, Any], str]:
    if hook_input.get("hook_event_name") != "PermissionRequest":
        raise BridgeError(f"unsupported hook event {hook_input.get('hook_event_name')!r}")
    for field in ("session_id", "cwd", "tool_name"):
        if not isinstance(hook_input.get(field), str) or not hook_input[field]:
            raise BridgeError(f"hook input is missing {field}")
    tool_input = hook_input.get("tool_input")
    if tool_input is None:
        tool_input = {}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}
    secret = secrets.token_urlsafe(32)
    payload = {
        "agent": AGENTS[agent],
        "kind": "tool_permission",
        "session_id": hook_input["session_id"],
        "workspace": hook_input["cwd"],
        "tool_name": hook_input["tool_name"],
        "tool_input": tool_input,
        "turn_id": hook_input.get("turn_id")
        if isinstance(hook_input.get("turn_id"), str)
        else None,
        # Only the Codex app-server path supplies this; the bridge treats it as untrusted.
        "reason": hook_input["reason"][:2000]
        if isinstance(hook_input.get("reason"), str)
        else None,
        "idempotency_key": str(uuid.uuid4()),
        "request_secret": secret,
        "ttl_seconds": wait_seconds,
    }
    return payload, secret


def decide(
    hook_input: dict[str, Any],
    *,
    agent: str,
    client: BridgeClient,
    wait_seconds: int,
    clock: Any = time.monotonic,
) -> Outcome:
    payload, secret = build_request(hook_input, agent, wait_seconds)
    deadline = clock() + wait_seconds
    created = client.create(payload)
    request_id = created["id"]
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            client.cancel(request_id, secret, "hook deadline reached")
            raise BridgeError("no decision before the hook deadline")
        result = client.wait(request_id, secret, min(POLL_SECONDS, max(1.0, remaining)))
        if not result.get("final"):
            continue
        state = result.get("state")
        message = result.get("message")
        if state == "APPROVED" and result.get("decision") == "approve":
            return Outcome("allow")
        if state == "REJECTED":
            text = "The user rejected this request by phone."
            if message:
                text += f" Their feedback: {message}"
            return Outcome("deny", text)
        raise BridgeError(f"request ended as {state}")


def render(outcome: Outcome) -> str | None:
    if outcome.behavior is None:
        return None
    decision: dict[str, Any] = {"behavior": outcome.behavior}
    if outcome.behavior == "deny":
        decision["message"] = outcome.message or "Denied by Agent Voice Bridge."
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}
    )


def _finish(outcome: Outcome) -> NoReturn:
    text = render(outcome)
    if text is not None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    raise SystemExit(0)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="avb-hook", description=__doc__.split("\n\n")[0])
    parser.add_argument("--agent", choices=sorted(AGENTS), required=True)
    parser.add_argument(
        "--on-failure", choices=["deny", "ask"], default=os.environ.get("AVB_ON_FAILURE", "deny")
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=int(os.environ.get("AVB_WAIT_SECONDS", DEFAULT_WAIT_SECONDS)),
        help="Give up after this long; must be shorter than the hook timeout configured in the agent",
    )
    args = parser.parse_args(argv)

    def failure(reason: str) -> NoReturn:
        sys.stderr.write(f"avb-hook: {reason}\n")
        if args.on_failure == "ask":
            _finish(Outcome(None))
        _finish(
            Outcome(
                "deny",
                f"Agent Voice Bridge could not get a decision ({reason}); denied by default.",
            )
        )

    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        hook_input = json.loads(raw)
        if not isinstance(hook_input, dict):
            raise ValueError("hook input is not a JSON object")
    except (ValueError, UnicodeDecodeError) as exc:
        failure(f"invalid hook input: {exc}")

    load_env_file(Path(os.environ.get("AVB_HOOK_ENV", default_data_dir() / "hook.env")))
    token = os.environ.get("AVB_AGENT_TOKEN", "")
    if not token:
        failure("AVB_AGENT_TOKEN is not set")
    try:
        client = BridgeClient(os.environ.get("AVB_BRIDGE_URL", DEFAULT_URL), token)
        _finish(decide(hook_input, agent=args.agent, client=client, wait_seconds=args.wait_seconds))
    except BridgeError as exc:
        failure(str(exc))
    except Exception as exc:  # never let an unexpected error become an implicit allow
        failure(f"unexpected error: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
