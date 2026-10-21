# ADR-0005: Bark wakes the phone; Linphone calls the bridge's own SIP server

Status: Accepted (supersedes ADR-0001 as the default). Date: 2026-10-20.

## Context

The user could not create a sip.linphone.org account, so ADR-0001's push path isn't
available. iOS only wakes a softphone through its publisher's push service, so a
self-hosted SIP server cannot ring a sleeping Linphone. Verified in source:

* Bark (MIT, App Store) can be pushed from any server, including the public
  `api.day.app` or a self-hosted bark-server. It supports a repeating ringtone
  (`call=1`), critical alerts and AES-GCM encrypted payloads (`Finb/Bark`,
  `CallProcessor`, `LevelProcessor`, `CiphertextProcessor`).
* Tapping a Bark notification opens its `url` (`AppDelegate.handleNotification`).
* Linphone iOS handles `linphone-sip:` / `sip:` URLs by placing a call at once
  (`URIHandler.initiateCall`, linphone-iphone `520d51a`).

Free iOS SIP apps that push for any server (Siperb, SipPulse, Zoiper) either route
through the vendor's hosted proxy or need an RFC 8599 push-capable registrar. None
was verified to work with a self-hosted server for free.

## Decision

1. The bridge runs a small SIP server (registrar + call answering, UDP/TCP, digest
   auth, G.711, RFC 4733 DTMF) bound to a private address, by default the PC's
   **Tailscale** address. Tailscale's free plan connects phone and PC across NATs
   and encrypts traffic with WireGuard, so the SIP server needs no TLS or SRTP.
2. A pending approval sends a **Bark** notification (critical, ringing, encrypted)
   whose tap action is `linphone-sip:avb@<pc>`. The phone calls the PC, so the PC
   never has to reach a sleeping phone.
3. Until speech recognition lands, the caller answers with the **keypad**; prompts
   are spoken with local TTS (Windows SAPI voices, espeak-ng elsewhere).

## Consequences

* One extra tap (unlock and tap the notification) compared with a CallKit ring.
  This was the user's explicit choice.
* No Linphone account, phone number or paid service is needed. Tailscale and the
  public Bark server are free third-party services; both can be self-hosted
  (Headscale, bark-server).
* The SIP server refuses non-private bind addresses unless `AVB_ALLOW_PUBLIC_SIP=1`.
* ADR-0001/0002 remain as the path if a Linphone account becomes available.
