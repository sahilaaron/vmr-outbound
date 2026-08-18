"""The one place that decides whether a verification provider settled an address.

VER-02 adds DeBounce behind MillionVerifier as a *fallback*, not as a second
opinion. That distinction only stays true if exactly one rule answers the
question "did this provider produce an authoritative usable verdict?" — so the
rule lives here rather than as an inline set literal inside the traversal loop,
and every caller reads the same answer.

The taxonomy below is the existing canonical vocabulary re-expressed once for
the fallback decision. It does not replace :class:`VerificationFailureClass` or
:class:`EmailPreciseStatus`; it is derived from them plus the transport
condition the adapter already knows, so there is no second competing model of
what went wrong.

Authoritative means "this provider answered the question about the mailbox".
It does **not** mean "this provider answered the way we wanted". A confirmed
INVALID is authoritative and stops the traversal: asking a second vendor whether
it disagrees would be shopping for a preferred answer, would spend a credit to
do it, and is explicitly not what this feature is for.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol

from app.models.enums import EmailPreciseStatus, VerificationFailureClass


class ProviderCondition(enum.StrEnum):
    """Why a provider step ended, at the granularity the fallback rule needs.

    ``AUTHORITATIVE`` — a settled verdict about this mailbox (A).
    ``TRANSPORT_FAILURE`` — no HTTP answer at all: timeout, DNS, reset, 5xx (B).
    ``ACCESS_REJECTED`` — the provider refused the credential or the plan (C).
    ``EXHAUSTED_CREDITS`` — the account has nothing left to spend (C).
    ``THROTTLED`` — rate or concurrency limited; the provider deferred us (D).
    ``UNRESOLVED_RESULT`` — a technically successful call whose verdict the
    active policy cannot treat as settled: catch-all and unknown (E).
    ``UNUSABLE_RESPONSE`` — a response we cannot parse into any verdict:
    a missing or unrecognised classification, ``success = 0`` (F). Permanent for
    the provider that sent it — the same reply parses the same way next time —
    but still worth asking the *next* provider, which is one more call rather
    than one more call per retry.
    ``NOT_ATTEMPTED`` — refused before provider work, e.g. no address on the
    job. Nothing downstream may fall back on this; there is no question to ask.
    """

    AUTHORITATIVE = "authoritative"
    TRANSPORT_FAILURE = "transport_failure"
    ACCESS_REJECTED = "access_rejected"
    EXHAUSTED_CREDITS = "exhausted_credits"
    THROTTLED = "throttled"
    UNRESOLVED_RESULT = "unresolved_result"
    UNUSABLE_RESPONSE = "unusable_response"
    NOT_ATTEMPTED = "not_attempted"


#: Address verdicts the active VMR policy already treats as settled. Catch-all
#: and unknown are deliberately absent: the product has always held them as
#: uncertain (AGENTS.md), so they are the honest fallback trigger rather than a
#: new VER-02 opinion about what those states mean.
AUTHORITATIVE_STATUSES: frozenset[EmailPreciseStatus] = frozenset(
    {
        EmailPreciseStatus.VALID,
        EmailPreciseStatus.INVALID,
        EmailPreciseStatus.DISPOSABLE,
        EmailPreciseStatus.ROLE_BASED,
    }
)

#: Conditions under which a *later* provider may be asked the same question.
#: Every one of them means the provider could not answer, never that it did.
FALLBACK_ELIGIBLE: frozenset[ProviderCondition] = frozenset(
    {
        ProviderCondition.TRANSPORT_FAILURE,
        ProviderCondition.ACCESS_REJECTED,
        ProviderCondition.EXHAUSTED_CREDITS,
        ProviderCondition.THROTTLED,
        ProviderCondition.UNRESOLVED_RESULT,
        ProviderCondition.UNUSABLE_RESPONSE,
    }
)

_REASONS: dict[ProviderCondition, str] = {
    ProviderCondition.TRANSPORT_FAILURE: "the provider could not be reached",
    ProviderCondition.ACCESS_REJECTED: "the provider rejected the configured credential",
    ProviderCondition.EXHAUSTED_CREDITS: "the provider reported no remaining credits",
    ProviderCondition.THROTTLED: "the provider rate-limited the request",
    ProviderCondition.UNRESOLVED_RESULT: (
        "the provider answered but the result is not a settled mailbox verdict"
    ),
    ProviderCondition.UNUSABLE_RESPONSE: "the provider response could not be interpreted",
    ProviderCondition.NOT_ATTEMPTED: "no provider call was attempted",
}


@dataclass(frozen=True)
class FallbackAssessment:
    """One provider step, classified for the fallback decision."""

    condition: ProviderCondition
    reason: str

    @property
    def authoritative(self) -> bool:
        return self.condition is ProviderCondition.AUTHORITATIVE

    @property
    def fallback_eligible(self) -> bool:
        """Whether a later provider may now be asked about the same address."""

        return self.condition in FALLBACK_ELIGIBLE


class ClassifiableOutcome(Protocol):
    """The four attributes :func:`assess` reads off a verification outcome.

    Declared structurally rather than imported: ``service`` imports this module,
    so depending on ``service.VerificationOutcome`` here would be a cycle.
    """

    @property
    def precise(self) -> EmailPreciseStatus: ...

    @property
    def failure_class(self) -> VerificationFailureClass: ...

    @property
    def condition(self) -> str | None: ...

    @property
    def reason(self) -> str | None: ...


def assess(outcome: ClassifiableOutcome) -> FallbackAssessment:
    """Classify one completed provider step. The only fallback rule there is."""

    declared = outcome.condition
    if declared is not None:
        try:
            condition = ProviderCondition(declared)
        except ValueError:
            condition = ProviderCondition.UNUSABLE_RESPONSE
        else:
            if condition is not ProviderCondition.AUTHORITATIVE:
                return FallbackAssessment(condition, outcome.reason or _REASONS[condition])

    if outcome.precise in AUTHORITATIVE_STATUSES:
        return FallbackAssessment(
            ProviderCondition.AUTHORITATIVE,
            outcome.reason or "the provider settled this mailbox",
        )
    if outcome.precise in {EmailPreciseStatus.CATCH_ALL, EmailPreciseStatus.UNKNOWN}:
        condition = ProviderCondition.UNRESOLVED_RESULT
    elif outcome.precise is EmailPreciseStatus.INSUFFICIENT_CREDITS:
        condition = ProviderCondition.EXHAUSTED_CREDITS
    elif outcome.failure_class is VerificationFailureClass.INVALID_INPUT:
        condition = ProviderCondition.NOT_ATTEMPTED
    elif outcome.failure_class is VerificationFailureClass.PERMANENT_PROVIDER:
        condition = ProviderCondition.ACCESS_REJECTED
    else:
        condition = ProviderCondition.TRANSPORT_FAILURE
    return FallbackAssessment(condition, outcome.reason or _REASONS[condition])
