"""Versioned, policy-bounded work-email discovery rules.

This module owns the locked Issue #224 decision boundary.  It is deliberately
pure: database services supply one sourced employee-count observation plus the
canonical Contact/Company inputs, and the policy returns either a complete,
ordered candidate plan or one truthful refusal.

The broader historical pattern engine remains available for its original
callers.  The production Email Agent does not use that open-ended ranking list:
it uses only the three formats authorized here, in this exact order.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import datetime

from app.services.email.normalization import ENGINE_VERSION, build_identity
from app.services.imports.normalization import (
    is_valid_email,
    is_valid_hostname,
    normalize_domain,
)

POLICY_IDENTIFIER = "policy-bounded-work-email"
POLICY_VERSION = "email-discovery-v1"


class EmployeeCountClass(enum.StrEnum):
    """The only employee-count classifications the policy may use."""

    MORE_THAN_50 = "more_than_50"
    FIFTY_OR_FEWER = "50_or_fewer"
    UNKNOWN = "unknown"


class EmployeeEvidenceFreshness(enum.StrEnum):
    """Whether the source evidence is usable by this policy version."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class EmailPolicyOutcome(enum.StrEnum):
    """Pure policy outcome before orchestration or provider work."""

    READY = "ready"
    EXISTING_ACCEPTED_EMAIL_REUSE = "existing_accepted_email_reuse"
    EMPLOYEE_COUNT_UNKNOWN = "employee_count_unknown"
    EMPLOYEE_COUNT_STALE = "employee_count_stale"
    UNUSABLE_FIRST_NAME = "unusable_first_name"
    UNUSABLE_LAST_NAME = "unusable_last_name"
    DOMAIN_INELIGIBLE = "domain_ineligible"


@dataclass(frozen=True)
class EmployeeCountEvidence:
    """The current sourced Company employee-count observation."""

    evidence_id: str | None
    raw_value: str | None
    source_reference: str | None
    observed_at: datetime | None
    ingested_at: datetime | None
    source_policy_version: str | None
    source_marked_stale: bool = False

    @property
    def effective_at(self) -> datetime | None:
        """Use the same observed-at/ingested-at fallback as ``freshness-v1``."""

        return self.observed_at or self.ingested_at


@dataclass(frozen=True)
class PolicyCandidate:
    """One exact candidate authorized by the locked policy."""

    format_id: str
    local_part: str
    email: str


@dataclass(frozen=True)
class EmailDiscoveryPolicyDecision:
    """Complete reproducible output of one policy evaluation."""

    outcome: EmailPolicyOutcome
    employee_count_class: EmployeeCountClass
    evidence: EmployeeCountEvidence
    evidence_freshness: EmployeeEvidenceFreshness
    normalized_domain: str | None
    ordered_formats: tuple[str, ...]
    candidates: tuple[PolicyCandidate, ...]
    normalization_version: str
    reason: str | None = None

    @property
    def ready(self) -> bool:
        return self.outcome is EmailPolicyOutcome.READY


_NUMBER = r"\d[\d,]*"
_EXACT_RE = re.compile(rf"^(?P<count>{_NUMBER})$")
_RANGE_RE = re.compile(rf"^(?P<low>{_NUMBER})\s*(?:-|–|—|to)\s*(?P<high>{_NUMBER})$")
_PLUS_RE = re.compile(rf"^(?P<low>{_NUMBER})\s*\+$")
_GREATER_RE = re.compile(rf"^(?:>|morethan|over)\s*(?P<low>{_NUMBER})$")
_AT_MOST_RE = re.compile(rf"^(?:<=|upto|atmost)\s*(?P<high>{_NUMBER})$")

_LARGE_FORMATS = (
    "firstname.lastname",
    "finitiallastname",
    "lastnamefinitial",
)
_SMALL_FORMATS = (
    "firstname",
    "firstname.lastname",
    "finitiallastname",
)
#: Used when the Company's employee count is unknown, its evidence is stale, or
#: the Campaign has switched size off.
#:
#: This is the large-company order, and that is a judgement rather than a
#: fallback-by-accident: ``firstname.lastname`` is the most common corporate
#: pattern overall, so it is the least bad first guess when nothing is known. The
#: bare ``firstname`` that the small-company order leads with pays off at small
#: firms and misses badly at large ones, which makes it the wrong thing to guess
#: with.
_DEFAULT_FORMATS = _LARGE_FORMATS


def _count(value: str) -> int:
    return int(value.replace(",", ""))


def _normalized_size_text(raw_value: str | None) -> str:
    if raw_value is None:
        return ""
    value = raw_value.casefold().strip()
    value = value.replace("employees", "").replace("employee", "").strip()
    value = re.sub(r"\s+", "", value)
    for suffix in ("orfewer", "orless"):
        if value.endswith(suffix):
            value = f"atmost{value.removesuffix(suffix)}"
            break
    return value


def classify_employee_count(raw_value: str | None) -> EmployeeCountClass:
    """Classify only values that settle which side of 50 the Company is on.

    Exact counts and non-crossing ranges are safe.  A range such as ``1-200``
    crosses the threshold and remains unknown; descriptive labels such as
    ``small`` are also unknown.  The policy never fills those gaps by guessing.
    """

    value = _normalized_size_text(raw_value)
    if not value:
        return EmployeeCountClass.UNKNOWN

    exact = _EXACT_RE.fullmatch(value)
    if exact:
        return (
            EmployeeCountClass.MORE_THAN_50
            if _count(exact.group("count")) > 50
            else EmployeeCountClass.FIFTY_OR_FEWER
        )

    range_match = _RANGE_RE.fullmatch(value)
    if range_match:
        low = _count(range_match.group("low"))
        high = _count(range_match.group("high"))
        if low > high:
            return EmployeeCountClass.UNKNOWN
        if low > 50:
            return EmployeeCountClass.MORE_THAN_50
        if high <= 50:
            return EmployeeCountClass.FIFTY_OR_FEWER
        return EmployeeCountClass.UNKNOWN

    plus = _PLUS_RE.fullmatch(value)
    if plus:
        # "50+" includes 50 and values above it, so it crosses the boundary.
        return (
            EmployeeCountClass.MORE_THAN_50
            if _count(plus.group("low")) > 50
            else EmployeeCountClass.UNKNOWN
        )

    greater = _GREATER_RE.fullmatch(value)
    if greater:
        return (
            EmployeeCountClass.MORE_THAN_50
            if _count(greater.group("low")) >= 50
            else EmployeeCountClass.UNKNOWN
        )

    at_most = _AT_MOST_RE.fullmatch(value)
    if at_most:
        return (
            EmployeeCountClass.FIFTY_OR_FEWER
            if _count(at_most.group("high")) <= 50
            else EmployeeCountClass.UNKNOWN
        )

    return EmployeeCountClass.UNKNOWN


def evidence_freshness(
    evidence: EmployeeCountEvidence,
    *,
    now: datetime,
) -> EmployeeEvidenceFreshness:
    """Honor the shared winner plus its explicit Company freshness state.

    ``freshness-v1`` determines which Company field observation is current; it
    intentionally defines no age TTL. Email must not invent one. The separate
    Company research lifecycle owns an explicit ``stale`` state, supplied here
    as ``source_marked_stale``.
    """

    if evidence.evidence_id is None or evidence.raw_value is None:
        return EmployeeEvidenceFreshness.UNKNOWN
    effective_at = evidence.effective_at
    if effective_at is None:
        return EmployeeEvidenceFreshness.UNKNOWN
    if effective_at.tzinfo is None or now.tzinfo is None:
        return EmployeeEvidenceFreshness.UNKNOWN
    if evidence.source_marked_stale:
        return EmployeeEvidenceFreshness.STALE
    return EmployeeEvidenceFreshness.FRESH


def _local_part(format_id: str, *, first: str, last: str, first_initial: str) -> str:
    return {
        "firstname": first,
        "firstname.lastname": f"{first}.{last}",
        "finitiallastname": f"{first_initial}{last}",
        "lastnamefinitial": f"{last}{first_initial}",
    }[format_id]


def evaluate(
    *,
    first_name: str | None,
    last_name: str | None,
    domain: str | None,
    employee_evidence: EmployeeCountEvidence,
    now: datetime,
    consult_employee_size: bool = True,
) -> EmailDiscoveryPolicyDecision:
    """Return the exact candidate plan or one explicit policy refusal.

    ``consult_employee_size`` is the Campaign's answer to whether company size
    should choose the format order at all. When false every Contact gets
    :data:`_DEFAULT_FORMATS` and the recorded classification is ``unknown``,
    which is truthful: the policy did not consult it.
    """

    normalized_domain = normalize_domain(domain)
    count_class = classify_employee_count(employee_evidence.raw_value)
    freshness = evidence_freshness(employee_evidence, now=now)
    empty_formats: tuple[str, ...] = ()
    empty_candidates: tuple[PolicyCandidate, ...] = ()

    if (
        normalized_domain is None
        or not is_valid_hostname(normalized_domain)
        or normalized_domain != domain
    ):
        return EmailDiscoveryPolicyDecision(
            outcome=EmailPolicyOutcome.DOMAIN_INELIGIBLE,
            employee_count_class=count_class,
            evidence=employee_evidence,
            evidence_freshness=freshness,
            normalized_domain=normalized_domain,
            ordered_formats=empty_formats,
            candidates=empty_candidates,
            normalization_version=ENGINE_VERSION,
            reason="the canonical Company domain is missing, malformed, or not normalized",
        )

    identity = build_identity(first_name, last_name)
    if not identity.first or not identity.first_initial:
        return EmailDiscoveryPolicyDecision(
            outcome=EmailPolicyOutcome.UNUSABLE_FIRST_NAME,
            employee_count_class=count_class,
            evidence=employee_evidence,
            evidence_freshness=freshness,
            normalized_domain=normalized_domain,
            ordered_formats=empty_formats,
            candidates=empty_candidates,
            normalization_version=ENGINE_VERSION,
            reason="the Contact first name has no supported normalized email token",
        )
    if not identity.last:
        return EmailDiscoveryPolicyDecision(
            outcome=EmailPolicyOutcome.UNUSABLE_LAST_NAME,
            employee_count_class=count_class,
            evidence=employee_evidence,
            evidence_freshness=freshness,
            normalized_domain=normalized_domain,
            ordered_formats=empty_formats,
            candidates=empty_candidates,
            normalization_version=ENGINE_VERSION,
            reason="the Contact last name has no supported normalized email token",
        )
    # Employee count picks the *order* of three formats. It never picked how many,
    # and it is not evidence about whether a mailbox exists — so not knowing it is
    # not a reason to refuse to look.
    #
    # It used to be. An unknown or stale count returned zero candidates, which
    # meant a Contact at a company whose size nobody had sourced could never have
    # an address discovered or verified at all. Since size is only sourced by
    # company research, and research is optional, the common case silently
    # produced nothing — a policy refusal that read, downstream, as "no address
    # could be found". Unknown size now falls back to a default order and says so
    # in the recorded classification.
    if not consult_employee_size:
        count_class = EmployeeCountClass.UNKNOWN
        formats = _DEFAULT_FORMATS
    elif (
        freshness is not EmployeeEvidenceFreshness.FRESH
        or count_class is EmployeeCountClass.UNKNOWN
    ):
        # Stale and unknown are recorded distinctly on the attempt row; both take
        # the default order. A stale count is still a real observation, just not
        # one this policy will act on.
        count_class = EmployeeCountClass.UNKNOWN
        formats = _DEFAULT_FORMATS
    elif count_class is EmployeeCountClass.MORE_THAN_50:
        formats = _LARGE_FORMATS
    else:
        formats = _SMALL_FORMATS
    candidates: list[PolicyCandidate] = []
    seen: set[str] = set()
    produced_formats: list[str] = []
    for format_id in formats:
        local = _local_part(
            format_id,
            first=identity.first,
            last=identity.last,
            first_initial=identity.first_initial,
        )
        email = f"{local}@{normalized_domain}".lower()
        if email in seen or not is_valid_email(email):
            continue
        seen.add(email)
        produced_formats.append(format_id)
        candidates.append(
            PolicyCandidate(
                format_id=format_id,
                local_part=local,
                email=email,
            )
        )

    if not candidates:
        # Defensive: the explicit first/last/domain checks above make this
        # unreachable for current formats, but the outcome remains truthful if a
        # future policy version changes them.
        return EmailDiscoveryPolicyDecision(
            outcome=EmailPolicyOutcome.UNUSABLE_FIRST_NAME,
            employee_count_class=count_class,
            evidence=employee_evidence,
            evidence_freshness=freshness,
            normalized_domain=normalized_domain,
            ordered_formats=empty_formats,
            candidates=empty_candidates,
            normalization_version=ENGINE_VERSION,
            reason="the normalized identity produced no valid exact-address candidate",
        )

    return EmailDiscoveryPolicyDecision(
        outcome=EmailPolicyOutcome.READY,
        employee_count_class=count_class,
        evidence=employee_evidence,
        evidence_freshness=freshness,
        normalized_domain=normalized_domain,
        ordered_formats=tuple(produced_formats),
        candidates=tuple(candidates[:3]),
        normalization_version=ENGINE_VERSION,
    )


def evaluate_existing_accepted_email_reuse(
    *,
    domain: str | None,
    employee_evidence: EmployeeCountEvidence,
    now: datetime,
    consult_employee_size: bool = True,
) -> EmailDiscoveryPolicyDecision:
    """Authorize reuse without inventing a candidate-policy branch.

    A fresh, accepted exact address does not need name components or candidate
    formats. It also does not need a company size: the address is already
    verified, so refusing to reuse it because nobody sourced a headcount would
    discard evidence that has been paid for. Unknown and stale size are recorded
    truthfully as ``unknown`` and do not block. A malformed domain still does —
    that one is about whether the address means anything at all.
    """

    normalized_domain = normalize_domain(domain)
    count_class = classify_employee_count(employee_evidence.raw_value)
    freshness = evidence_freshness(employee_evidence, now=now)
    empty: tuple[str, ...] = ()
    empty_candidates: tuple[PolicyCandidate, ...] = ()

    if (
        normalized_domain is None
        or not is_valid_hostname(normalized_domain)
        or normalized_domain != domain
    ):
        outcome = EmailPolicyOutcome.DOMAIN_INELIGIBLE
        reason = "the canonical Company domain is missing, malformed, or not normalized"
    else:
        if (
            not consult_employee_size
            or freshness is not EmployeeEvidenceFreshness.FRESH
            or count_class is EmployeeCountClass.UNKNOWN
        ):
            count_class = EmployeeCountClass.UNKNOWN
        outcome = EmailPolicyOutcome.EXISTING_ACCEPTED_EMAIL_REUSE
        reason = "fresh production-eligible exact-address evidence makes discovery unnecessary"

    return EmailDiscoveryPolicyDecision(
        outcome=outcome,
        employee_count_class=count_class,
        evidence=employee_evidence,
        evidence_freshness=freshness,
        normalized_domain=normalized_domain,
        ordered_formats=empty,
        candidates=empty_candidates,
        normalization_version=ENGINE_VERSION,
        reason=reason,
    )
