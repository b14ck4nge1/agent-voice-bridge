# ADR-0002: Pure-Python SIP user agent first, Asterisk as fallback

Status: Proposed. Date: 2026-10-20.

## Context

We need a Windows-native outbound SIP user agent that registers to
sip.linphone.org over TLS, offers SDES-SRTP and ICE, and exposes PCM audio to
Python. Options and their evidence are in feasibility.md section 5.

## Decision

1. Build a minimal UA on `asyncio` + `ssl` (TLS), `pylibsrtp` (SRTP) and `aioice`
   (ICE), all BSD-licensed with Windows wheels, using G.711. It implements
   `CallDispatcher` and posts call events to the bridge.
2. Keep Asterisk (WSL2 or Docker, chan_pjsip outbound registration, ARI External
   Media) as a documented fallback behind the same interface.
3. Do not take a hard dependency on GPL SIP stacks (PJSIP, liblinphone); allow them
   only as optional, separately installed adapters.

## Consequences

* Writing a SIP UA is real work, and interoperability bugs are likely. Mitigation:
  first test against Linphone Desktop on a LAN (HW-02), then sip.linphone.org.
* The approval bridge is unaffected if we switch to Asterisk.
