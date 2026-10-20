# iPhone setup

Status: instructions for the Phase 1 hardware test. **Not yet validated on a device
by this project.**

1. Install **Linphone** (Belledonne Communications) from the App Store. It's free.
2. Create or sign in to a free **sip.linphone.org** account. Use this account only
   for the phone; the PC gets its own.
3. Allow **notifications** and **microphone** when asked. Without notifications iOS
   can't wake Linphone for a call.
4. In Linphone account settings, make sure **push notifications** are enabled.
   (They're on by default for sip.linphone.org accounts; third-party SIP accounts
   default to off and don't get push from the free service.)
5. Optional, recommended: in your authenticator app (any RFC 6238 app: Apple
   Passwords, Google Authenticator, Aegis, ...) add the `otpauth://` secret printed
   by `avb-admin init`. You'll read its 6-digit code aloud to approve high-risk
   operations.

Calls should appear on the native iOS incoming-call screen (CallKit) when the phone
is locked. That behaviour is what hardware test
[HW-01](hardware-tests/HW-01-linphone-push.md) verifies.

## Known caveats

* Focus / Do Not Disturb modes can silence calls. Allow Linphone, or your contacts,
  in the Focus settings.
* Low Power Mode and poor connectivity can delay pushes.
* Linphone's free service has no guaranteed availability.
