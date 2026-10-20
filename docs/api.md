# Bridge API

The machine-readable spec is served at `/openapi.json`, and interactive docs at
`/docs`, while `avb-bridge` runs. All endpoints except `/healthz` need
`Authorization: Bearer <token>`. The **agent** token and the **voice** token are
accepted only on their own endpoints (403 otherwise).

## Agent endpoints (agent token)

### `POST /v1/approvals`

```json
{
  "agent": "claude_code",              // claude_code | codex | test
  "kind": "tool_permission",           // tool_permission | command_execution | file_change | question
  "session_id": "4f0c…",
  "workspace": "C:\\Users\\me\\frontend",
  "tool_name": "Bash",
  "tool_input": {"command": "npm install"},
  "turn_id": null,
  "reason": null,                      // optional agent-supplied text (untrusted)
  "idempotency_key": "uuid",
  "request_secret": "≥32 random chars",// needed later for wait/cancel; stored hashed
  "ttl_seconds": 300                   // optional, capped by AVB_MAX_TTL_SECONDS
}
```

`201` with `{"request": ApprovalView, "created": true}`. Repeating the same
`(agent, session_id, idempotency_key)` with the same secret and request returns
`200` and the existing request. With different content it returns
`409 idempotency_conflict`. Unknown fields: `422`.

### `GET /v1/approvals/{id}/wait?timeout=25`

Header `X-AVB-Request-Secret`. Long-polls up to 55 s and returns
`{"id", "state", "final", "decision", "message"}`. A wrong secret returns `404`.

### `POST /v1/approvals/{id}/cancel`

Header `X-AVB-Request-Secret`, optional body `{"reason": "..."}`.

## Voice endpoints (voice token)

* `GET /v1/voice/pending`: non-terminal, unexpired requests (redacted).
* `GET /v1/voice/approvals/{id}`
* `POST /v1/voice/approvals/{id}/call-events` with
  `{"event": "dialing|ringing|answered|presented|no_answer|hangup|failed", "call_id": "...", "detail": null}`.
  `dialing` binds `call_id` to the request; later events must use the same id.
* `POST /v1/voice/approvals/{id}/decision`:

```json
{"decision": "approve", "fingerprint": "<64 hex>", "call_id": "...", "nonce": "≥16 chars",
 "confirmed": true, "message": null, "totp_code": "123456"}
```

Error codes (JSON `{"error", "detail"}`): `bad_state` (409), `call_mismatch` (409),
`fingerprint_mismatch` (409), `replay` (409), `final` (409), `expired` (410),
`not_confirmed` (422), `second_factor_required` / `second_factor_invalid` /
`second_factor_unavailable` / `approval_forbidden` (403), `not_found` (404).

## ApprovalView

`id, agent, kind, session_id, workspace, project_name, tool_name, tool_input
(redacted), summary, reason, risk (low|medium|high|critical), risk_reasons,
fingerprint, state, created_at, expires_at, updated_at, call_attempts,
active_call_id, decision, decision_message, second_factor_required`.

## Database schema

See `SCHEMA` in `src/agent_voice_bridge/database/store.py`: `approval_requests`,
`decisions` (nonce primary key), `used_totp_counters`, `audit_log`, `meta`
(schema version).
