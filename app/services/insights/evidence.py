"""Create and read traceable Company and Contact insights (INS-001).

The service accepts already-retrieved evidence; it does not browse, interpret a
research engine payload, qualify a Contact, or approve personalization. Its job
is narrower: validate the shared boundary and preserve claims separately from
the observations that support them.

Everything arriving here is untrusted external text. Source URLs, titles,
summaries and excerpts are stored as observations *about* a subject and are
never read as instructions, workflow commands, policy overrides or
configuration. The caller declares ``kind`` and ``state``; no wording inside the
evidence can set them, and :func:`is_personalization_eligible` decides from
stored columns alone, so nothing a page asserts about itself can promote it. How
this text is later placed into a model prompt belongs to AIC-002, not here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import InsightKind, InsightState, InsightSubject
from app.models.insight import Insight, InsightEvidence
from app.services.audit import record_audit_event

INSIGHT_ACTOR = "system:insight-evidence"

#: Every bounded string column this service writes, and the width it declares.
#: Enforced here so an over-long value is refused as an :class:`InsightError`
#: at the boundary instead of reaching the driver, where a string-truncation
#: error aborts the caller's whole transaction.
MAX_LENGTHS = {
    "source_url": 1024,
    "source_title": 1024,
    "extraction_method": 255,
    "source_record_type": 100,
    "idempotency_key": 255,
    "actor": 255,
    "insight_type": 64,
    "derivation_version": 64,
}

#: Kept as a name because callers and tests refer to the URL limit directly.
SOURCE_URL_MAX_LENGTH = MAX_LENGTHS["source_url"]


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


def _bounded(value: str, *, field: str) -> str:
    """Refuse a value the column cannot hold, naming the field that overflowed."""

    limit = MAX_LENGTHS.get(field)
    if limit is not None and len(value) > limit:
        raise InsightError(f"{field} must be at most {limit} characters")
    return value


def _required_text(value: str, *, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise InsightError(f"{field} must not be blank")
    return _bounded(cleaned, field=field)


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
    if item.source_title is not None:
        _bounded(item.source_title.strip(), field="source_title")


def _evidence_identity(item: EvidenceInput) -> tuple[str, int]:
    """What makes one observation *the same source* as another.

    The normalized URL plus the evidence version — the same pair the
    ``uq_insight_evidence_source_version`` constraint enforces in the database.
    Retrieval metadata is deliberately excluded: re-reading the same page later
    is the same source, not a new one.
    """

    return (_source_url(item.source_url), item.version)


def _validate_packet(items: tuple[EvidenceInput, ...]) -> tuple[tuple[str, int], ...]:
    """Validate every observation and return their identities.

    Runs to completion before anything is written, so a rejected packet leaves
    the caller's transaction untouched and still usable. Two observations
    citing the same source at the same version would otherwise reach the unique
    constraint and abort that transaction with a driver-level error.
    """

    identities: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for item in items:
        _validate_evidence(item)
        identity = _evidence_identity(item)
        if identity in seen:
            raise InsightError(
                f"evidence repeats source {identity[0]} at version {identity[1]} within one packet"
            )
        seen.add(identity)
        identities.append(identity)
    return tuple(identities)


def _validate_structured_payload(value: dict[str, object] | None) -> None:
    if value is None:
        return
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise InsightError("structured_payload must contain JSON values") from exc
    if len(encoded.encode("utf-8")) > 100_000:
        raise InsightError("structured_payload is too large (max 100000 bytes)")


def _content_hash(
    *,
    subject_id: uuid.UUID,
    claim: str,
    kind: InsightKind,
    state: InsightState,
    version: int,
    identities: tuple[tuple[str, int], ...],
    insight_type: str | None = None,
    structured_payload: dict[str, object] | None = None,
    producer_job_id: uuid.UUID | None = None,
    dossier_version_id: uuid.UUID | None = None,
    derivation_version: str | None = None,
) -> str:
    """Stable digest of *claim identity*, used to tell a retry from a collision.

    Deliberately excludes retrieval metadata — ``retrieved_at``, excerpts,
    confidence, extraction method, freshness. A retry that re-fetches its
    sources produces new timestamps while asserting the very same claim from the
    very same sources; that is precisely what an idempotency key exists to
    absorb. Only the claim itself and the identity of the sources behind it
    participate, and the source set is order-independent because the order
    evidence arrives in carries no meaning.

    A changed claim, subject, kind, state, version, or source set still yields a
    different digest and so still rejects reuse of the same key.
    """

    payload = {
        "subject_id": str(subject_id),
        "claim": claim,
        "kind": kind.value,
        "state": state.value,
        "version": version,
        "sources": sorted([url, str(evidence_version)] for url, evidence_version in identities),
        "insight_type": insight_type,
        "structured_payload": structured_payload,
        "producer_job_id": str(producer_job_id) if producer_job_id else None,
        "dossier_version_id": str(dossier_version_id) if dossier_version_id else None,
        "derivation_version": derivation_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _find_by_key(
    session: Session,
    *,
    company_id: uuid.UUID | None,
    contact_id: uuid.UUID | None,
    key: str,
) -> Insight | None:
    """The insight already stored under this key for this owner, if any."""

    owner_clause = (
        Insight.company_id == company_id
        if company_id is not None
        else Insight.contact_id == contact_id
    )
    return session.scalars(
        select(Insight).where(owner_clause, Insight.idempotency_key == key)
    ).first()


def _reuse_or_reject(existing: Insight, *, digest: str) -> Insight:
    """Return the original submission, or refuse to overwrite a different one."""

    if existing.content_hash != digest:
        raise InsightError("idempotency_key was already used for different content")
    return existing


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
    insight_type: str | None = None,
    structured_payload: dict[str, object] | None = None,
    producer_job_id: uuid.UUID | None = None,
    dossier_version_id: uuid.UUID | None = None,
    derivation_version: str | None = None,
) -> Insight:
    """Store one claim and its evidence as an append-only packet.

    Exactly one permanent subject is required. Supported and conflicting claims
    require traceable evidence; an explicit unknown may be stored without a
    source because recording a known gap is different from asserting a fact.

    The whole packet is validated before anything is written, so a rejected
    packet raises :class:`InsightError` and leaves the caller's transaction
    usable. Two writers submitting the same key at once resolve to one stored
    record, and the writer that loses the race is returned that record rather
    than a driver error.
    """

    if (company_id is None) == (contact_id is None):
        raise InsightError("exactly one of company_id or contact_id is required")
    cleaned_claim = _required_text(claim, field="claim")
    if version < 1:
        raise InsightError("insight version must be positive")
    items = tuple(evidence)
    if state is not InsightState.UNKNOWN and not items:
        raise InsightError("supported and conflicting insights require evidence")
    identities = _validate_packet(items)
    _validate_structured_payload(structured_payload)
    if (insight_type is None) != (structured_payload is None):
        raise InsightError("insight_type and structured_payload must be supplied together")
    clean_type = _required_text(insight_type, field="insight_type") if insight_type else None
    clean_derivation = (
        _required_text(derivation_version, field="derivation_version")
        if derivation_version
        else None
    )
    if clean_type is not None and (
        producer_job_id is None or dossier_version_id is None or clean_derivation is None
    ):
        raise InsightError(
            "structured insights require producer_job_id, dossier_version_id and derivation_version"
        )

    subject = InsightSubject.COMPANY if company_id is not None else InsightSubject.CONTACT
    subject_id = company_id if company_id is not None else contact_id
    if subject_id is None:  # pragma: no cover - guarded by the check above
        raise InsightError("exactly one of company_id or contact_id is required")
    digest = _content_hash(
        subject_id=subject_id,
        claim=cleaned_claim,
        kind=kind,
        state=state,
        version=version,
        identities=identities,
        insight_type=clean_type,
        structured_payload=structured_payload,
        producer_job_id=producer_job_id,
        dossier_version_id=dossier_version_id,
        derivation_version=clean_derivation,
    )
    cleaned_key = (
        _required_text(idempotency_key, field="idempotency_key")
        if idempotency_key is not None
        else None
    )
    _required_text(actor, field="actor")
    if cleaned_key is not None:
        existing = _find_by_key(
            session, company_id=company_id, contact_id=contact_id, key=cleaned_key
        )
        if existing is not None:
            return _reuse_or_reject(existing, digest=digest)

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
        insight_type=clean_type,
        structured_payload=structured_payload,
        producer_job_id=producer_job_id,
        dossier_version_id=dossier_version_id,
        derivation_version=clean_derivation,
    )
    try:
        # A SAVEPOINT, so losing the race below rolls back this INSERT alone and
        # leaves the caller's transaction intact.
        with session.begin_nested():
            session.add(insight)
            session.flush()
    except IntegrityError:
        if cleaned_key is None:
            raise
        # Another writer committed the same key between the lookup above and
        # this insert. The unique constraint is what makes the retry contract
        # true under concurrency; reaching it means the other writer won, so
        # honour its record exactly as the non-racing path would have. The
        # SAVEPOINT rollback has already detached the losing row.
        existing = _find_by_key(
            session, company_id=company_id, contact_id=contact_id, key=cleaned_key
        )
        if existing is None:  # pragma: no cover - the constraint implies a row
            raise
        return _reuse_or_reject(existing, digest=digest)

    for item, (normalized_url, _) in zip(items, identities, strict=True):
        session.add(
            InsightEvidence(
                insight_id=insight.id,
                source_url=normalized_url,
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

    Deterministic by construction: it reads stored columns and nothing else, so
    the same row always gives the same answer and no model judgement sits inside
    the persistence layer.
    """

    if insight.state is not InsightState.SUPPORTED:
        return False
    if insight.insight_type == "employee_size":
        payload = insight.structured_payload or {}
        if payload.get("status") != "supported" or payload.get("temporal_status") != "current":
            return False
        if insight.company_id is None:
            return False
        current = session.scalars(
            select(Insight)
            .where(
                Insight.company_id == insight.company_id,
                Insight.insight_type == "employee_size",
            )
            .order_by(Insight.created_at.desc(), Insight.id.desc())
        ).first()
        if current is None or current.id != insight.id:
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
