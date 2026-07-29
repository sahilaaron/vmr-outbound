# Phase 2 Email Agent

Issue #224 implements policy-bounded work-email discovery on the common Phase 2
Agent lifecycle. The Email Agent generates candidates; the Verification Agent
decides what each exact address means.

## Versioned discovery policy

Policy `policy-bounded-work-email`, version `email-discovery-v1`, classifies
sourced Company employee-count evidence into exactly three values:

| Classification | Evidence | Ordered formats |
| --- | --- | --- |
| `more_than_50` | integer count greater than 50 | `firstname.lastname`, `finitiallastname`, `lastnamefinitial` |
| `50_or_fewer` | integer count from 0 through 50 | `firstname`, `firstname.lastname`, `finitiallastname` |
| `unknown` | absent, unparseable, contradictory, or unsourced count | no candidates; explicit block |

The policy appends the canonical normalized Company domain, normalizes through
the existing `eml-1` identity rules, removes equivalent normalized addresses
while preserving the first occurrence, and never produces more than three.
There are no fallback permutations.

The stored execution records the policy identifier/version, classification,
Company field-evidence row and source reference, evidence timestamp/freshness,
domain, ordered formats, and normalized candidates. `email-discovery-v1`
considers employee evidence stale after 180 days or whenever the Company's
existing research state is `stale`; changing that rule requires a new policy
version.

## Durable orchestration

`AgentJob` remains the only queue and worker lifecycle. One
`EmailCandidateAttempt` records the Email-specific facts for one locked
candidate:

- immutable policy, employee-evidence, domain, format, and index;
- the existing `EmailCandidate` reference;
- exactly one requesting relationship to a child Verification Agent Job;
- the committed authoritative `VerificationDecision`;
- exact Verification evidence, refusal, and timestamps.

The database bounds candidate indexes to 0–2, prevents duplicate candidate
indexes or addresses within an Email job, permits one Verification child per
attempt, and permits only one accepted attempt per Email job.

The execution sequence is:

1. Lock and recheck the permanent Contact, Campaign eligibility, Company/domain
   gate, current suppression, employee evidence, and existing accepted email.
2. Persist the versioned candidate plan and current attempt.
3. Idempotently enqueue one Verification child on the shared queue and pause the
   Email parent as `waiting_on_verification`.
4. Let the common worker claim and execute the child through the authoritative
   `VerificationAgentAdapter`.
5. Wake the Email parent only after the child transaction commits a decision.
6. Read that decision from the child's committed result/error detail:
   - `ACCEPT`: validate exact fresh live evidence, recheck suppression, write
     through the permanent Contact plus attempt/audit provenance, and stop.
   - `TRY_NEXT_CANDIDATE`: preserve the failed attempt and enqueue only the next
     locked format.
   - `RETRY_LATER`: keep the same candidate; the child queue owns backoff.
   - `STOP_NO_RESULT`: finish truthfully without an address.
   - `REFUSED`: block/stop; simulated evidence can never be production-ready.

The Email Agent never calls MillionVerifier, never invokes `process_job()`, and
never claims its child inline. Verification children use the common lease,
retry, recovery, parent-job, attempt-evidence, and provider-cost contracts.

## Existing accepted email

A current Contact email is reused only when it belongs to the same Contact,
matches the canonical Company domain, has fresh valid non-role live
`ExactEmailVerification`, and is not suppressed. The execution records reuse and
creates no candidate, child job, provider call, or duplicate evidence row.

A forced refresh requires `force_refresh=true` plus a non-empty,
idempotently-scoped `refresh_scope`. Both are durable and auditable.

## Campaign projection

For Campaign work, Email runs only after its configured dependencies and
controls permit it. A live accepted child (or eligible reused evidence) completes
the Email stage and projects the same committed evidence onto the Verification
stage. No-result, unknown Company size, stale evidence, refusal, simulation,
suppression, and disabled controls do not advance the Campaign Contact.

Nested Verification lifecycle events are append-only but do not mutate the
top-level Verification stage while Email is still running. This prevents the
child from bypassing the Email dependency. Campaign/global controls are rechecked
before provider work and again before the accepted Contact write.

## Read model and migration

`GET /api/agent-jobs/{job_id}/email-attempts` exposes the authoritative ordered
attempt ledger for later Workbench consumption. No Workbench templates or
operator UI are part of this change.

Migration `d2f6c8a104be` descends from the Verification Agent migration
`b9d4e7a15c38`. It adds only `email_candidate_attempts`; downgrade refuses while
irreconstructable attempt history exists.
