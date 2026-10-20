# Technical feasibility report (Phase 0)

Status: **research complete; the decisive hardware test (HW-01) has not been run.**
Nothing in this document is a verified call. Conclusions are labelled by evidence:

* **[source]**: read in upstream source code at a stated commit.
* **[docs]**: official documentation.
* **[report]**: third-party report or issue.
* **[inference]**: our reasoning, to be confirmed by a test.

## 1. Can a stock iPhone ring for an internet SIP call while locked?

iOS suspends background apps. A VoIP app only rings when locked or killed if a
server sends it an Apple **PushKit VoIP push**, after which the app reports the call
to **CallKit** (the native incoming-call screen). The push must be signed with
credentials for that app's bundle ID, which only the app's publisher holds. So:

* A self-hosted Asterisk or registrar **cannot** wake the App Store Linphone app on
  its own. It has no Apple credentials for Linphone's bundle ID **[inference,
  consistent with the reports below]**.
* Linphone's FAQ, as quoted in search results, says mobile push is enabled only for
  Linphone (sip.linphone.org) accounts, not third-party SIP accounts. Belledonne
  sells push for third-party accounts as a professional offering **[docs; the page
  itself was unreachable from the research sandbox]**.
* The Linphone iOS source confirms the defaults: sip.linphone.org accounts get
  `push_notification_allowed=1` and `remote_push_notification_allowed=1`; the
  third-party assistant sets both to `0` **[source: linphone-iphone `520d51a`,
  `assistant_*_default_values`]**.
* A self-hosted Flexisip in front of Asterisk with the official Linphone iOS 6.2.2
  app works through Belledonne's `external-push-flexiapi` path, and has a client bug:
  after a push cold start the app can re-register with a malformed `pn-prid`, which
  silently disables later pushes. Maintainers closed it as a client bug on 2026-09-28
  **[report: flexisip#267, linphone-iphone#1048]**.

**Conclusion.** The free path that keeps locked-screen ringing is to give **both
ends free sip.linphone.org accounts**:

```
iPhone: Linphone app, account  sip:alice-phone@sip.linphone.org   (push enabled)
PC:     Agent Voice Bridge UA, sip:alice-bot@sip.linphone.org     (registers, dials)
```

When the PC dials `sip:alice-phone@sip.linphone.org`, Belledonne's Flexisip proxy
sends the PushKit push and the phone rings through CallKit. **[inference, test
HW-01]** No phone number, PSTN or trunk is involved.

**Risk: the issue above may hit our setup.** If it does, Belledonne's own servers
are affected too. Mitigations: re-open the app after a missed push, and report it
upstream. HW-01 includes a cold-start test to catch it.

**Dependency on a third-party free service.** sip.linphone.org is a free community
service "with no guarantee of support" **[docs]**. Its terms for automated (bot)
accounts were **not found**; read them before relying on this. If it is unsuitable,
the fallbacks are in section 6.

## 2. What the PC side must support to talk to a Linphone account

From the Linphone iOS defaults for sip.linphone.org accounts **[source]**:

| Requirement | Value | Consequence for our UA |
|---|---|---|
| Signalling transport | `<sip:sip.linphone.org;transport=tls>` | SIP over TLS (port 5061). Python's `ssl` module is enough. |
| Media encryption | `srtp`, **mandatory** | SDES-SRTP (`a=crypto` in SDP) via `pylibsrtp`. A plain-RTP offer will probably be refused **[inference]**. |
| NAT traversal | `stun,ice`, `stun.linphone.org` | ICE candidates via `aioice`. Without ICE, media between two NATed peers relies on the server relaying media, which is **not verified**. |
| Codecs | The factory config read does not disable any audio codec; liblinphone normally includes Opus and G.711 **[inference]** | Offer PCMU/PCMA (no native codec dependency); confirm in HW-02. |
| Push parameters | Only the *phone* registers with `pn-*` Contact params | The PC account needs none. |

None of `pyVoIP`, Agent-SIP or sip-to-ai provides TLS + SRTP + ICE. Hence ADR-0002.

## 3. Cross-network audio

The iPhone on cellular data is usually behind carrier-grade NAT; the PC is behind a
home router. ICE with STUN alone fails with symmetric NAT on both sides. Whether
sip.linphone.org offers TURN or media relay to free accounts is **unknown**.
HW-01 tests this using Linphone Desktop on Windows, which runs the same media stack
as the phone. If Linphone↔Linphone audio fails over cellular, our own UA can't do
better without a TURN server, and we'd add a free-tier or self-hosted coturn.

## 4. Agent integration feasibility

* Claude Code `PermissionRequest` hook: **feasible, implemented, tested with
  simulated hook input** in the documented schema. Not yet run inside a real Claude
  Code session (task CC-04).
* Codex `PermissionRequest` hook: **feasible on paper**. The schema was verified in
  source; the same `avb-hook` binary is used. Needs a real Codex test (CX-04),
  including whether the VS Code extension honours user hooks.
* Codex app-server: **implemented and tested against a protocol-faithful fake**.
  Only covers threads `avb-codex` starts.
* Clarification questions (Claude's `AskUserQuestion`, Codex
  `item/tool/requestUserInput`): **not yet supported**. They need a free-text answer
  path and are tracked in the backlog.

## 5. SIP transport options compared

| Option | Windows-native | TLS/SRTP/ICE | Python audio access | License impact | Verdict |
|---|---|---|---|---|---|
| Pure-Python UA (asyncio + `ssl` + `pylibsrtp` + `aioice`) | Yes | Yes (to build) | Direct | BSD deps, MIT code | **Primary** |
| Asterisk + ARI External Media (WSL2/Docker) | No | Yes | RTP/WebSocket to Python | GPL, separate process: fine | **Fallback** |
| liblinphone Python wrapper | Yes (if wheel available) | Yes, same as phone | Yes | GPL-3.0 | Optional adapter |
| PJSUA2 | Build it yourself | Yes | Yes | GPL-2.0 | Rejected for MVP |
| baresip | Build it yourself | Yes | Custom module | BSD | Alternative |
| LiveKit SIP | Docker | TLS/SRTP yes | LiveKit agents | Apache-2.0 | Too heavy for one user |

## 6. If sip.linphone.org can't be used

In order of preference, all still on the internet with no phone numbers:

1. **Linphone app + Belledonne's paid push for third-party accounts** in front of a
   self-hosted Asterisk. Breaks "free operation", keeps everything else.
2. **Another free SIP app with its own push proxy** (for example Acrobits Groundwire
   or Zoiper). Each is a paid app or has a paid push tier. To verify case by case.
3. **ntfy / Pushover "call me" notification plus manual dial-in**: the iPhone gets a
   push and the user taps to call the bridge. Not a ringing call. Only with the
   user's explicit agreement (the brief rules out silently downgrading).

## 7. Costs (honest)

See [costs.md](costs.md). Summary: the software is free, and with sip.linphone.org
plus local speech, running costs can be **0** (electricity aside). Cloud speech or
LLM providers are optional and billed per use.
