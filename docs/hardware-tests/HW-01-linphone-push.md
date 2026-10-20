# HW-01: Does a free Linphone call ring your locked iPhone, with audio, across networks?

**Why this first.** It's the project's biggest risk, and it needs no code from this
repository. If two stock Linphone apps on free sip.linphone.org accounts can't do
this, our own SIP code won't either, and the architecture has to change
(feasibility.md section 6).

Time needed: about 45 minutes. Cost: free.

## You need

* Your iPhone, with **Linphone** from the App Store (publisher Belledonne
  Communications).
* Your Windows PC, with **Linphone Desktop** for Windows from linphone.org
  (Downloads).
* Two **free sip.linphone.org accounts**: account **A** for the phone, account **B**
  for the PC. Create them in the apps ("Create account"), or on the website
  subscribe.linphone.org. Write down both usernames. Account B later becomes the
  bridge's own account.
* Optional: a second network for the PC, such as a phone hotspot or a friend's
  Wi-Fi.

## Setup

1. iPhone: open Linphone and sign in with account **A**. When iOS asks, **allow
   notifications and microphone**. In Linphone, under *Settings → Accounts → A*,
   check that push notifications are on.
2. iPhone: *Settings → Linphone*: notifications allowed; *Settings → Focus*: make
   sure no Focus mode is silencing calls during the test.
3. PC: install Linphone Desktop and sign in with account **B**. Confirm it shows as
   registered (green).
4. Write down: iOS version, Linphone iOS version (*Settings → About*), Linphone
   Desktop version, mobile carrier and country.

## Tests

On the PC, call `sip:<A-username>@sip.linphone.org` each time. For each test, record
**rang? / seconds until ring / native call screen? / you hear PC? / PC hears you? /
notes**.

| # | iPhone state | iPhone network | PC network | Pass if |
|---|---|---|---|---|
| T1 | Linphone open, unlocked | Home Wi-Fi | Home Wi-Fi | Rings, two-way audio |
| T2 | **Locked**, screen off, Linphone in background | Home Wi-Fi | Home Wi-Fi | Native (CallKit) incoming-call screen on the lock screen; two-way audio after answering |
| T3 | Locked, Linphone **swiped away** from the app switcher | Home Wi-Fi | Home Wi-Fi | As T2 |
| T4 | Locked, swiped away | **Cellular only** (Wi-Fi off) | Home Wi-Fi | As T2, and keep talking 60 s: audio both ways the whole time |
| T5 | Repeat T4 twice more, 5 minutes apart, without opening Linphone in between | Cellular | Home Wi-Fi | Rings every time (catches the known re-registration push bug) |
| T6 | Locked | Cellular | **Other network** (hotspot) | As T4 |
| T7 | Locked; **decline** the call on the iPhone | any | any | PC shows the call declined |
| T8 | Locked; **don't answer** | any | any | Note how long it rings and what the PC shows |

During one connected call (T4 or T6), open **call statistics** in Linphone Desktop
(the call window's stats or info panel) and note: **audio codec**, **media
encryption** (SRTP or ZRTP), and **ICE status / candidate type** (host, srflx or
relay). This tells us whether media went direct or through a relay, which decides
whether our own SIP stack needs TURN.

## Report back

Copy this into a message or a GitHub issue:

```
HW-01 result
iOS:            Linphone iOS:          Linphone Desktop:
Carrier/country:
T1: rang Y/N, ring after __s, audio both ways Y/N, notes:
T2:
T3:
T4:
T5: (call 1/2/3)
T6:
T7:
T8: rang for __s, PC showed:
Call stats (T4/T6): codec __, encryption __, ICE __ (host/srflx/relay)
Anything odd:
```

Screenshots of the lock-screen call and the stats panel help. **Blur your SIP
usernames** if you post them publicly.

## What happens next

* **All pass:** ADR-0001 is accepted, and task SIP-01 (our TLS/SRTP SIP user agent)
  starts. HW-02 then repeats T1–T6 with our UA in place of Linphone Desktop.
* **T2/T3 fail** (no ring when locked/killed): push isn't working for free
  accounts. Stop and decide on a fallback together.
* **T4/T6 fail on audio only**: NAT traversal needs a relay. Add a TURN server
  (task SIP-05).
* **T5 fails**: likely the upstream re-registration bug. Report upstream and check
  whether opening the app restores push.
