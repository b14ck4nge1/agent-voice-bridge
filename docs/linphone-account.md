# Linphone-account mode (no Bark)

Two ways to reach the iPhone; pick one with `deploy/linphone.sh`:

| | **bark-sip** (default) | **linphone** |
|---|---|---|
| Wakes the phone | Bark notification; you tap it and Linphone calls the server | Linphone's own push: a normal incoming call, also when locked |
| Accounts | None (the server is the SIP account) | Two free sip.linphone.org accounts: yours (on the iPhone) and one for the bridge |
| Depends on | Bark's push server | sip.linphone.org (free, no guarantee of service) |

## Setup

1. Create a second free account for the bridge at <https://subscribe.linphone.org>
   (or in the Linphone app on another device). Keep **your** account signed in on the iPhone.
2. On the iPhone, in Linphone, for your linphone.org account: allow notifications;
   settings → account → **media encryption: SRTP** (the bridge doesn't do ZRTP).
3. On the server:

   ```bash
   cd ~/agent-voice-bridge && git pull
   sudo bash deploy/linphone.sh setup   # asks for the bridge account and your username
   sudo bash deploy/linphone.sh test    # your phone rings: answer, press keys, # to end
   ```

4. If the test works: `sudo bash deploy/linphone.sh use`. Back to Bark any time:
   `sudo bash deploy/linphone.sh bark`.

## How it works

The bridge signs in to sip.linphone.org over TLS (port 5061) with its account and calls
yours. Flexisip (Linphone's server) sends a push to the iPhone, which shows a normal
incoming call. Audio goes straight between the phone and the server (SDES-SRTP); the
server's public IP is in the SDP (`AVB_SIP_MEDIA_IP`) and UDP 40000-40200 must be open.
Unanswered calls are retried (`AVB_LINPHONE_RETRIES`, default 3, a minute apart).

## Troubleshooting

| Message | Meaning |
|---|---|
| `Sign-in failed: ... wrong username or password` | The bridge account's details are wrong; run `setup` again |
| `408` / `480` | The phone didn't wake: Linphone signed out, notifications off, or push not working |
| `603` | You declined |
| `phone answered without SRTP` | Set media encryption to SRTP for your account in Linphone |
| Call connects, no audio | UDP 40000-40200 not open in the AWS security group, or wrong `AVB_SIP_MEDIA_IP` |

Not tested on a real phone yet: this was built and tested against a simulated
sip.linphone.org. Whether sip.linphone.org accepts the bridge's sign-in and how its
media behaves are confirmed only by your first test call.
