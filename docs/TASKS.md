# Task tracker

Statuses: BACKLOG, READY, IN_PROGRESS, BLOCKED, TESTING, DONE. Ordered by technical
risk within each phase. **AWAITING USER** marks tasks blocked on you (devices,
accounts).

Evidence for DONE items refers to files and tests in this repository at the commit
that marked them done.

## Phase 0: research and feasibility

| ID | Task | Pri | Deps | Status | Acceptance criteria | Evidence |
|---|---|---|---|---|---|---|
| R-01 | Inspect the 6 referenced repos (code, license, activity) | P0 | – | DONE | Comparison table with a reuse decision per repo | docs/research.md |
| R-02 | Verify Claude Code PermissionRequest hook schema | P0 | – | DONE | Input/output fields, timeout, Windows shell behaviour documented from official docs | docs/research.md, ADR-0003 |
| R-03 | Verify Codex hook + app-server protocol | P0 | – | DONE | Method names, params, decisions and hook schema from openai/codex source | docs/research.md, `agents/codex/app_server.py` docstring |
| R-04 | Linphone iOS push feasibility | P0 | – | DONE (research) | Source/docs evidence on which accounts get push | docs/feasibility.md section 1 |
| R-05 | SIP transport comparison for Windows | P0 | – | DONE | Options table, decision | docs/feasibility.md section 5, ADR-0002 |
| R-06 | Cost analysis | P1 | – | DONE | Software vs. operating costs | docs/costs.md |
| R-07 | Read sip.linphone.org terms of use re: automated accounts | P1 | – | BLOCKED | Terms quoted in feasibility.md | linphone.org unreachable from the research sandbox; needs a browser |

## Phase 1: SIP calling proof of concept (highest risk)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Evidence |
|---|---|---|---|---|---|---|
| HW-01 | Linphone Desktop → Linphone iOS: locked/killed, cellular, two-way audio | **P0** | – | SUPERSEDED (own SIP server / linphone mode used instead) | T1–T8 in the HW-01 doc pass and call stats recorded | docs/hardware-tests/HW-01-linphone-push.md |
| SIP-01 | Python SIP UA: REGISTER over TLS + digest auth to sip.linphone.org | P0 | HW-01 | DONE (user report 2026-10-20: registers and calls via sip.linphone.org) | Registers, refreshes, survives a network change; tested against sip.linphone.org | – |
| SIP-02 | Outbound INVITE with SDES-SRTP offer (PCMU/PCMA), BYE/CANCEL | P0 | SIP-01 | DONE (user report 2026-10-20: real calls via sip.linphone.org) | Linphone Desktop on LAN answers; SRTP negotiated | – |
| SIP-03 | ICE (aioice) with stun.linphone.org | P0 | SIP-02 | NOT NEEDED (server has a public IP; media latching works on real calls) | Media flows iPhone (cellular) ↔ PC (home NAT) | – |
| SIP-04 | `SipDispatcher` implementing `CallDispatcher` + call events | P0 | SIP-02 | DONE (LinphoneDispatcher) | Bridge request rings the phone; events update state | – |
| HW-02 | Repeat HW-01 T1–T6 with our UA as caller | P0 | SIP-03 | DONE (user report 2026-10-20: linphone-mode calls work) | All pass on real device | – |
| SIP-05 | TURN support (only if HW-01/02 show relay needed) | P1 | HW-01 | BACKLOG | Media over cellular via TURN | – |
| SIP-06 | Asterisk fallback config (WSL2) | P2 | – | BACKLOG | Documented, tested originate via ARI | – |

### Phase 1b: Bark + own SIP server (ADR-0005, current default)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Evidence |
|---|---|---|---|---|---|---|
| PB-01 | Bark notifier (AES-GCM, critical, call=1, dial URL) | P0 | – | DONE | Payload matches Bark app format | `notify/bark.py`, `tests/unit/test_bark.py` |
| PB-02 | SIP registrar + UAS (UDP/TCP, digest MD5/SHA-256, G.711, DTMF, lockout) | P0 | – | DONE | Tests + independent client interop | `sip/`, `tests/integration/test_sip_server.py`, pyVoIP spike (PROJECT_STATUS) |
| PB-03 | Call flow: TTS prompts, keypad decisions, barge-in, silence timeout | P0 | PB-02 | DONE | E2E with real hook process | `voice/phone_call.py`, `tests/end_to_end/test_phone_e2e.py` |
| PB-04 | Windows setup script + phone-test command | P0 | PB-03 | SUPERSEDED (server deployment) | Script parses in CI; user run pending | `scripts/setup-phone.ps1`, `phone_test.py` |
| HW-03 | Real iPhone test of the full path | P0 | PB-04 | SUPERSEDED by HW-04 | Bark rings locked phone; tap → call → hear prompts → keys work | docs/phone-setup.md |
| VO-07 | Speech recognition instead of keypad | P1 | HW-03 | DONE (user report 2026-10-20: spoken conversation works) | Spoken approve/reject works on the phone | – |

### Phase 1c: public server (ADR-0006)

| ID | Task | Pri | Deps | Status | Acceptance criteria | Evidence |
|---|---|---|---|---|---|---|
| PS-01 | SIP over TLS + SDES-SRTP | P0 | PB-02 | DONE | TLS+SRTP call test, forged RTP dropped | `sip/srtp.py`, `tests/integration/test_sip_server.py` |
| PS-02 | Docker/Caddy deployment + Ubuntu install script | P0 | PS-01 | DONE (running on the user's server) | Runs on user's Ubuntu 26.04 server | `deploy/` |
| PS-03 | PC connection script + test-approval command | P0 | PS-02 | DONE (user report: test-approval Decision: allow) | `Decision: allow` on user's PC | `scripts/setup-pc-remote.ps1`, `admin.py` |
| HW-04 | Real iPhone via server: Bark → Linphone TLS/SRTP call → approve | P0 | PS-03 | DONE (user report 2026-10-20: calls, audio, keys, approvals) | Works on cellular | User report 2026-10-20: TLS register, Bark, tap-to-call, audio and keypad worked on one call; docs/server-setup.md |

## Phase 2: approval bridge

| ID | Task | Pri | Deps | Status | Acceptance criteria | Evidence |
|---|---|---|---|---|---|---|
| BR-01 | FastAPI app, models, OpenAPI | P0 | – | DONE | Endpoints documented; OpenAPI served | `api/app.py`, `tests/integration/test_api.py::test_openapi_document` |
| BR-02 | SQLite store + schema + audit log | P0 | – | DONE | Atomic transitions; audit per event | `database/store.py`, `test_audit_log_records_lifecycle` |
| BR-03 | Explicit state machine | P0 | – | DONE | All transitions tabled; invalid ones raise | `approvals/state_machine.py`, `tests/unit/test_state_machine.py` |
| BR-04 | Auth roles (agent / voice), request secret | P0 | – | DONE | Cross-role use refused | `test_auth_is_required_and_roles_are_separate`, `test_wait_requires_request_secret` |
| BR-05 | Idempotency, nonces, fingerprint binding, replay protection | P0 | – | DONE | Tests for replay, mismatch, idempotent retry | `tests/integration/test_service.py` |
| BR-06 | Expiry, cancellation, missed-call retry, restart recovery | P0 | – | DONE | Tests pass | `test_expiry`, `test_cancel`, `test_missed_calls_retry_then_fail`, `test_restart_recovery_redials_interrupted_calls` |
| BR-07 | Redaction before storage; TOTP second factor for high risk | P0 | – | DONE | Secrets absent from DB; TOTP single-use | `test_create_queues_a_call_and_redacts`, `test_totp_code_cannot_be_reused` |
| SEC-03 | HTTPS reverse-proxy guide for remote bridge | P2 | – | DONE (deploy/Caddyfile, docs/server-setup.md) | Caddy config + docs | – |
| SEC-04 | Rate-limit decision attempts per request | P1 | – | BACKLOG | N failed attempts → request FAILED | – |
| SEC-05 | Policy: TOTP for every approval | P2 | – | BACKLOG | Config flag + tests | – |

## Phase 3: Claude Code

| ID | Task | Pri | Deps | Status | Acceptance criteria | Evidence |
|---|---|---|---|---|---|---|
| CC-01 | `avb-hook` (stdlib) PermissionRequest adapter | P0 | BR-01 | DONE | allow/deny JSON per docs; never widens permissions | `agents/hook.py`, `tests/end_to_end/test_hook_e2e.py` |
| CC-02 | Fail-safe behaviour (down, timeout, bad token, bad input) | P0 | CC-01 | DONE | All deny (or defer in `ask` mode) | `test_bridge_down_denies`, `test_timeout_denies_and_cancels`, `test_invalid_token_denies` |
| CC-03 | Installer for settings.json (exec form, backup, idempotent) | P0 | CC-01 | DONE | Unit tests | `tests/unit/test_install.py` |
| CC-04 | Test inside a real Claude Code session on Windows | P0 | CC-03 | DONE (user report 2026-10-20: approvals from Claude Code and Codex) | Approve/reject/timeout observed in Claude Code; two sessions at once | – |
| CC-05 | Clarification questions (AskUserQuestion) by phone | P2 | Phase 4 | BACKLOG | Research the supported hook path first | – |

## Phase 4: AI voice

| ID | Task | Pri | Deps | Status | Acceptance criteria | Evidence |
|---|---|---|---|---|---|---|
| VO-01 | Conversation engine (rules, confirmation, TOTP, multiple requests) | P0 | BR-01 | DONE | Example dialogue from the brief passes as a test | `tests/integration/test_conversation.py` |
| VO-02 | Text simulator over the voice API | P0 | VO-01 | DONE | Manual smoke test: hook → sim → allow | PROJECT_STATUS.md, test results |
| VO-03 | Local STT (faster-whisper) + VAD + barge-in | P1 | SIP-04 | SUPERSEDED (Gemini / API / Whisper speech-to-text, endpointer, barge-in) | Latency < 1.5 s on a typical PC | – |
| VO-04 | Local TTS (Kokoro / SAPI) | P1 | SIP-04 | DONE (Piper) | Natural speech over G.711 | – |
| VO-05 | Optional LLM for free-form Q&A (no decision tools beyond the bridge's) | P2 | VO-01 | DONE (assistant brain; approvals confirmed by code) | Injection tests pass | – |
| VO-06 | Silence / interruption / disconnection handling | P1 | VO-03 | DONE (silence timeout, barge-in, pause joining; tests) | Scenario tests | – |

## Phase 5: Codex

| ID | Task | Pri | Deps | Status | Acceptance criteria | Evidence |
|---|---|---|---|---|---|---|
| CX-01 | Codex hook config installer (`hooks.json`) | P0 | CC-01 | DONE | Unit test; only allowed top-level keys | `test_codex_entry_and_removal` |
| CX-02 | app-server client (`avb-codex run`) | P1 | BR-01 | DONE (simulated) | Approvals round-trip against a fake server | `tests/end_to_end/test_codex_app_server.py` |
| CX-03 | Handle `serverRequest/resolved` by cancelling the bridge request | P1 | CX-02 | BACKLOG | Bridge request cancelled, call stopped | Currently only the local handler is cancelled |
| CX-04 | Test with real Codex CLI and VS Code extension on Windows | P0 | CX-01 | IN PROGRESS (Codex CLI approvals worked; VS Code extension not reported) | Hook fires in CLI; result recorded for the extension | – |
| CX-05 | `item/tool/requestUserInput` support | P2 | VO-05 | BACKLOG | – | – |

## Phase 6–7: end-to-end and release

| ID | Task | Pri | Deps | Status | Acceptance criteria | Evidence |
|---|---|---|---|---|---|---|
| E2E-01 | Full Claude Code → iPhone → approve → resume | P0 | HW-02, CC-04 | BACKLOG | Real device | – |
| E2E-02 | Same for Codex | P0 | HW-02, CX-04 | BACKLOG | Real device | – |
| E2E-03 | Failure matrix (missed, lock screen, network loss, registration loss) | P1 | E2E-01 | BACKLOG | Report | – |
| WIN-01 | GitHub Actions on Windows + Ubuntu | P1 | – | DONE | CI green on both | `.github/workflows/ci.yml`; Actions run 39391626629 green on windows/ubuntu × 3.11/3.13 |
| WIN-02 | install/start/test scripts verified on a personal Windows PC | P1 | – | DONE (setup-pc-remote.ps1 run by the user) | Scripts run cleanly | – |
| REL-01 | License check of dependencies | P2 | – | BACKLOG | Table in docs | – |

| AI-01 | AI assistant: STT (Gemini/API/Whisper), LLM brain (Claude/OpenAI-style) with tools, Piper TTS | P0 | VO-07 | DONE (user report 2026-10-20) | Real call: natural conversation, approvals confirmed by code | docs/assistant.md |
| AI-02 | PC helper (avb-worker): start/continue/report Claude Code tasks by phone | P0 | AI-01 | DONE (user report 2026-10-20) | Task started by voice runs on the PC and is reported back | src/agent_voice_bridge/worker/cli.py |
| AI-03 | Call when a phone task finishes; calls to the bridge (code-protected) | P1 | AI-02 | IN REVIEW (inbound calls work; task-finished callback not reported yet) | Real phone | – |
| AI-04 | Codex tasks from the phone (`codex exec`) | P2 | AI-02 | BACKLOG | – | – |
| AI-05 | Personal assistant: scheduled calls, memory, call summaries, pause joining | P1 | AI-01 | IN REVIEW | Real calls | src/agent_voice_bridge/assistant/personal.py |
| AI-06 | Cloud-session approvals (repository hook) | P1 | – | IN REVIEW | A cloud session's command rings the phone | docs/cloud-sessions.md |
| OPS-01 | Server auto-update with health check and rollback | P1 | – | IN REVIEW | First real update logged | deploy/auto-update.sh |
