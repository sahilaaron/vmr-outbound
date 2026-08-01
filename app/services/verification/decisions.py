"""What a verification outcome means for the Campaign pipeline (MVP-01E / #225).

One function decides whether an exact address may be accepted, and it is the only
place that decision is made. Everything upstream produces a normalized status;
everything downstream — the Verification Agent adapter, the Email Agent, the
pipeline projection — reads the decision rather than re-deriving verification
semantics from a precise status. Re-deriving it in two places is exactly how
"risky" quietly becomes "verified".

The rule this file exists to enforce, from AGENTS.md and the email-readiness
rules in #161: **only fresh, valid, non-role evidence is an accepted address.**
Catch-all, unknown, disposable, role-based, stale and conflicting are all real
answers, and none of them is verification. There is no branch below that turns
any of them into :attr:`VerificationDecision.ACCEPT`.

This module is verification-domain vocabulary, not Agent vocabulary. It does not
name execution states, job statuses, retry schedules or Agent identifiers —
Phase 2 owns all of those. It says what the mailbox evidence permits; the adapter
translates that into the shared Agent contract exactly once.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.models.enums import EmailPreciseStatus, VerificationFailureClass


class VerificationDecision(enum.StrEnum):
    """What the pipeline should do with one verified exact address.

    ``ACCEPT`` — fresh valid non-role mailbox. Commit the evidence, complete the
    Verification stage, and let the Campaign Contact advance. The Email Agent
    stops generating candidates.

    ``TRY_NEXT_CANDIDATE`` — a definitive answer that is not an accepted address:
    invalid, disposable, catch-all, unknown or role-based. The result is real and
    is recorded, but the stage must not read as verified for outreach, and
    retrying this address would only spend credit. The Email Agent moves to its
    next allowed format.

    ``RETRY_LATER`` — no answer yet and another attempt is permitted. The domain
    only classifies this; the Phase 2 queue owns whether and when a retry
    actually runs.

    ``STOP_NO_RESULT`` — no answer and no further attempt will help: exhausted
    credits, a credential or configuration condition needing an operator, or
    evidence that has gone stale or self-contradictory and cannot settle itself.
    Finish honestly with no verified address.

    ``REFUSED`` — declined before any provider work: a malformed address, a
    suppressed identity, a policy-version mismatch, or verification not being
    live-authorised. No credit was spent and none will be until the cause is
    fixed.
    """

    ACCEPT = "accept"
    TRY_NEXT_CANDIDATE = "try_next_candidate"
    RETRY_LATER = "retry_later"
    STOP_NO_RESULT = "stop_no_result"
    REFUSED = "refused"


# Definitive answers about the mailbox that are nonetheless not an accepted
# address. Each is a real verdict, so no retry is warranted.
DEFINITIVE_NOT_ACCEPTED: frozenset[EmailPreciseStatus] = frozenset(
    {
        EmailPreciseStatus.INVALID,
        EmailPreciseStatus.DISPOSABLE,
        EmailPreciseStatus.CATCH_ALL,
        EmailPreciseStatus.UNKNOWN,
        EmailPreciseStatus.ROLE_BASED,
    }
)

# Statuses that constitute a verdict about a mailbox at all, as opposed to an
# operational condition. Only these are re-checked for staleness and conflict.
ADDRESS_VERDICTS: frozenset[EmailPreciseStatus] = frozenset(
    {EmailPreciseStatus.VALID} | DEFINITIVE_NOT_ACCEPTED
)

# What the address read model may say that overrides a recorded verdict: the
# evidence has aged out, or two fresh results disagree. Both mean the address is
# no longer settled, and neither may be reported as an accepted address.
UNSETTLED_EVIDENCE: frozenset[EmailPreciseStatus] = frozenset(
    {
        EmailPreciseStatus.STALE_EVIDENCE,
        EmailPreciseStatus.CONFLICTING_EVIDENCE,
    }
)

# Statuses that mean verification has not finished rather than that it failed.
IN_FLIGHT: frozenset[EmailPreciseStatus] = frozenset(
    {
        EmailPreciseStatus.UNVERIFIED,
        EmailPreciseStatus.QUEUED,
        EmailPreciseStatus.CHECKING,
        EmailPreciseStatus.RETRY_SCHEDULED,
        EmailPreciseStatus.STALE_RECHECK_SCHEDULED,
    }
)

_REASONS: dict[EmailPreciseStatus, str] = {
    EmailPreciseStatus.INVALID: "the provider confirmed this mailbox does not exist",
    EmailPreciseStatus.DISPOSABLE: "this is a disposable or temporary mailbox",
    EmailPreciseStatus.CATCH_ALL: (
        "the domain accepts all mail, so this mailbox is unproven and not scheduling-ready"
    ),
    EmailPreciseStatus.UNKNOWN: "the provider could not determine whether this mailbox exists",
    EmailPreciseStatus.ROLE_BASED: (
        "this mailbox is valid but role-based, so it is not an individual outreach target"
    ),
    EmailPreciseStatus.STALE_EVIDENCE: (
        "this address was verified, but the evidence has aged past its freshness policy; "
        "a forced refresh is required before it can be accepted"
    ),
    EmailPreciseStatus.CONFLICTING_EVIDENCE: (
        "fresh evidence for this address disagrees; it is not treated as verified until "
        "a forced refresh settles it"
    ),
    EmailPreciseStatus.INSUFFICIENT_CREDITS: (
        "the provider reported insufficient credits; top up and re-run"
    ),
    EmailPreciseStatus.PROVIDER_ERROR: "the provider did not return a verdict",
}


@dataclass(frozen=True)
class DecisionOutcome:
    """A decision plus the truthful reason recorded against the pipeline stage."""

    decision: VerificationDecision
    status: EmailPreciseStatus
    # Short slug for the Agent Job's error class and the pipeline reason code.
    reason_code: str
    # Operator-readable sentence. Never carries a credential.
    reason: str

    @property
    def accepted(self) -> bool:
        """True only for a fresh, valid, non-role mailbox."""

        return self.decision is VerificationDecision.ACCEPT


def decide(
    status: EmailPreciseStatus,
    *,
    failure_class: VerificationFailureClass = VerificationFailureClass.NONE,
    retry_available: bool = False,
    simulated: bool = False,
) -> DecisionOutcome:
    """Classify one normalized verification status for the pipeline.

    ``retry_available`` is the caller's statement that the attempt budget still
    permits another try. The domain never schedules the retry itself; it only
    says whether another one could help.

    ``simulated`` refuses acceptance outright. A simulator result is a real
    normalized outcome and is stored as such, but it is not external verification
    and must never advance a production Campaign Contact.
    """

    if status is EmailPreciseStatus.VALID:
        if simulated:
            return DecisionOutcome(
                VerificationDecision.REFUSED,
                status,
                "verification_simulated",
                "simulated verification cannot advance a Campaign Contact; "
                "live evidence from an enabled verification provider is required",
            )
        return DecisionOutcome(
            VerificationDecision.ACCEPT,
            status,
            "verified",
            "fresh live evidence confirms this mailbox exists",
        )

    if status in DEFINITIVE_NOT_ACCEPTED:
        return DecisionOutcome(
            VerificationDecision.TRY_NEXT_CANDIDATE,
            status,
            f"verification_{status.value}",
            _REASONS[status],
        )

    if status in UNSETTLED_EVIDENCE:
        # A real answer that can no longer be trusted. Retrying the same finished
        # work would spin, so this stops rather than pretending it can recover.
        return DecisionOutcome(
            VerificationDecision.STOP_NO_RESULT,
            status,
            f"verification_{status.value}",
            _REASONS[status],
        )

    if status in IN_FLIGHT:
        return DecisionOutcome(
            VerificationDecision.RETRY_LATER,
            status,
            "verification_incomplete",
            "verification has not reached a verdict yet",
        )

    if failure_class is VerificationFailureClass.TRANSIENT_PROVIDER and retry_available:
        return DecisionOutcome(
            VerificationDecision.RETRY_LATER,
            status,
            "verification_transient",
            _REASONS.get(status, "the provider failed transiently"),
        )

    return DecisionOutcome(
        VerificationDecision.STOP_NO_RESULT,
        status,
        f"verification_{failure_class.value}",
        _REASONS.get(status, "verification produced no usable result"),
    )


def refusal(reason_code: str, reason: str) -> DecisionOutcome:
    """A refusal decided before any provider work."""

    return DecisionOutcome(
        VerificationDecision.REFUSED,
        EmailPreciseStatus.UNVERIFIED,
        reason_code,
        reason,
    )
