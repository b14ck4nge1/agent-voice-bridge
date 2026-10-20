# Project status

Last updated: 2026-10-20 (session 1).

## Summary

Phase 0 (research) is complete. Phases 2, 3 and parts of 4 and 5 are implemented
and tested in software. **No phone call has been made.** The project's main risk,
whether a free Linphone setup rings a locked iPhone with two-way audio across
networks, is untested and waits on you: [HW-01](docs/hardware-tests/HW-01-linphone-push.md).

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
* **Not run:** Windows (CI is configured but hasn't run on GitHub yet), a real
  Claude Code or Codex session, any SIP call, any iPhone test.

## Current work

None in progress. The next engineering task, SIP-01 (TLS registration to
sip.linphone.org), should wait for HW-01's result.

## Blockers

| ID | Blocker | Needs |
|---|---|---|
| HW-01 | Real-device push/audio test | You: iPhone + Linphone + 2 free accounts (~45 min) |
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
