"""Automatic company-domain resolution for contact captures (DAT-017).

This is the layer that turns the capture-to-Contact path from "every capture
waits for an operator" into "every capture the evidence can settle resolves
itself, and only the genuinely uncertain ones wait".

It does four things, in this order:

1. **Gather evidence** the repository already holds — prior operator
   confirmations, operator-captured LinkedIn company pages, canonical companies,
   and provider candidates.
2. **Ask the policy** (:mod:`app.services.enrichment.domain_policy`), which is
   pure and versioned, what that evidence supports.
3. **Record the answer** on the enrichment record — decision, policy version,
   ordered reason codes, and the full evidence set — whether or not it resolved.
   A review case that records why it needs review is what the Review Queue
   (#172) will consume.
4. **Apply it** when it is safe: confirm the domain and promote the capture
   through the unchanged DAT-014 path, so suppression, identity ambiguity and
   idempotency keep working exactly as they did.

Deliberate limits
-----------------

*The provider is asked last, and only when the answer is not already known.* A
prior mapping or an identity-matched company page settles the question before
any call is made, which is the difference between an enrichment bill that scales
with new companies and one that scales with captures.

*An operator's decision is never overwritten.* The policy acts only on records
that are still ``UNCONFIRMED``; a ``MANUAL``, ``CANDIDATE`` or ``UNRESOLVED``
record is left exactly as the operator left it.

*Nothing here can promote something the existing rules would block.* Promotion
still runs through :func:`app.services.captures.promotion.promote`, so a
suppressed address, an ambiguous identity or a missing surname blocks an
automatic capture exactly as it blocks a manual one. Automation changes who
chooses the domain, not what is allowed to become a Contact.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.capture_promotion import ContactCapturePromotion
from app.models.company import Company
from app.models.enums import (
    DomainResolutionDecision,
    EnrichmentConfirmationSource,
    EnrichmentConfirmationStatus,
    EnrichmentLookupStatus,
)
from app.models.linkedin_company import LinkedInCompanySnapshot
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.services.audit import record_audit_event
from app.services.captures import promotion as promotion_service
from app.services.enrichment import companies as enrichment
from app.services.enrichment import domain_policy as policy
from app.services.enrichment import logodev
from app.services.imports import normalization as norm

__all__ = [
    "RESOLUTION_ACTOR",
    "RESOLUTION_AUDIT_ACTION",
    "ResolutionOutcome",
    "ReviewItem",
    "ResolutionMetrics",
    "gather_evidence",
    "resolve_capture",
    "resolve_and_promote",
    "pending_reviews",
    "metrics",
]

RESOLUTION_ACTOR = "domain-resolution"
RESOLUTION_AUDIT_ACTION = "capture.domain_resolved"
_ENTITY_TYPE = "salesnav_company_enrichment"

#: Policy conclusions that name a domain the system may apply on its own.
_APPLICABLE = frozenset(
    {
        DomainResolutionDecision.AUTO_CONFIRMED,
        DomainResolutionDecision.PRIOR_MAPPING_REUSED,
    }
)

#: Conclusions that belong in the review queue rather than in a retry.
_REVIEWABLE = frozenset(
    {
        DomainResolutionDecision.REVIEW_REQUIRED,
        DomainResolutionDecision.CONFLICT,
        DomainResolutionDecision.NO_CREDIBLE_CANDIDATE,
    }
)

#: How an applied policy decision maps onto the promotion-blocking view.
_SOURCE_FOR_DECISION = {
    DomainResolutionDecision.AUTO_CONFIRMED: EnrichmentConfirmationSource.AUTOMATIC_POLICY,
    DomainResolutionDecision.PRIOR_MAPPING_REUSED: EnrichmentConfirmationSource.PRIOR_MAPPING,
}


@dataclass(frozen=True)
class ResolutionOutcome:
    """What one resolution attempt concluded and did."""

    record: SalesNavCompanyEnrichment | None
    decision: policy.ResolutionDecision | None
    applied: bool
    provider_called: bool
    promotion: ContactCapturePromotion | None = None
    promotion_result: promotion_service.PromotionResult | None = None
    skipped_reason: str | None = None

    @property
    def resolved_domain(self) -> str | None:
        return self.record.confirmed_domain if self.record else None

    @property
    def promoted(self) -> bool:
        return bool(self.promotion_result and self.promotion_result.promoted)

    def summary(self) -> dict[str, Any]:
        return {
            "decision": self.decision.decision.value if self.decision else None,
            "domain": self.resolved_domain,
            "applied": self.applied,
            "provider_called": self.provider_called,
            "promoted": self.promoted,
            "skipped_reason": self.skipped_reason,
        }


# --- Evidence gathering -------------------------------------------------------


def _company_page_evidence(
    session: Session,
    *,
    company_key: str,
    company_name: str | None,
    linkedin_id: str | None,
    linkedin_url: str | None,
) -> list[policy.DomainEvidence]:
    """Domains an operator captured from a LinkedIn company page.

    This is the evidence DAT-017 exists to start using. It was already being
    stored by DAT-012G and never consulted when resolving a person's employer,
    which is why every capture needed a human even when the answer was sitting
    in the database.

    The join matters as much as the value. Matching on the LinkedIn company id
    or the normalized company URL is an *identity* match: the person capture and
    the company page name the same LinkedIn entity, and that is strong enough to
    stand alone. Matching on a normalized name is not — names collide, and
    "Apex" is several companies — so a name-only match is returned as
    corroborating evidence that cannot decide by itself.
    """

    if not (linkedin_id or linkedin_url or company_key):
        return []

    snapshots = session.scalars(
        select(LinkedInCompanySnapshot).where(LinkedInCompanySnapshot.website_domain.is_not(None))
    ).all()

    evidence: list[policy.DomainEvidence] = []
    for snapshot in snapshots:
        domain = norm.normalize_domain(snapshot.website_domain)
        if not domain:
            continue

        identity_matched = False
        how: str | None = None
        if (
            linkedin_id
            and snapshot.company_linkedin_id
            and snapshot.company_linkedin_id == linkedin_id
        ):
            identity_matched = True
            how = "linkedin_company_id"
        elif (
            linkedin_url
            and snapshot.normalized_company_url
            and snapshot.normalized_company_url == linkedin_url
        ):
            identity_matched = True
            how = "normalized_company_url"
        else:
            fields = snapshot.company_fields or {}
            page_name = fields.get("name") if isinstance(fields, dict) else None
            if company_key and enrichment.company_key(page_name) == company_key:
                how = "company_name"

        if how is None:
            continue

        notes: list[str] = []
        if policy.domain_label_agrees(company_name, domain):
            notes.append(policy.Reason.DOMAIN_LABEL_AGREEMENT)

        evidence.append(
            policy.DomainEvidence(
                domain=domain,
                axis=policy.EvidenceAxis.COMPANY_PAGE,
                identity_matched=identity_matched,
                source_ref=str(snapshot.id),
                notes=tuple(notes),
                detail={
                    "matched_on": how,
                    "captured_at": (
                        snapshot.captured_at.isoformat() if snapshot.captured_at else None
                    ),
                },
            )
        )
    return evidence


def _prior_mapping_evidence(
    session: Session,
    *,
    record: SalesNavCompanyEnrichment,
) -> list[policy.DomainEvidence]:
    """Domains an operator already confirmed for this same company."""

    domains = promotion_service.prior_confirmed_domains(
        session,
        company_key_value=record.company_key,
        company_linkedin_id=record.company_linkedin_id,
        exclude_record_id=record.id,
    )
    return [
        policy.DomainEvidence(
            domain=domain,
            axis=policy.EvidenceAxis.PRIOR_MAPPING,
            identity_matched=True,
            detail={"company_key": record.company_key},
        )
        for domain in sorted(domains)
    ]


def _canonical_company_evidence(
    session: Session,
    *,
    domains: Sequence[str],
    company_name: str | None,
) -> list[policy.DomainEvidence]:
    """Canonical companies that already carry one of the candidate domains.

    A ``companies`` row proves the domain is one this system already works with;
    it does not prove it belongs to *this* person's employer. So it corroborates
    only when the stored company name is the captured company name — an exact
    match after normalization, never a similarity score.
    """

    if not domains:
        return []
    rows = session.scalars(select(Company).where(Company.domain.in_(list(domains)))).all()
    key = enrichment.company_key(company_name)
    evidence: list[policy.DomainEvidence] = []
    for company in rows:
        if not company.domain:
            continue
        if not key or enrichment.company_key(company.name) != key:
            continue
        evidence.append(
            policy.DomainEvidence(
                domain=company.domain,
                axis=policy.EvidenceAxis.CANONICAL_COMPANY,
                identity_matched=True,
                source_ref=str(company.id),
                detail={"company_name": company.name},
            )
        )
    return evidence


def _provider_evidence(record: SalesNavCompanyEnrichment) -> list[policy.DomainEvidence]:
    """The provider's candidates, with rank recorded but never weighted.

    Rank is preserved in ``detail`` because it is part of what the provider
    said, and dropping it would make a stored decision harder to audit later.
    It is not read by the policy: ordering is not confidence, and the first
    result being first is not evidence about anything.
    """

    evidence: list[policy.DomainEvidence] = []
    for candidate in record.candidates or []:
        if not isinstance(candidate, dict):
            continue
        domain = norm.normalize_domain(candidate.get("domain"))
        if not domain:
            continue
        notes: list[str] = []
        if policy.brand_name_agrees(record.company_name, candidate.get("name")):
            notes.append(policy.Reason.BRAND_NAME_AGREEMENT)
        if policy.domain_label_agrees(record.company_name, domain):
            notes.append(policy.Reason.DOMAIN_LABEL_AGREEMENT)
        evidence.append(
            policy.DomainEvidence(
                domain=domain,
                axis=policy.EvidenceAxis.PROVIDER_CANDIDATE,
                identity_matched=False,
                notes=tuple(notes),
                detail={"provider_name": candidate.get("name"), "rank": candidate.get("rank")},
            )
        )
    return evidence


def gather_evidence(
    session: Session,
    *,
    record: SalesNavCompanyEnrichment,
    hints: promotion_service.CompanyHints,
) -> tuple[policy.DomainEvidence, ...]:
    """Everything the policy is allowed to consider, in one bundle.

    Nothing is inferred here. Every entry points at a stored record that an
    operator or an intake wrote, which is what lets a decision be re-explained
    from the evidence alone months later.
    """

    evidence: list[policy.DomainEvidence] = []
    evidence.extend(_prior_mapping_evidence(session, record=record))
    evidence.extend(
        _company_page_evidence(
            session,
            company_key=record.company_key,
            company_name=record.company_name,
            linkedin_id=record.company_linkedin_id,
            linkedin_url=hints.linkedin_url,
        )
    )
    provider = _provider_evidence(record)
    evidence.extend(provider)
    evidence.extend(
        _canonical_company_evidence(
            session,
            domains=[item.domain for item in evidence],
            company_name=record.company_name,
        )
    )
    return tuple(evidence)


# --- Resolution ---------------------------------------------------------------


def _needs_provider(evidence: Sequence[policy.DomainEvidence]) -> bool:
    """Whether asking the provider could still change the answer.

    It cannot when a prior mapping or an identity-matched company page already
    names the domain: those settle the question on their own, and a call would
    be spent confirming something already known.
    """

    for item in evidence:
        if item.axis == policy.EvidenceAxis.PRIOR_MAPPING:
            return False
        if item.axis == policy.EvidenceAxis.COMPANY_PAGE and item.identity_matched:
            return False
    return True


def _store_decision(
    session: Session,
    *,
    record: SalesNavCompanyEnrichment,
    decision: policy.ResolutionDecision,
    evidence: Sequence[policy.DomainEvidence],
) -> None:
    """Persist the conclusion and the evidence behind it.

    Written for every decision, including the ones that resolve nothing. A
    review case is only usable by a queue if it says why it is a review case.
    """

    record.resolution_policy_version = decision.policy_version
    record.resolution_decision = decision.decision
    record.resolution_reasons = list(decision.reasons)
    record.resolution_evidence = [item.as_dict() for item in evidence]
    record.resolution_recommendation = decision.recommendation
    record.resolved_at = datetime.now(UTC)
    session.flush()


def resolve_capture(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    api_key: str | None = None,
    search_url: str | None = None,
    timeout: float | None = None,
    max_candidates: int | None = None,
    actor: str = RESOLUTION_ACTOR,
    allow_provider_call: bool = True,
    transport: logodev.Transport | None = None,
) -> ResolutionOutcome:
    """Resolve one capture's company domain, automatically where it is safe.

    Idempotent by construction. A record an operator already decided is left
    alone; a record the policy already confirmed is not re-decided and costs no
    further provider call; re-running on an unresolved record re-evaluates
    against whatever evidence exists *now*, which is the behaviour that lets a
    later company-page capture unblock an earlier person capture.
    """

    # The flag is checked here rather than at each call site so there is exactly
    # one place automation can be switched off. While off nothing is written at
    # all: the DAT-014 behaviour is not "the same path with a smaller policy",
    # it is the path untouched.
    if not get_settings().features.automatic_domain_resolution:
        return ResolutionOutcome(
            record=None,
            decision=None,
            applied=False,
            provider_called=False,
            skipped_reason="automatic domain resolution is disabled",
        )

    promotion, record = promotion_service.ensure_records(session, snapshot)
    hints = promotion_service.company_hints(snapshot)

    if record is None:
        # No company on the page — there is nothing to resolve, and the existing
        # evaluation already states that truthfully.
        promotion_service.evaluate_company(
            session, promotion=promotion, record=None, hints=hints, actor=actor
        )
        return ResolutionOutcome(
            record=None,
            decision=None,
            applied=False,
            provider_called=False,
            promotion=promotion,
            skipped_reason="the captured page showed no company name",
        )

    # An operator's decision is authoritative and is never revisited.
    if record.confirmation_status is not EnrichmentConfirmationStatus.UNCONFIRMED:
        already_automatic = record.confirmation_source in (
            EnrichmentConfirmationSource.AUTOMATIC_POLICY,
            EnrichmentConfirmationSource.PRIOR_MAPPING,
        )
        promotion_service.evaluate_company(
            session, promotion=promotion, record=record, hints=hints, actor=actor
        )
        return ResolutionOutcome(
            record=record,
            decision=None,
            applied=False,
            provider_called=False,
            promotion=promotion,
            skipped_reason=(
                "already resolved automatically"
                if already_automatic
                else "already decided by an operator"
            ),
        )

    evidence = gather_evidence(session, record=record, hints=hints)
    provider_called = False

    # Ask the provider only when it could still change the answer, and only when
    # it has not already been asked for this company.
    if (
        allow_provider_call
        and api_key
        and search_url
        and _needs_provider(evidence)
        and record.lookup_status is EnrichmentLookupStatus.NOT_STARTED
    ):
        enrichment.run_lookup(
            session,
            record=record,
            api_key=api_key,
            search_url=search_url,
            timeout=timeout if timeout is not None else 10.0,
            max_candidates=max_candidates if max_candidates is not None else 10,
            actor=actor,
            transport=transport,
        )
        provider_called = True
        evidence = gather_evidence(session, record=record, hints=hints)

    decision = policy.decide(
        policy.ResolutionInput(
            company_key=record.company_key,
            company_name=record.company_name,
            company_linkedin_id=record.company_linkedin_id,
            lookup_status=record.lookup_status,
            evidence=evidence,
        )
    )
    _store_decision(session, record=record, decision=decision, evidence=evidence)

    applied = False
    if decision.decision in _APPLICABLE and decision.domain:
        enrichment.confirm_record(
            session,
            record=record,
            source=_SOURCE_FOR_DECISION[decision.decision],
            domain=decision.domain,
            actor=actor,
            note=(
                "resolved automatically by "
                f"{decision.policy_version} ({', '.join(decision.reasons)})"
            ),
        )
        applied = True

    promotion_service.evaluate_company(
        session, promotion=promotion, record=record, hints=hints, actor=actor
    )

    record_audit_event(
        session,
        actor=actor,
        action=RESOLUTION_AUDIT_ACTION,
        entity_type=_ENTITY_TYPE,
        entity_id=str(record.id),
        new_state=decision.decision.value,
        reason="automatic company-domain resolution",
        context={
            "capture_id": str(snapshot.id),
            "company_key": record.company_key,
            "policy_version": decision.policy_version,
            "reasons": list(decision.reasons),
            "domain": decision.domain,
            "recommendation": decision.recommendation,
            "applied": applied,
            "provider_called": provider_called,
            "evidence_count": len(evidence),
        },
    )

    return ResolutionOutcome(
        record=record,
        decision=decision,
        applied=applied,
        provider_called=provider_called,
        promotion=promotion,
    )


def resolve_and_promote(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    api_key: str | None = None,
    search_url: str | None = None,
    timeout: float | None = None,
    max_candidates: int | None = None,
    actor: str = RESOLUTION_ACTOR,
    allow_provider_call: bool = True,
    transport: logodev.Transport | None = None,
) -> ResolutionOutcome:
    """Resolve the domain and, when that succeeded, promote the capture.

    The promotion is the unchanged DAT-014 call. That is the point: automation
    decides the domain and nothing else. Suppression, identity ambiguity, a
    missing surname and the already-promoted short-circuit all behave exactly as
    they do for a capture an operator resolved by hand, and a blocked capture
    keeps its truthful blocking reason.
    """

    outcome = resolve_capture(
        session,
        snapshot=snapshot,
        api_key=api_key,
        search_url=search_url,
        timeout=timeout,
        max_candidates=max_candidates,
        actor=actor,
        allow_provider_call=allow_provider_call,
        transport=transport,
    )

    record = outcome.record
    if record is None or record.confirmation_status is not EnrichmentConfirmationStatus.CONFIRMED:
        return outcome

    result = promotion_service.promote(session, snapshot=snapshot, actor=actor)
    return ResolutionOutcome(
        record=record,
        decision=outcome.decision,
        applied=outcome.applied,
        provider_called=outcome.provider_called,
        promotion=result.promotion,
        promotion_result=result,
        skipped_reason=outcome.skipped_reason,
    )


# --- Review boundary (the seam APP-008 / #172 will consume) -------------------


@dataclass(frozen=True)
class ReviewItem:
    """One unresolved company, described the way a review queue needs it.

    Deliberately a projection over the existing enrichment record rather than a
    new table. The record already is the company-review queue — it has the
    subject, the evidence, the decision history and an idempotent one-row-per-
    capture shape. A second queue would mean two places to look for the same
    unresolved company and two places for them to disagree.

    #172 can consume this without DAT-017 being reworked: it carries subject
    type and id, the blocked action, reason codes, the evidence, a
    recommendation, and whether that recommendation is reusable.
    """

    subject_type: str
    subject_id: uuid.UUID
    capture_id: uuid.UUID | None
    company_key: str
    company_name: str
    blocked_action: str
    decision: DomainResolutionDecision
    reason_codes: tuple[str, ...]
    evidence: tuple[dict[str, Any], ...]
    recommendation: str | None
    policy_version: str | None
    created_at: datetime
    resolved_at: datetime | None
    #: A resolution of this item may be replayed for other captures of the same
    #: company, which is what makes one operator decision worth more than one
    #: record.
    reusable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_type": self.subject_type,
            "subject_id": str(self.subject_id),
            "capture_id": str(self.capture_id) if self.capture_id else None,
            "company_key": self.company_key,
            "company_name": self.company_name,
            "blocked_action": self.blocked_action,
            "decision": self.decision.value,
            "reason_codes": list(self.reason_codes),
            "evidence": [dict(item) for item in self.evidence],
            "recommendation": self.recommendation,
            "policy_version": self.policy_version,
            "created_at": self.created_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "reusable": self.reusable,
        }


def pending_reviews(session: Session, *, limit: int = 200) -> list[ReviewItem]:
    """Companies the policy could not settle, oldest first.

    Only genuinely unresolved records appear. A record that resolved
    automatically, that an operator has decided, or that is merely waiting for a
    retryable provider is not an exception for a human to handle.
    """

    rows = session.scalars(
        select(SalesNavCompanyEnrichment)
        .where(
            SalesNavCompanyEnrichment.confirmation_status
            == EnrichmentConfirmationStatus.UNCONFIRMED,
            SalesNavCompanyEnrichment.resolution_decision.in_(sorted(_REVIEWABLE)),
        )
        .order_by(SalesNavCompanyEnrichment.created_at)
        .limit(limit)
    ).all()

    items: list[ReviewItem] = []
    for record in rows:
        assert record.resolution_decision is not None  # narrowed by the WHERE clause
        items.append(
            ReviewItem(
                subject_type="company_domain",
                subject_id=record.id,
                capture_id=record.capture_id,
                company_key=record.company_key,
                company_name=record.company_name,
                blocked_action="contact_promotion",
                decision=record.resolution_decision,
                reason_codes=tuple(record.resolution_reasons or ()),
                evidence=tuple(record.resolution_evidence or ()),
                recommendation=record.resolution_recommendation,
                policy_version=record.resolution_policy_version,
                created_at=record.created_at,
                resolved_at=record.resolved_at,
            )
        )
    return items


# --- Metrics ------------------------------------------------------------------


@dataclass(frozen=True)
class ResolutionMetrics:
    """Enough to answer "is this actually reducing operator load, and safely?"."""

    decided: int = 0
    by_decision: dict[str, int] = field(default_factory=dict)
    automatic: int = 0
    review: int = 0
    corrections: int = 0
    provider_calls: int = 0
    records_with_provider_call: int = 0

    @property
    def automatic_rate(self) -> float:
        """Share of decided companies that needed no operator."""

        return self.automatic / self.decided if self.decided else 0.0

    @property
    def review_rate(self) -> float:
        return self.review / self.decided if self.decided else 0.0

    @property
    def correction_rate(self) -> float:
        """Share of automatic decisions an operator later overrode.

        The number that matters most: a high automatic rate is only good news
        while this stays near zero. Measured against automatic decisions, not
        against all decisions, so it cannot be diluted by review volume.
        """

        return self.corrections / self.automatic if self.automatic else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "decided": self.decided,
            "by_decision": dict(self.by_decision),
            "automatic": self.automatic,
            "review": self.review,
            "corrections": self.corrections,
            "provider_calls": self.provider_calls,
            "records_with_provider_call": self.records_with_provider_call,
            "automatic_rate": round(self.automatic_rate, 4),
            "review_rate": round(self.review_rate, 4),
            "correction_rate": round(self.correction_rate, 4),
        }


def metrics(session: Session, *, since: datetime | None = None) -> ResolutionMetrics:
    """Automatic-resolution, review, correction and provider-call figures."""

    def _scoped(stmt: Any) -> Any:
        if since is not None:
            return stmt.where(SalesNavCompanyEnrichment.resolved_at >= since)
        return stmt

    counts = session.execute(
        _scoped(
            select(
                SalesNavCompanyEnrichment.resolution_decision,
                func.count(),
            ).where(SalesNavCompanyEnrichment.resolution_decision.is_not(None))
        ).group_by(SalesNavCompanyEnrichment.resolution_decision)
    ).all()

    by_decision: dict[str, int] = {}
    automatic = 0
    review = 0
    decided = 0
    for decision, count in counts:
        by_decision[decision.value] = count
        decided += count
        if decision in _APPLICABLE:
            automatic += count
        elif decision in _REVIEWABLE:
            review += count

    corrections = (
        session.execute(
            _scoped(select(func.count()).select_from(SalesNavCompanyEnrichment)).where(
                SalesNavCompanyEnrichment.resolution_corrected_at.is_not(None)
            )
        ).scalar_one()
        or 0
    )

    # Provider cost is measured from the lookup ledger already on the record:
    # total attempts, and how many companies cost at least one call. The gap
    # between them is the retry overhead.
    provider_calls = (
        session.execute(
            _scoped(select(func.coalesce(func.sum(SalesNavCompanyEnrichment.lookup_attempts), 0)))
        ).scalar_one()
        or 0
    )
    records_with_call = (
        session.execute(
            _scoped(select(func.count()).select_from(SalesNavCompanyEnrichment)).where(
                SalesNavCompanyEnrichment.lookup_attempts > 0
            )
        ).scalar_one()
        or 0
    )

    return ResolutionMetrics(
        decided=decided,
        by_decision=by_decision,
        automatic=automatic,
        review=review,
        corrections=int(corrections),
        provider_calls=int(provider_calls),
        records_with_provider_call=int(records_with_call),
    )
