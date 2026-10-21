# ADR-0006: Optional public server so the phone needs only Bark and Linphone

Status: Accepted. Date: 2026-10-20.

## Context

The user wants no VPN app on the iPhone (Tailscale must stay connected) and has a
server and a domain. Cloudflare's free proxy carries only HTTP(S), so SIP can't go
through it; DNS on Cloudflare (grey cloud) is fine.

## Decision

* The bridge can run on a public Ubuntu server (Docker, host networking) behind
  Caddy, which obtains a Let's Encrypt certificate and proxies only the agent
  endpoints (`/healthz`, `/v1/approvals*`); voice endpoints stay on localhost.
* The SIP server reuses Caddy's certificate for SIP over TLS (5061), refuses plain
  UDP/TCP and requires SDES-SRTP. A public bind address is rejected unless all
  three hold.
* PC hooks reach the server over HTTPS with the agent token.

## Consequences

* Phone: Bark + Linphone only; works on any network.
* Requests (redacted) are stored on the user's server rather than the PC.
* Speech on the server uses espeak-ng (robotic) until a better TTS is added.
* SDES keys travel inside TLS-protected SIP; media is SRTP end to end between phone
  and server.
