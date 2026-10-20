# SIP setup

> **Superseded as the default** by [phone-setup.md](phone-setup.md) (Bark + Tailscale + the bridge's own SIP server, ADR-0005). This page applies only if you use a sip.linphone.org account.

Status: **the bridge's SIP user agent is not implemented yet** (task SIP-01). This
page describes the target setup so the accounts can be prepared and HW-01 run.

## Default: two free sip.linphone.org accounts

| Account | Used by | Settings |
|---|---|---|
| `you-phone@sip.linphone.org` | Linphone on the iPhone | Push on (default) |
| `you-bot@sip.linphone.org` | Agent Voice Bridge on the PC | Registers over TLS and dials the phone account |

Planned environment variables (not read by this release):

```
AVB_CALL_DISPATCHER=sip
AVB_SIP_USERNAME=you-bot
AVB_SIP_PASSWORD=...            # store in bridge.env, never in git
AVB_SIP_DOMAIN=sip.linphone.org
AVB_SIP_TRANSPORT=tls
AVB_SIP_TARGET_URI=sip:you-phone@sip.linphone.org
AVB_STUN_SERVER=stun.linphone.org
# AVB_TURN_URI / AVB_TURN_USERNAME / AVB_TURN_PASSWORD   if HW-01 shows relay is needed
```

The PC makes **outbound** connections only (SIP over TLS to port 5061, media via
ICE). No inbound port forwarding should be needed. HW-02 will confirm.

## Fallback: Asterisk (WSL2 or Docker)

If the Python user agent can't interoperate, Asterisk will register account
`you-bot` with `chan_pjsip` (outbound registration, `transport=tls`,
`media_encryption=sdes`, `ice_support=yes`) and the bridge will drive it through ARI
(`POST /channels` to originate, External Media to stream audio to Python).
Configuration files will go in `configuration/asterisk/` when that path is needed.

## Diagnostics

Until the UA exists, use Linphone Desktop to check connectivity to
sip.linphone.org from your network:

```powershell
Test-NetConnection sip.linphone.org -Port 5061   # TLS signalling reachable?
Resolve-DnsName _sips._tcp.sip.linphone.org -Type SRV
```
