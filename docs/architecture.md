# Architecture

## Overview

```
 Windows PC                                                           iPhone
 ┌──────────────────────────────────────────────────────────┐
 │ Claude Code ──PermissionRequest hook──┐                  │
 │ Codex (CLI/IDE) ─PermissionRequest hook┼─► avb-hook ─HTTP─┼─┐
 │ avb-codex ─codex app-server JSON-RPC───┘  (agent token +  │ │
 │                                           request secret) │ │
 │                                                           │ │
 │  ┌─────────────── Approval bridge (FastAPI) ◄────────────┼─┘
 │  │ state machine · SQLite · auth · expiry · audit │      │
 │  └───────┬──────────────────────────▲─────────────┘      │
 │          │ CallDispatcher           │ voice token        │
 │          ▼                          │ (call events,      │
 │  ┌──────────────────┐   ┌───────────┴───────┐ decisions)  │
 │  │ SIP user agent   │◄─►│ Conversation      │            │
 │  │ TLS · SRTP · ICE │   │ engine            │            │
 │  └────────┬─────────┘   │ STT → intents → TTS│            │
 │           │             └───────────────────┘            │
 └───────────┼──────────────────────────────────────────────┘
             │ SIP/TLS + SRTP over the internet
             ▼
   sip.linphone.org (Flexisip) ──PushKit VoIP push──► Linphone app ─► CallKit
```

In this snapshot, the SIP user agent and speech are **not implemented**. The text
simulator `avb-voice-sim` stands in for the call, driving the same conversation
engine over the same voice API.

## Components and code locations

| Concern | Location | Notes |
|---|---|---|
| Configuration | `src/agent_voice_bridge/config.py` | Environment only; secrets have no defaults. |
| Models | `src/agent_voice_bridge/models.py` | Pydantic, `extra="forbid"` on inputs. |
| State machine | `approvals/state_machine.py` | Explicit transition table. |
| Business logic | `approvals/service.py` | Create, wait, cancel, call events, decide, expiry, restart recovery. |
| Persistence | `database/store.py` | SQLite (WAL); a transaction per state change; audit log. |
| Security | `security/` | Tokens and roles, fingerprint, redaction, TOTP, risk rules. |
| HTTP API | `api/app.py`, `api/server.py` | `avb-bridge`. |
| Agent hook (Claude + Codex) | `agents/hook.py`, `agents/install.py` | `avb-hook`, `avb-admin install-hooks`. Standard library only. |
| Codex app-server | `agents/codex/` | `avb-codex run`. |
| Conversation | `voice/conversation/` | Intent rules, dialog manager, `BridgeOps` adapters. |
| Speech interfaces | `voice/stt/`, `voice/tts/` | Interfaces only so far. |
| Call transport boundary | `sip/adapter/base.py` | `CallDispatcher` protocol; `LoggingDispatcher`. |

The brief's suggested `backend/`, `agents/`, `voice/`, `sip/` folders map to
subpackages of the single `agent_voice_bridge` package (src layout). One
installable package keeps Windows setup to a single `pip install`. The boundaries
are kept by imports: `approvals` never imports `voice` or `sip`; `voice` reaches the
bridge only through `BridgeOps`.

## Request lifecycle

```
CREATED ─► PENDING_CALL ─► RINGING ─► IN_CALL ─► AWAITING_DECISION ─► APPROVED
              ▲   │           │          │              │          └─► REJECTED
              │   │           │ no answer│ hang up      │ hang up
              └───┴───────────┴──────────┴──────────────┘
any non-terminal state ─► CANCELLED (agent) | EXPIRED (TTL) | FAILED (attempts exhausted, transport error)
```

* Decisions are accepted **only** in `AWAITING_DECISION`, which the call reaches
  after the assistant has read out the request (`presented` event).
* `APPROVED` is the only state that makes `avb-hook` return `allow`. Every other
  terminal state returns `deny`.
* A missed or dropped call returns the request to `PENDING_CALL` for a redial,
  until `AVB_MAX_CALL_ATTEMPTS` is reached; then it is `FAILED`.
* On bridge restart, calls in progress are lost: those requests return to
  `PENDING_CALL` and are redialled; expired ones are expired.

## Sequence: Claude Code approval

1. Claude Code is about to show a permission prompt and runs
   `avb-hook --agent claude-code` with the hook JSON on stdin.
2. The hook generates a random **request secret** and **idempotency key**, then
   `POST /v1/approvals` with the agent token.
3. The bridge fingerprints the exact request (agent, session, workspace, tool, full
   input), stores only a **redacted** copy of the input, rates risk, and asks the
   `CallDispatcher` to call.
4. The hook long-polls `GET /v1/approvals/{id}/wait` (secret in a header) until the
   request is final or its own deadline passes.
5. The call side posts `dialing` → `ringing` → `answered`, the assistant presents
   the request (`presented`), the user decides and confirms, and the voice side
   posts the decision with the fingerprint, call id and a fresh nonce.
6. The hook prints `{"hookSpecificOutput": {"hookEventName": "PermissionRequest",
   "decision": {"behavior": "allow"}}}` (or `deny` with the user's feedback), and
   Claude Code proceeds through its normal permission path.

Codex via hook works the same way. Via `avb-codex`, `CodexApprovalRouter` turns
`item/*/requestApproval` into the same bridge request and answers `accept` or
`decline`.

## Why these boundaries

* **The hook is the agent's own extension point.** No terminal scraping or keystroke
  injection, and it fails closed (ADR-0003).
* **Roles are split.** The agent token can create requests but not decide them; the
  voice token can decide but not create. A compromised or confused voice
  assistant cannot fabricate an approval request.
* **Approval logic doesn't know about SIP.** `CallDispatcher` is the only
  interface, so Asterisk can replace the Python UA (ADR-0002).
