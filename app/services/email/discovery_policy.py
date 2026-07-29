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
from datetime import UTC, datetime, timedelta

from app.services.email.normalization import ENGINE_VERSION, build_identity
from app.services.imports.normalization import (
    is_valid_email,
    is_valid_hostname,
    normalize_domain,
)

POLICY_IDENTIFIER = "policy-bounded-work-email"
POLICY_VERSION = "email-discovery-v1"

# Employee counts are a quickly changing operational fact.  The age boundary is
# part of POLICY_VERSION rather than an operator setting: changing it requires a
# policy-version bump so a stored execution remains reproducible.
EMPLOYEE_COUNT_EVIDENCE_TTL = timedelta(days=180)


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
    """Apply the policy's versioned age boundary to the current winner."""

    if evidence.evidence_id is None or evidence.raw_value is None:
        return EmployeeEvidenceFreshness.UNKNOWN
    effective_at = evidence.effective_at
    if effective_at is None:
        return EmployeeEvidenceFreshness.UNKNOWN
    if effective_at.tzinfo is None or now.tzinfo is None:
        return EmployeeEvidenceFreshness.UNKNOWN
    if evidence.source_marked_stale:
        return EmployeeEvidenceFreshness.STALE
    if now.astimezone(UTC) - effective_at.astimezone(UTC) > EMPLOYEE_COUNT_EVIDENCE_TTL:
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
) -> EmailDiscoveryPolicyDecision:
    """Return the exact candidate plan or one explicit policy refusal."""

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
    if freshness is EmployeeEvidenceFreshness.STALE:
        return EmailDiscoveryPolicyDecision(
            outcome=EmailPolicyOutcome.EMPLOYEE_COUNT_STALE,
            employee_count_class=count_class,
            evidence=employee_evidence,
            evidence_freshness=freshness,
            normalized_domain=normalized_domain,
            ordered_formats=empty_formats,
            candidates=empty_candidates,
            normalization_version=ENGINE_VERSION,
            reason="the sourced Company employee-count evidence is stale",
        )
    if (
        freshness is not EmployeeEvidenceFreshness.FRESH
        or count_class is EmployeeCountClass.UNKNOWN
    ):
        return EmailDiscoveryPolicyDecision(
            outcome=EmailPolicyOutcome.EMPLOYEE_COUNT_UNKNOWN,
            employee_count_class=EmployeeCountClass.UNKNOWN,
            evidence=employee_evidence,
            evidence_freshness=freshness,
            normalized_domain=normalized_domain,
            ordered_formats=empty_formats,
            candidates=empty_candidates,
            normalization_version=ENGINE_VERSION,
            reason="the sourced Company employee count does not settle the 50-employee boundary",
        )

    formats = _LARGE_FORMATS if count_class is EmployeeCountClass.MORE_THAN_50 else _SMALL_FORMATS
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
) -> EmailDiscoveryPolicyDecision:
    """Authorize reuse without inventing a candidate-policy branch.

    A fresh, accepted exact address does not need name components or candidate
    formats, but the Email execution still records a sourced, current Company
    classification. Unknown and stale employee evidence remain explicit blocks;
    neither is silently classified as a small Company.
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
    elif freshness is EmployeeEvidenceFreshness.STALE:
        outcome = EmailPolicyOutcome.EMPLOYEE_COUNT_STALE
        reason = "the sourced Company employee-count evidence is stale"
    elif (
        freshness is not EmployeeEvidenceFreshness.FRESH
        or count_class is EmployeeCountClass.UNKNOWN
    ):
        outcome = EmailPolicyOutcome.EMPLOYEE_COUNT_UNKNOWN
        count_class = EmployeeCountClass.UNKNOWN
        reason = "the sourced Company employee count does not settle the 50-employee boundary"
    else:
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
