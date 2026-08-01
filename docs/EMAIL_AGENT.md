# Phase 2 Email Agent

Issue #224 implements policy-bounded work-email discovery on the common Phase 2
Agent lifecycle. The Email Agent generates candidates; the Verification Agent
decides what each exact address means.

## Versioned discovery policy

The active immutable `email-pattern-policy/v1` version orders a bounded subset
of `firstname.lastname`, `finitiallastname`, and `lastnamefinitial`. A valid
learned format from accepted, live, non-role evidence on the same Company domain
may be placed first. The policy then appends the canonical normalized Company
domain, normalizes through the existing identity rules, removes equivalent
addresses while preserving first occurrence, and never exceeds its configured
candidate bound. There are no ungoverned fallback permutations.

Employee Size is not an input to candidate selection, ordering, count, blocking
or execution. INS-002 may derive it for Insights, but Email and Verification do
not consume it.

## Durable orchestration

`AgentJob` remains the only queue and worker lifecycle. One
`EmailCandidateAttempt` records the Email-specific facts for one locked
candidate:

- immutable policy, domain, format, and index;
- the existing `EmailCandidate` reference;
- exactly one requesting relationship to a child Verification Agent Job;
- the committed authoritative `VerificationDecision`;
- exact Verification evidence, refusal, and timestamps.

The database bounds candidate indexes to 0–2, prevents duplicate candidate
indexes or addresses within an Email job, permits one Verification child per
attempt, and permits only one accepted attempt per Email job.

The execution sequence is:

1. Lock and recheck the permanent Contact, Campaign eligibility, Company/domain
   gate, current suppression, active pattern policy, and existing accepted email.
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
stage. No-result, refusal, simulation, suppression, and disabled controls do not
advance the Campaign Contact. Employee Size has no transition authority.

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
