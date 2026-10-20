# Project status

Last updated: 2026-10-20 (session 1, part 2: phone path).

## Summary

Phase 0 (research) is complete. Phases 2, 3 and parts of 4 and 5 are implemented
and tested in software. **First real-device result (2026-10-20, user report):** on the
server deployment (`deploy/`, AWS EC2 + Caddy), Linphone iOS registers over TLS, a
Bark notification arrives, and tapping it placed a TLS/SRTP call where the prompts
were audible and the keypad worked. Not every attempt connected: one earlier tap
gave "wrong user/pass" (most likely a stale second Linphone account). Not yet
tested: locked-phone ringing, cellular, repeated calls, and a real Claude Code or
Codex request.

## Completed (with evidence)

| Area | What | Evidence |
|---|---|---|
| Research | 6 referenced repos cloned and read; licenses and activity checked; alternatives compared | [docs/research.md](docs/research.md) |
| Feasibility | Linphone iOS push only for sip.linphone.org accounts; TLS + mandatory SRTP + ICE required; Codex has a Claude-compatible PermissionRequest hook | [docs/feasibility.md](docs/feasibility.md), ADRs 0001–0004 |
| Bridge | FastAPI + SQLite, state machine, roles, fingerprint, nonces, idempotency, expiry, retries, restart recovery, TOTP, redaction, audit | `src/agent_voice_bridge/{api,approvals,database,security}`, `tests/integration/test_service.py`, `test_api.py` |
| Claude Code / Codex hook | `avb-hook`, stdlib only, fail-closed; installer for both agents' config | `agents/hook.py`, `agents/install.py`, `tests/end_to_end/test_hook_e2e.py`, `tests/unit/test_install.py` |
| Codex app-server | JSON-RPC client + approval router + `avb-codex run` | `agents/codex/`, `tests/end_to_end/test_codex_app_server.py` (fake server) |
| Conversation | Rule-based dialog: explain, answer questions, confirm, TOTP, several requests per call, injection-resistant | `voice/conversation/`, `tests/integration/test_conversation.py` |
| Simulator | `avb-voice-sim` text "phone" | Manual smoke test below |

## Test results (this session, Linux sandbox)

* `pytest`: **122 passed** on Python 3.13.16 and 3.11.15.
* `ruff check`, `ruff format --check`, `mypy --strict`: clean.
* Manual smoke test with the real console scripts: `avb-admin init` →
  `avb-bridge` → `avb-hook --agent claude-code` (fed the documented hook JSON) →
  `avb-voice-sim` (piped: answer, "what command?", "will that delete anything?",
  "approve it", "yes") → the hook printed
  `{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}`.
* GitHub Actions on PR #1 (commit `bcd7081`): lint, mypy and the full test suite
  **passed on `windows-latest` and `ubuntu-latest`, Python 3.11 and 3.13** (run
  39391626629).
* **Not run:** the PowerShell scripts on a personal Windows PC, a real Claude Code
  or Codex session, any SIP call, any iPhone test.

## Phone path (added in part 2, ADR-0005)

Built because a sip.linphone.org account couldn't be created:
Bark notification (encrypted, critical, ringing) → tap → Linphone calls the bridge's
own SIP server over Tailscale → spoken prompts (Windows SAPI / espeak-ng) → keypad
decision.

* Verified in upstream source: Bark opens the notification URL on tap; Linphone iOS
  places a call for `linphone-sip:` URLs; Bark's AES-GCM format.
* SIP server tested against an **independent client (pyVoIP 1.6.8)**: digest
  REGISTER and INVITE accepted, two-way audio confirmed by frequency analysis
  (server heard the 440 Hz test tone, client heard the 1 kHz one).
* liblinphone 5.2 (Debian) and baresip couldn't run in the sandbox (no IPv6 in the
  kernel), so **no Linphone-to-bridge call has been tested yet**.
* `avb-admin phone-test` smoke test (test client dialling in, keys 4 2 #): answered,
  speech played, keys read back, bridge hung up.
* Tests: **171 passed, 1 skipped** (Windows SAPI test, which runs on the Windows CI runner).

## Server mode (part 3, ADR-0006)

* SIP over TLS (certificate reloaded periodically) and SDES-SRTP (libsrtp via
  pylibsrtp); forged/unauthenticated RTP is dropped; plain RTP refused when required.
* `deploy/`: Dockerfile, docker-compose (Caddy + bridge, host networking), Caddyfile
  exposing only agent endpoints, `install.sh` for Ubuntu; `scripts/setup-pc-remote.ps1`;
  `avb-admin test-approval`.
* Verified here: TLS + SRTP call round trip, DTMF and two-way audio (tests); config
  safety rules; clean package install on Python 3.12; `install.sh` passes `bash -n`.
* **Not verified here:** the Docker image build (Debian mirrors are blocked in this
  sandbox), Caddy certificate issuance, any call from real Linphone.

## Current work

None in progress. The next engineering task, SIP-01 (TLS registration to
sip.linphone.org), should wait for HW-01's result.

## Blockers

| ID | Blocker | Needs |
|---|---|---|
| HW-04 | Server path: partly passed (one call: audio + keypad OK). Still needs repeat calls, locked phone, cellular | You: repeat `avb-admin test-approval`, report failures with the bridge log |
| HW-01 | (Only for the Linphone-account path) | A sip.linphone.org account, which couldn't be created |
| CC-04 | Hook inside real Claude Code on Windows | You: run the quick start, try approve/reject/timeout |
| CX-04 | Hook inside real Codex CLI and the VS Code extension | You: same, plus trusting the hook in Codex |
| R-07 | sip.linphone.org terms for automated accounts | linphone.org was unreachable from the research sandbox |

## Known limitations

* No SIP, audio or speech yet; `AVB_CALL_DISPATCHER` only supports `log`.
* Clarification questions (Claude AskUserQuestion, Codex requestUserInput) aren't
  routed yet.
* When Codex resolves an approval itself (`serverRequest/resolved`), `avb-codex`
  stops waiting but the bridge request stays open until it expires (CX-03).
* The risk rating is heuristic, and the conversation engine understands English only.

## Next steps

1. **You:** run HW-01 and send the report template back.
2. **You (optional, 10 min):** Windows quick start with Claude Code and the
   simulator, to cover CC-04.
3. **Me, after HW-01:** SIP-01 to SIP-04 (TLS + SRTP + ICE user agent), then HW-02
   with our UA.
4. Then local STT/TTS (VO-03, VO-04).
