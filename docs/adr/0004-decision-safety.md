# ADR-0004: How a spoken decision becomes an approval

Status: Accepted. Date: 2026-10-20.

## Decision

A decision is accepted only if all of the following hold. They are enforced in
`ApprovalService.decide`, not in the voice layer.

1. The request is in `AWAITING_DECISION`: the assistant has presented it on this call.
2. The decision names the active `call_id`.
3. Its fingerprint equals the stored fingerprint of the exact request (constant-time
   comparison).
4. `confirmed` is true. The conversation engine sets it only after the user said yes
   to a read-back of that specific request.
5. The nonce has never been used before. An identical retry is idempotent; anything
   else is refused as a replay.
6. The request hasn't expired.
7. For HIGH/CRITICAL risk approvals, a valid, unused TOTP code (RFC 6238) is given,
   unless `AVB_HIGH_RISK_POLICY=voice`. With `deny`, high-risk requests can only be
   rejected by phone.

The conversation engine takes decisions only from the user's utterances matched by
conservative rules. Text inside the request (command, agent reason) is read out as
data and is never parsed for intents. An unknown utterance never decides anything.

## Consequences

* Rejecting is always possible without a second factor; approving risky things is
  deliberately harder.
* Voice is not authentication. Anyone who answers the phone can reject, or approve
  low/medium-risk requests. Users who need more can set the policy so every
  approval needs TOTP (task SEC-05).
