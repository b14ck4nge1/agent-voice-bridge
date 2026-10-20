# Agent Voice Bridge

**Status: pre-alpha.** The approval bridge, the Claude Code / Codex hooks, the Codex
app-server client, a built-in SIP server and a keypad + text-to-speech call flow work
and are tested in software. **It has not yet been tried on a real iPhone**; see
[phone-setup.md](docs/phone-setup.md) and [PROJECT_STATUS.md](PROJECT_STATUS.md).
Speech recognition (talking instead of pressing keys) is next.

How the phone part works: a pending approval sends a ringing **Bark** notification;
tapping it makes **Linphone** call the bridge's own SIP server over **Tailscale**.
No phone number, Linphone account or paid service.

When Claude Code or OpenAI Codex on your Windows PC needs permission, Agent Voice
Bridge is meant to **ring your iPhone over the internet**: no phone number, no
PSTN, no custom iOS app. An assistant tells you which agent and project is asking,
what exactly it wants to do and how risky that looks. It answers your questions and
takes an explicit, confirmed approve or reject for that one operation. The agent
then continues or stops through its own official permission mechanism.

```
Claude Code / Codex ──PermissionRequest hook──► avb-hook ──► approval bridge (FastAPI + SQLite)
                                                                   │
                         Linphone on iPhone ◄── SIP/TLS+SRTP ◄── call + conversation   (planned)
                                                                   │
                                     avb-voice-sim (text stand-in) ┘   (works today)
```

## What works today

* `avb-bridge`: authenticated local API with an explicit state machine, request
  fingerprints, expiring requests, idempotency, replay protection, TOTP for
  high-risk approvals, redaction before storage, and an audit log.
* `avb-hook --agent claude-code|codex`: the `PermissionRequest` hook for both
  agents. It returns only allow/deny for the one call, and denies on any failure.
* `avb-admin init | install-hooks`: generates secrets and safely edits
  `~/.claude/settings.json` and `~/.codex/hooks.json`.
* `avb-codex run "prompt"`: runs Codex through `codex app-server`, with approvals
  routed to the bridge.
* `avb-voice-sim`: answer approval "calls" by typing, using the same conversation
  engine the phone call will use.
* Built-in SIP server, Bark wake-up and a keypad call flow with spoken prompts.
* 171 automated tests: unit, integration, and end-to-end with real hook processes and SIP calls.

## Quick start with your own server (phone needs only Bark + Linphone)

See [docs/server-setup.md](docs/server-setup.md): Cloudflare DNS, one install command For the no-Bark option (Linphone accounts), see [docs/linphone-account.md](docs/linphone-account.md).
on Ubuntu, one PowerShell command on the PC.

## Quick start with the phone over Tailscale (Windows)

See [docs/phone-setup.md](docs/phone-setup.md): one script (`scripts/setup-phone.ps1`)
installs everything and writes the exact values to enter on the iPhone.

## Quick start without the phone (Windows)

```powershell
git clone https://github.com/b14ck4nge1/agent-voice-bridge.git
cd agent-voice-bridge
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
scripts\start.ps1                                    # terminal 1: the bridge
.\.venv\Scripts\avb-voice-sim                        # terminal 2: the "phone"
.\.venv\Scripts\avb-admin install-hooks --claude     # then restart Claude Code
```

Next time Claude Code asks for permission, terminal 2 rings. Try `what command?`,
`is it safe?`, `approve`, `yes`, or `reject it, use pnpm instead`.

Details: [Windows setup](docs/windows-setup.md) · [iPhone setup](docs/iphone-setup.md)
· [SIP setup](docs/sip-setup.md) · [API](docs/api.md) ·
[Troubleshooting](docs/troubleshooting.md)

## Design and research

* [Research: existing projects and reuse decisions](docs/research.md)
* [Feasibility: iPhone push, SIP, NAT](docs/feasibility.md)
* [Architecture](docs/architecture.md) and [ADRs](docs/adr/)
* [Security model](docs/security.md) · [Costs](docs/costs.md) · [Task tracker](docs/TASKS.md)

Key findings: the App Store **Linphone** app only gets iOS push (needed to ring when
locked) for free **sip.linphone.org** accounts. So the PC registers its own free
sip.linphone.org account and dials yours, which requires SIP over TLS with mandatory
SRTP. Both Claude Code and Codex expose a `PermissionRequest` hook with the same
decision format, so one hook serves both.

## License

MIT, see [LICENSE](LICENSE). Linphone, sip.linphone.org and the agents are
third-party products with their own terms.
