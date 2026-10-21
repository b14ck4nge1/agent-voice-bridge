# ADR-0001: Use free sip.linphone.org accounts on both ends

Status: Proposed, pending hardware test HW-01. Date: 2026-10-20.

## Context

The call must ring a locked iPhone using a free App Store app, with no phone
numbers. iOS only wakes VoIP apps through PushKit pushes signed with the app
publisher's credentials. The official Linphone iOS app enables push only for
sip.linphone.org accounts (source defaults, Linphone FAQ). Push for third-party SIP
servers is a paid Belledonne offering.

## Decision

The iPhone uses Linphone with a free sip.linphone.org account. The bridge registers
its **own** free sip.linphone.org account and dials the phone's SIP address. No
self-hosted registrar in the default setup.

## Consequences

* Free to run, with push handled by Belledonne's infrastructure.
* We depend on a free community service with no SLA and unknown rules on automated
  accounts. Both are documented risks.
* The PC user agent must do SIP over TLS, mandatory SRTP and ICE (see ADR-0002).
* If HW-01 fails (no ring when locked, or no audio across networks), fall back as
  described in feasibility.md section 6, with the user's agreement.
