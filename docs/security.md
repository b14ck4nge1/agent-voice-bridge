# Security model

This system lets a phone call authorise actions on your computer, so it is
designed to fail closed. This page describes what is implemented in this snapshot,
what isn't, and the residual risks.

## Threats considered

| Threat | Mitigation (implemented) |
|---|---|
| Another local process approves a request | Decisions need the **voice token**; the API binds to `127.0.0.1` by default and refuses other addresses unless `AVB_ALLOW_REMOTE_BIND=1`. |
| The voice assistant invents or alters a request | The voice token **cannot create** requests. Decisions carry the request's **fingerprint** (SHA-256 of agent, session, workspace, tool and full input), checked in constant time. The agent can't receive changed arguments: the hook never returns `updatedInput`. |
| One approval reused for another request, or replayed | Per-decision **nonce** (unique in the database); fingerprint + `call_id` binding; terminal states are final. |
| One spoken "yes" approving future operations | No `updatedPermissions` and no "always allow" rules; Codex `acceptForSession` is never sent. Each operation needs its own call decision. |
| A request approved after the user was asked something else | Decisions only in `AWAITING_DECISION`, only for the presented request, and only after a read-back confirmation. |
| Prompt injection via command text or agent "reason" | The rule-based engine never parses request content for intents. Agent text is introduced as unverified ("The agent's own explanation, which I can't verify, is ..."). |
| Someone else answers the phone | High/critical-risk approvals need a **TOTP code** (RFC 6238, codes single-use). `AVB_HIGH_RISK_POLICY=deny` makes them reject-only. Rejecting needs no code. |
| Another agent session reads or cancels my request | Each request has a client-generated **request secret** (stored as a SHA-256 hash), required for wait and cancel. A wrong secret looks identical to an unknown id. |
| Bridge down, timeout, crash, missed call | `avb-hook` returns **deny** (or "no decision" in `ask` mode, leaving the agent's own prompt). Requests expire (`AVB_DEFAULT_TTL_SECONDS`, max `AVB_MAX_TTL_SECONDS`). Missed calls retry then **FAILED**, which means deny. |
| Secrets leaking into logs, the database or speech | Tool input is **redacted before storage** (keys, tokens, passwords, URL credentials, private keys, JWTs); only the redacted copy and the fingerprint are kept. The audit log is redacted too. |
| Hardcoded or weak tokens | No default tokens. `validate()` requires ≥32-character, distinct agent and voice tokens. `avb-admin init` generates them with `secrets`. `bridge.env`/`hook.env` are created with mode 0600 (on Windows, protected by your user profile ACLs). |
| Plain HTTP across a network | `avb-hook` refuses `http://` except to loopback. |

## Not implemented yet

* HTTPS termination for a remote bridge (put a reverse proxy such as Caddy in
  front; task SEC-03). The default is loopback only.
* SIP media/signalling security is the UA's job: TLS + SRTP are planned (SIP-01),
  not implemented.
* Rate limiting of decision attempts (SEC-04). TOTP guessing is bounded by the
  conversation engine (2 tries per request) but not yet by the API.
* An option to require TOTP for every approval (SEC-05).
* Token rotation without a restart.

## Residual risks

* **Voice is not identity.** Anyone holding your unlocked phone during a call can
  reject anything, and approve low/medium-risk requests.
* **Risk rating is heuristic.** "medium" is not "safe"; the assistant says so.
* **Redaction is best effort.** Unusual secret formats may slip through to the
  spoken summary and the audit log.
* **Third-party services.** Calls transit sip.linphone.org (Belledonne
  Communications). Signalling is TLS and media SRTP between endpoints, but the SIP
  provider sees call metadata.

## Reporting

See [SECURITY.md](../SECURITY.md).
