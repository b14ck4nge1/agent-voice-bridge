# Phase 0 research: existing projects

Researched 2026-10-20. Each repository was cloned and its code read (not just its
README). Commit dates and counts come from `git log` on the default branch at that
time. Star counts are omitted: the GitHub API was rate-limited from the research
environment, and popularity is not evidence that something works.

## Referenced repositories

| Project | Language / license | Last commit (commits) | What it actually does | Windows | SIP / calls | Security | Running cost | Reuse decision |
|---|---|---|---|---|---|---|---|---|
| [HrushiBorhade/pingme](https://github.com/HrushiBorhade/pingme) | TypeScript (Node 18+), **MIT** | 2026-02-21 (42) | Claude Code hooks → Express daemon → [Bolna](https://bolna.ai) places a **PSTN** call; replies are typed into the agent with `tmux send-keys`. | No: bash hooks using `jq`, tmux routing. | None of its own; Bolna handles telephony. | Bearer token, a blocklist of risky instructions. | Bolna per-minute fees, a phone number, Deepgram, ElevenLabs, Anthropic. | **Ideas only.** It is the closest product idea, but it needs PSTN plus paid services, routes answers by keystroke injection, and doesn't run on Windows. |
| [AI-RedCode/Agent-SIP](https://github.com/AI-RedCode/Agent-SIP) | Python (FastAPI), **GPL-3.0** | 2026-10-17 (9) | Pure-Python SIP user agent (UDP, REGISTER with MD5 digest, INVITE) + G.711 RTP bridged to the OpenAI Realtime API, with an MCP control API. | Docker with host networking (no native Windows path). | UDP only. No TLS, SRTP or ICE found in the code. | Web UI defaults to `admin`/`admin`; MCP bearer token. | OpenAI Realtime per minute; needs a PBX. | **Reference only.** GPL-3.0 code can't go into an MIT project. It also lacks TLS and SRTP, which sip.linphone.org requires (see feasibility). |
| [darwincr/sip-phone-mcp](https://github.com/darwincr/sip-phone-mcp) | Python, **MIT** | 2026-05-02 (6) | MCP server that has an agent call phone numbers through LiveKit SIP (Docker: LiveKit, livekit-sip, Redis) and an STT→LLM→TTS pipeline. | Docker Desktop. | Outbound calls through a **SIP trunk** to phone numbers. | LiveKit dev key hardcoded in compose. | Paid SIP trunk + 2–3 AI API keys. | **Not reused.** It solves "agent calls a person", not approvals, and its design centres on PSTN. LiveKit stays noted as a heavier fallback media stack. |
| [inkbox-ai/codex-plugin](https://github.com/inkbox-ai/codex-plugin) | Python, **no license file** | 2026-10-14 (112) | Drives Codex through `codex app-server` and relays it over Inkbox email, SMS, iMessage and calls. Handles `item/commandExecution/requestApproval` and related requests. | No: macOS or Linux only (systemd / launchd). | Inkbox phone numbers (PSTN). | Webhook signing. | Inkbox subscription. | **Read, not copied.** Without a license nothing may be reused. It is useful as independent confirmation of the app-server method names, which we verified against the openai/codex source ourselves. |
| [aicc2025/sip-to-ai](https://github.com/aicc2025/sip-to-ai) | Python 3.12, **Apache-2.0** | 2026-10-15 (76) | Pure asyncio SIP UAS + RTP (G.711) bridged to realtime voice models (OpenAI, Deepgram, Gemini, Grok). | Pure Python, so likely works; untested on Windows. | Receives INVITEs on a LAN. No outbound REGISTER/INVITE client. Its config says TLS is "not required" (internal networks). | None on the SIP side. | Realtime model per minute. | **Candidate for partial reuse** (license compatible, with attribution) of its RTP timing and codec code in Phase 4. Its SIP layer doesn't fit: we need an outbound TLS client. |
| [lincotalk/linco-bridge](https://github.com/lincotalk/linco-bridge) | Node / NestJS, **MIT** | 2026-10-15 (380) | Connects local agent CLIs (Claude Code via `--permission-prompt-tool stdio`, Codex, others) to Linco's own app and web clients over WebSocket. | Yes (Node). | None: chat/app UI, no voice calls. | Signed sessions; demo is explicitly not for sensitive data. | Free demo; official app via TestFlight. | **Not reused.** Different channel (an app, not a call). It confirms that Claude's `--permission-prompt-tool` is another integration route; we chose hooks instead (ADR-0003). |

### Other projects and components evaluated

| Component | License | Notes | Decision |
|---|---|---|---|
| Linphone iOS ([BelledonneCommunications/linphone-iphone](https://github.com/BelledonneCommunications/linphone-iphone)) | GPL-3.0 app (free on the App Store) | Read `Linphone/Ressources/assistant_linphone_default_values` at commit `520d51a` (2026-09-17). For sip.linphone.org accounts it sets `transport=tls`, `stun,ice` with `stun.linphone.org`, `media_encryption=srtp` with **`media_encryption_mandatory=1`**, and `push_notification_allowed=1`. Third-party accounts default to push **off**. | **Chosen phone app.** See [feasibility.md](feasibility.md). |
| Flexisip ([BelledonneCommunications/flexisip](https://github.com/BelledonneCommunications/flexisip)) | AGPL-3.0 | Belledonne's SIP proxy, which sends PushKit pushes for its own apps. Issue [#267](https://github.com/BelledonneCommunications/flexisip/issues/267) (closed 2026-09-28) shows a self-hosted Flexisip in front of Asterisk with official Linphone iOS 6.2.2, relying on Belledonne's paid external-push path. | Not self-hosted. Using the free sip.linphone.org service gets the same push path without hosting anything. |
| Asterisk (chan_pjsip, ARI, External Media) | GPL-2.0, separate process | Mature: TLS, SRTP (SDES), ICE, outbound registration. Not native on Windows (WSL2 or Docker). The license isn't a concern for a separate process. | **Fallback transport** (ADR-0002) if the pure-Python user agent can't interoperate. |
| PJSIP / pjsua2 | GPL-2.0 or commercial | The newest PyPI `pjsua2` is from 2023 with no wheels. `pjsua2-pybind11` ships Linux wheels only. A Windows build needs MSVC + SWIG. | Rejected for the MVP: no Windows binaries, and GPL. |
| baresip | BSD-3-Clause | Full-featured C user agent (TLS, SRTP, ICE, Opus). No official Windows binaries. Getting audio into Python needs a custom module. | Kept as an alternative user agent if the Python one fails. |
| liblinphone SDK (Python wrapper) | GPL-3.0 or commercial | Same stack as the phone, so the best compatibility, but the Python wrapper is only on download.linphone.org (unreachable from the research sandbox, so not verified), and GPL. | Possible optional, separately installed adapter. Not a default dependency. |
| `pylibsrtp` 1.0.0, `aioice` 0.10.2 | BSD-3-Clause | aiortc's libraries. Windows wheels on PyPI (checked 2026-10-20). They provide SRTP (AES-CM + HMAC-SHA1) and ICE/STUN/TURN. | **Building blocks for the pure-Python TLS/SRTP user agent** (ADR-0002). |
| `faster-whisper` 1.2.1 | MIT | Local STT, Windows wheels. | Default local STT (Phase 4). |
| `piper-tts` 1.8.0 | **GPL-3.0-or-later** (since moving to OHF-Voice) | High-quality local TTS. | Optional only, because of its license. |
| `kokoro` 0.9.4 | Apache-2.0 | Local neural TTS. | Candidate default local TTS. |
| Windows SAPI voices | Built into Windows | Zero install, lower quality. | Fallback TTS. |
| OpenAI Realtime / Deepgram / ElevenLabs | Commercial APIs | Best latency and quality; per-minute cost. | Optional cloud providers. |

## Agent integration surfaces (verified)

| Agent | Mechanism | Evidence | Notes |
|---|---|---|---|
| Claude Code | `PermissionRequest` command hook | [code.claude.com/docs/en/hooks](https://code.claude.com/docs/en/hooks), read 2026-10-20 | Input: `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `tool_name`, `tool_input`, `permission_suggestions`. Output: `hookSpecificOutput.decision.behavior` = `allow`/`deny`, optional `message`/`interrupt`/`updatedInput`/`updatedPermissions`. Default command-hook timeout 600 s. Exit code 2 is **ignored** for this event. Exec form (`command` + `args`) avoids the shell on Windows. In sessions that can't prompt, "if no hook returns a decision, it denies the tool call". |
| Codex (CLI, IDE, app) | `PermissionRequest` command hook | `codex-rs/hooks/schema/generated/permission-request.command.{input,output}.schema.json` at openai/codex `0144894` (2026-10-20) | Same decision shape as Claude. `updatedInput`, `updatedPermissions` and `interrupt: true` currently **fail closed**. Config in `~/.codex/hooks.json` (`HooksFile`: only `description` + `hooks` keys allowed). Default timeout 600 s. Runs through `cmd.exe /C` on Windows. New hooks must be reviewed and trusted. Hooks run inside Codex core, so they *should* also fire in the VS Code extension; **not yet verified on a real install**. |
| Codex | `codex app-server` JSON-RPC (stdio) | `codex-rs/app-server-protocol/src/protocol/common.rs`, `v2/item.rs` at `0144894` | `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/permissions/requestApproval`, `item/tool/requestUserInput`, `mcpServer/elicitation/request`. Decisions: `accept`, `acceptForSession`, `acceptWithExecpolicyAmendment`, `applyNetworkPolicyAmendment`, `decline`, `cancel`. There is no `"jsonrpc"` field. A separate client only sees threads it started; it **cannot** take over approvals belonging to the VS Code extension's own app-server. |

## Reuse vs. reimplementation decision

* **Reimplement** the approval bridge, hooks, Codex client and conversation logic.
  Nothing existing provides a security-focused approval state machine, and the hook
  and app-server surfaces are small and well specified.
* **Build the SIP user agent on BSD-licensed libraries** (`pylibsrtp`, `aioice`) plus
  the standard library's `ssl`, because sip.linphone.org requires TLS + SRTP and no
  MIT/BSD/Apache Python SIP stack with those features has Windows wheels.
  sip-to-ai's Apache-2.0 RTP code may be adapted, with attribution.
* **Keep Asterisk as the documented fallback** behind the same `CallDispatcher`
  interface.
* **Copy nothing** from GPL or unlicensed repositories.
