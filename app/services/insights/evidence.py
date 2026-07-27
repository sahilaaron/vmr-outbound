"""Create and read traceable Company and Contact insights (INS-001).

The service accepts already-retrieved evidence; it does not browse, interpret a
research engine payload, qualify a Contact, or approve personalization. Its job
is narrower: validate the shared boundary and preserve claims separately from
the observations that support them.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import InsightKind, InsightState, InsightSubject
from app.models.insight import Insight, InsightEvidence
from app.services.audit import record_audit_event

INSIGHT_ACTOR = "system:insight-evidence"


class InsightError(ValueError):
    """A claim or evidence packet cannot be stored safely."""


@dataclass(frozen=True)
class EvidenceInput:
    """One source observation supplied at the service boundary."""

    source_url: str
    retrieved_at: datetime
    evidence_summary: str
    confidence: float
    extraction_method: str
    source_title: str | None = None
    published_at: datetime | None = None
    excerpt: str | None = None
    freshness_at: datetime | None = None
    source_record_type: str | None = None
    source_record_id: uuid.UUID | None = None
    version: int = 1


def _required_text(value: str, *, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise InsightError(f"{field} must not be blank")
    return cleaned


def _aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InsightError(f"{field} must include a timezone")
    return value


def _source_url(value: str) -> str:
    cleaned = _required_text(value, field="source_url")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise InsightError("source_url must be an absolute http or https URL")
    return cleaned


def _validate_evidence(item: EvidenceInput) -> None:
    _source_url(item.source_url)
    _aware(item.retrieved_at, field="retrieved_at")
    _required_text(item.evidence_summary, field="evidence_summary")
    _required_text(item.extraction_method, field="extraction_method")
    if not 0 <= item.confidence <= 1:
        raise InsightError("confidence must be between 0 and 1")
    if item.version < 1:
        raise InsightError("evidence version must be positive")
    if item.published_at is not None:
        _aware(item.published_at, field="published_at")
    if item.freshness_at is not None:
        _aware(item.freshness_at, field="freshness_at")
    if (item.source_record_type is None) != (item.source_record_id is None):
        raise InsightError("source_record_type and source_record_id must be supplied together")
    if item.source_record_type is not None:
        _required_text(item.source_record_type, field="source_record_type")


def _content_hash(
    *,
    subject_id: uuid.UUID,
    claim: str,
    kind: InsightKind,
    state: InsightState,
    version: int,
    evidence: tuple[EvidenceInput, ...],
) -> str:
    """Stable digest used to distinguish a safe retry from a key collision."""

    payload = {
        "subject_id": str(subject_id),
        "claim": claim,
        "kind": kind.value,
        "state": state.value,
        "version": version,
        "evidence": [
            {
                "source_url": item.source_url.strip(),
                "source_title": item.source_title,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "retrieved_at": item.retrieved_at.isoformat(),
                "excerpt": item.excerpt,
                "evidence_summary": item.evidence_summary.strip(),
                "confidence": item.confidence,
                "extraction_method": item.extraction_method.strip(),
                "freshness_at": item.freshness_at.isoformat() if item.freshness_at else None,
                "source_record_type": item.source_record_type,
                "source_record_id": (
                    str(item.source_record_id) if item.source_record_id is not None else None
                ),
                "version": item.version,
            }
            for item in evidence
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def create_insight(
    session: Session,
    *,
    claim: str,
    kind: InsightKind,
    state: InsightState,
    evidence: tuple[EvidenceInput, ...] | list[EvidenceInput],
    company_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    version: int = 1,
    idempotency_key: str | None = None,
    actor: str = INSIGHT_ACTOR,
) -> Insight:
    """Store one claim and its evidence as an append-only packet.

    Exactly one permanent subject is required. Supported and conflicting claims
    require traceable evidence; an explicit unknown may be stored without a
    source because recording a known gap is different from asserting a fact.
    """

    if (company_id is None) == (contact_id is None):
        raise InsightError("exactly one of company_id or contact_id is required")
    cleaned_claim = _required_text(claim, field="claim")
    if version < 1:
        raise InsightError("insight version must be positive")
    items = tuple(evidence)
    if state is not InsightState.UNKNOWN and not items:
        raise InsightError("supported and conflicting insights require evidence")
    for item in items:
        _validate_evidence(item)

    subject = InsightSubject.COMPANY if company_id is not None else InsightSubject.CONTACT
    subject_id = company_id if company_id is not None else contact_id
    assert subject_id is not None
    digest = _content_hash(
        subject_id=subject_id,
        claim=cleaned_claim,
        kind=kind,
        state=state,
        version=version,
        evidence=items,
    )
    cleaned_key = (
        _required_text(idempotency_key, field="idempotency_key")
        if idempotency_key is not None
        else None
    )
    if cleaned_key is not None:
        owner_clause = (
            Insight.company_id == company_id
            if company_id is not None
            else Insight.contact_id == contact_id
        )
        existing = session.scalars(
            select(Insight).where(owner_clause, Insight.idempotency_key == cleaned_key)
        ).first()
        if existing is not None:
            if existing.content_hash != digest:
                raise InsightError("idempotency_key was already used for different content")
            return existing

    insight = Insight(
        subject=subject,
        company_id=company_id,
        contact_id=contact_id,
        claim=cleaned_claim,
        kind=kind,
        state=state,
        version=version,
        created_by=actor,
        idempotency_key=cleaned_key,
        content_hash=digest,
    )
    session.add(insight)
    session.flush()

    for item in items:
        session.add(
            InsightEvidence(
                insight_id=insight.id,
                source_url=_source_url(item.source_url),
                source_title=item.source_title.strip() if item.source_title else None,
                published_at=item.published_at,
                retrieved_at=item.retrieved_at,
                excerpt=item.excerpt,
                evidence_summary=item.evidence_summary.strip(),
                confidence=item.confidence,
                extraction_method=item.extraction_method.strip(),
                freshness_at=item.freshness_at,
                source_record_type=(
                    item.source_record_type.strip() if item.source_record_type else None
                ),
                source_record_id=item.source_record_id,
                version=item.version,
            )
        )
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="insight.created",
        entity_type=subject.value,
        entity_id=str(subject_id),
        reason="versioned insight and evidence stored",
        context={
            "insight_id": str(insight.id),
            "kind": kind.value,
            "state": state.value,
            "version": version,
            "evidence_count": len(items),
        },
    )
    return insight


def is_personalization_eligible(session: Session, *, insight: Insight) -> bool:
    """Whether an insight has the minimum traceability for later approval.

    This is not campaign eligibility and not an approval. It only prevents a
    source-less, conflicted, or explicitly unknown claim from being presented
    downstream as approved personalization evidence.
    """

    if insight.state is not InsightState.SUPPORTED:
        return False
    evidence = list(
        session.scalars(select(InsightEvidence).where(InsightEvidence.insight_id == insight.id))
    )
    return bool(evidence) and all(
        item.source_url
        and item.retrieved_at is not None
        and bool(item.evidence_summary and item.evidence_summary.strip())
        and item.confidence is not None
        and bool(item.extraction_method and item.extraction_method.strip())
        for item in evidence
    )


def list_for_company(session: Session, *, company_id: uuid.UUID) -> list[Insight]:
    """All reusable insights for one permanent Company, newest first."""

    return list(
        session.scalars(
            select(Insight)
            .where(Insight.company_id == company_id)
            .order_by(Insight.created_at.desc(), Insight.id.desc())
        )
    )


def list_for_contact(session: Session, *, contact_id: uuid.UUID) -> list[Insight]:
    """All reusable insights for one permanent Contact, newest first."""

    return list(
        session.scalars(
            select(Insight)
            .where(Insight.contact_id == contact_id)
            .order_by(Insight.created_at.desc(), Insight.id.desc())
        )
    )
