"""Turn one structured answer into one stored Company Intelligence version (CI-001).

Everything in this module except the answer itself is deterministic. The same
input and the same answer produce byte-identical rows, in the same order, with
the same ranks — which is what makes a stored classification something you can
re-derive and check rather than something you have to trust.

The validation policy, in the order it runs, because the order is the argument:

1. **Shape.** An answer that is not a JSON object, or whose ``classifications``
   is not a list, is malformed. Nothing is persisted and the caller is told it
   may retry. A half-understood answer is never salvaged: partial parsing is how
   a model's formatting accident becomes a company's industry.
2. **Dimension.** Unknown dimension names are dropped and counted. The dimension
   set is closed, and quietly accepting a twelfth would create a category no
   screen renders and no reader knows about.
3. **Citation.** Each cited handle must resolve to a fact this run actually
   showed the producer. Handles that do not resolve are dropped and counted.
4. **Evidence.** A value with no surviving citation is **not discarded** — it is
   stored ``UNRESOLVED`` with ``evidence_status = INSUFFICIENT`` and the reason
   ``no_evidence``. Dropping it would hide the producer's suggestion from the
   person whose job is to judge it; storing it as a fact would be a lie. Keeping
   it, visibly unsupported, is the only option that is both honest and useful.
5. **Normalization.** The value is resolved against the active vocabulary. A
   value that does not map stays ``UNRESOLVED`` with reason ``unmapped_value``
   and keeps the producer's exact wording, which is what an operator needs in
   order to add the alias that fixes it.
6. **Conflict.** Values named in a conflict become ``CONFLICTED`` and share a
   group. Nothing picks a winner. A conflict naming fewer than two values that
   actually survived is dropped, because a "conflict" with one side is a claim.
7. **Rank.** Dense, deterministic, per dimension, in the order the answer gave
   them after filtering. ``is_primary`` applies only to industry, only at rank 0.

Two things this module will not do, whatever the answer says: write a canonical
Company field, and touch anything in Research. Both are checked by tests rather
than left to reading.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_intelligence import (
    CompanyIntelligenceClassification,
    CompanyIntelligenceConflict,
    CompanyIntelligenceEvidenceLink,
    CompanyIntelligenceVersion,
)
from app.models.enums import (
    IntelligenceConfidenceBand,
    IntelligenceDimension,
    IntelligenceEvidenceStatus,
    IntelligenceEvidenceSupport,
    IntelligenceNormalization,
    IntelligenceValueState,
)
from app.services.audit import record_audit_event
from app.services.company_intelligence import taxonomy as taxonomy_service
from app.services.company_intelligence.inputs import IntelligenceInput
from app.services.company_intelligence.normalization import normalize_term

PRODUCER_ACTOR = "system:company-intelligence"

#: The deterministic half of production, versioned separately from whatever
#: produced the answer. Changing a validation, a cap or the ranking rule is a
#: policy bump, and a policy bump changes the input digest, which is what makes
#: the change produce a new version instead of silently reinterpreting an old one.
POLICY_VERSION = "1"

#: How many values are kept per dimension. Caps are not tidiness: an unbounded
#: list turns "we found eleven products" into a wall nobody reviews, and review
#: is the whole point. Values beyond the cap are dropped and counted, never
#: silently truncated.
DIMENSION_CAPS: dict[IntelligenceDimension, int] = {
    IntelligenceDimension.INDUSTRY: 4,
    IntelligenceDimension.SUBINDUSTRY: 6,
    IntelligenceDimension.PRODUCT: 10,
    IntelligenceDimension.SERVICE: 10,
    IntelligenceDimension.SPECIALTY: 8,
    IntelligenceDimension.CAPABILITY: 8,
    IntelligenceDimension.GEOGRAPHY: 10,
    IntelligenceDimension.OPERATING_MARKET: 8,
    IntelligenceDimension.CUSTOMER_SEGMENT: 8,
    IntelligenceDimension.BUSINESS_MODEL: 3,
    IntelligenceDimension.COMPANY_TYPE: 3,
}

MAX_VALUE_CHARS = 500
MAX_RATIONALE_CHARS = 600
MAX_STATEMENT_CHARS = 1000
MAX_EXCERPT_CHARS = 1000

#: Confidence bands. Deliberately coarse: 0.62 and 0.58 are not different
#: judgements, and a screen that shows them side by side implies they are.
_BAND_HIGH = 0.75
_BAND_MEDIUM = 0.45

REASON_NO_EVIDENCE = "no_evidence"
REASON_UNMAPPED = "unmapped_value"
REASON_CONFLICT = "conflicting_evidence"
REASON_SILENT = "evidence_silent"


class IntelligenceProducerError(Exception):
    """A production attempt that did not yield a storable version."""

    retryable = False
    code = "intelligence_failed"

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class IntelligenceMalformed(IntelligenceProducerError):
    """The answer was not the JSON object the contract asks for.

    Retryable on purpose, and for the same reason the thinking seam treats a
    malformed answer as retryable: one bad response is usually a one-off, and
    failing the Company terminally costs an operator more than one repeat call.
    """

    retryable = True
    code = "intelligence_malformed"


@dataclass(frozen=True)
class ProductionResult:
    """What one production attempt stored."""

    version: CompanyIntelligenceVersion
    created: bool
    classifications: int
    supported: int
    unresolved: int
    conflicts: int
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reused(self) -> bool:
        """True when the identical question had already been answered."""

        return not self.created


@dataclass
class _Candidate:
    """One surviving classification, before it becomes a row."""

    dimension: IntelligenceDimension
    value: str
    normalized_value: str
    is_primary: bool
    confidence: float | None
    rationale: str | None
    evidence: list[tuple[uuid.UUID | None, uuid.UUID | None, str | None, str | None]]
    resolution: taxonomy_service.TermResolution
    conflict_group: int | None = None


def confidence_band(value: float | None) -> IntelligenceConfidenceBand | None:
    """Map a numeric confidence onto a band. Deterministic, boundaries included."""

    if value is None:
        return None
    if value >= _BAND_HIGH:
        return IntelligenceConfidenceBand.HIGH
    if value >= _BAND_MEDIUM:
        return IntelligenceConfidenceBand.MEDIUM
    return IntelligenceConfidenceBand.LOW


def existing_version(
    session: Session, *, company_id: uuid.UUID, input_digest: str
) -> CompanyIntelligenceVersion | None:
    """The version already produced for this exact question, if any."""

    return session.scalars(
        select(CompanyIntelligenceVersion).where(
            CompanyIntelligenceVersion.company_id == company_id,
            CompanyIntelligenceVersion.input_digest == input_digest,
        )
    ).first()


def vocabulary_for_prompt(session: Session, *, limit_per_dimension: int = 200) -> dict[str, Any]:
    """The canonical values the producer is shown, per dimension.

    Only the *category* level of the industry hierarchy is listed in full;
    listing 245 subindustries would crowd out the evidence in the prompt, and the
    producer is explicitly allowed to write its own subindustry wording, which
    normalization then either maps or flags. That trade is documented in
    ``docs/COMPANY_INTELLIGENCE_TAXONOMY.md`` because it is a real limitation,
    not an oversight.
    """

    vocabularies: dict[str, Any] = {}
    for dimension in IntelligenceDimension:
        edition = taxonomy_service.active_taxonomy(session, dimension=dimension)
        if edition is None:
            continue
        depth = 0 if dimension is IntelligenceDimension.INDUSTRY else None
        if dimension is IntelligenceDimension.SUBINDUSTRY:
            continue
        terms = taxonomy_service.list_terms(session, taxonomy=edition, depth=depth)
        if not terms:
            continue
        vocabularies[dimension.value] = [
            term.canonical_label for term in terms[:limit_per_dimension]
        ]
    return vocabularies


def produce(
    session: Session,
    *,
    company: Company,
    source: IntelligenceInput,
    answer: dict[str, Any],
    raw_answer: str = "",
    job_id: uuid.UUID | None = None,
    created_by: str | None = None,
    make_current: bool = True,
    actor: str = PRODUCER_ACTOR,
) -> ProductionResult:
    """Validate one structured answer and persist it as one immutable version."""

    if company.id != source.company_id:
        raise IntelligenceProducerError(
            "the assembled input describes a different company than the one being written"
        )

    reused = existing_version(session, company_id=company.id, input_digest=source.digest)
    if reused is not None:
        # The identical question was already answered under the identical
        # producer and vocabulary. Answering it twice would spend a model call to
        # reach a version we already have, and would make "one version per input"
        # a convention rather than a guarantee.
        return ProductionResult(
            version=reused,
            created=False,
            classifications=reused.classification_count,
            supported=reused.supported_count,
            unresolved=reused.unresolved_count,
            conflicts=reused.conflict_count,
            warnings=("reused an existing version for an unchanged input",),
        )

    warnings: list[str] = []
    candidates, unknown_dimensions = _validate(answer, source=source, warnings=warnings)
    conflicts = _apply_conflicts(answer, candidates=candidates, warnings=warnings)
    candidates = _apply_caps(candidates, warnings=warnings)

    for candidate in candidates:
        candidate.resolution = taxonomy_service.resolve(
            session, dimension=candidate.dimension, value=candidate.value
        )

    version = _persist(
        session,
        company=company,
        source=source,
        candidates=candidates,
        conflicts=conflicts,
        unknown_dimensions=unknown_dimensions,
        raw_answer=raw_answer,
        job_id=job_id,
        created_by=created_by,
        warnings=warnings,
    )

    if make_current:
        select_current(session, company=company, version=version, actor=actor)

    record_audit_event(
        session,
        actor=actor,
        action="company_intelligence.version_produced",
        entity_type="company",
        entity_id=str(company.id),
        new_state=str(version.version_number),
        reason=f"company intelligence produced by {source.producer}/{source.producer_version}",
        context={
            "intelligence_version_id": str(version.id),
            "dossier_version": source.dossier_version_number,
            "input_digest": source.digest,
            "classifications": version.classification_count,
            "unresolved": version.unresolved_count,
            "conflicts": version.conflict_count,
        },
    )
    return ProductionResult(
        version=version,
        created=True,
        classifications=version.classification_count,
        supported=version.supported_count,
        unresolved=version.unresolved_count,
        conflicts=version.conflict_count,
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _validate(
    answer: Any,
    *,
    source: IntelligenceInput,
    warnings: list[str],
) -> tuple[list[_Candidate], tuple[IntelligenceDimension, ...]]:
    if not isinstance(answer, dict):
        raise IntelligenceMalformed("the answer was not a JSON object")
    raw = answer.get("classifications", [])
    if not isinstance(raw, list):
        raise IntelligenceMalformed("`classifications` was present but was not a list")

    known = {dimension.value: dimension for dimension in IntelligenceDimension}
    candidates: list[_Candidate] = []
    seen: set[tuple[IntelligenceDimension, str]] = set()

    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"classification {index} was not an object and was dropped")
            continue
        dimension = known.get(str(item.get("dimension", "")).strip().lower())
        if dimension is None:
            warnings.append(
                f"classification {index} named unknown dimension "
                f"{str(item.get('dimension'))[:40]!r} and was dropped"
            )
            continue
        value = _text(item.get("value"), limit=MAX_VALUE_CHARS)
        if value is None:
            warnings.append(f"classification {index} had no value and was dropped")
            continue
        normalized = normalize_term(value)
        if (dimension, normalized) in seen:
            # The same value twice on one dimension is one value. Storing both
            # would double-count it everywhere it is later summarised.
            warnings.append(f"duplicate value {value[:60]!r} on {dimension.value} was dropped")
            continue
        seen.add((dimension, normalized))

        evidence = _evidence(item.get("evidence"), source=source, warnings=warnings, index=index)
        candidates.append(
            _Candidate(
                dimension=dimension,
                value=value,
                normalized_value=normalized,
                is_primary=bool(item.get("is_primary"))
                and dimension is IntelligenceDimension.INDUSTRY,
                confidence=_confidence(item.get("confidence")),
                rationale=_text(item.get("rationale"), limit=MAX_RATIONALE_CHARS),
                evidence=evidence,
                resolution=taxonomy_service.TermResolution(
                    normalization=IntelligenceNormalization.UNMAPPED
                ),
            )
        )

    unknown = _unknown_dimensions(answer.get("unknown_dimensions"), known=known, warnings=warnings)
    # A dimension cannot be both classified and unknown. When the answer says
    # both, the classification wins and the contradiction is recorded, because a
    # value with evidence is a stronger statement than an absence of one.
    classified = {candidate.dimension for candidate in candidates}
    contradictory = tuple(dimension for dimension in unknown if dimension in classified)
    if contradictory:
        warnings.append(
            "answer called "
            + ", ".join(dimension.value for dimension in contradictory)
            + " both classified and unknown; kept the classified values"
        )
    return candidates, tuple(d for d in unknown if d not in classified)


def _unknown_dimensions(
    raw: Any,
    *,
    known: dict[str, IntelligenceDimension],
    warnings: list[str],
) -> tuple[IntelligenceDimension, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        warnings.append("`unknown_dimensions` was not a list and was ignored")
        return ()
    out: list[IntelligenceDimension] = []
    for entry in raw:
        dimension = known.get(str(entry).strip().lower())
        if dimension is None:
            warnings.append(
                f"unknown_dimensions named {str(entry)[:40]!r}, which is not a dimension"
            )
            continue
        if dimension not in out:
            out.append(dimension)
    return tuple(out)


def _evidence(
    raw: Any,
    *,
    source: IntelligenceInput,
    warnings: list[str],
    index: int,
) -> list[tuple[uuid.UUID | None, uuid.UUID | None, str | None, str | None]]:
    """Resolve cited handles into persisted evidence references.

    A handle that does not resolve is dropped, never softened into a URL the
    producer might have meant. The returned tuples are
    ``(insight_id, insight_evidence_id, source_url, excerpt)``.
    """

    if raw is None:
        return []
    entries: Iterable[Any] = raw if isinstance(raw, list) else [raw]
    resolved: list[tuple[uuid.UUID | None, uuid.UUID | None, str | None, str | None]] = []
    seen: set[tuple[uuid.UUID | None, str | None]] = set()
    for entry in entries:
        handle = str(entry).strip()
        if not handle:
            continue
        fact = source.fact_by_ref(handle)
        if fact is not None:
            first = fact.evidence[0] if fact.evidence else None
            key: tuple[uuid.UUID | None, str | None] = (
                fact.insight_id,
                first.source_url if first else None,
            )
            if key in seen:
                continue
            seen.add(key)
            resolved.append(
                (
                    fact.insight_id,
                    first.evidence_id if first else None,
                    first.source_url if first else None,
                    (first.excerpt[:MAX_EXCERPT_CHARS] if first and first.excerpt else None),
                )
            )
            continue
        # A bare URL is accepted only when the producer was actually shown it.
        if handle in source.source_urls:
            key = (None, handle)
            if key in seen:
                continue
            seen.add(key)
            resolved.append((None, None, handle[:1024], None))
            continue
        warnings.append(
            f"classification {index} cited {handle[:60]!r}, which was not in the evidence"
        )
    return resolved


def _apply_conflicts(
    answer: dict[str, Any],
    *,
    candidates: list[_Candidate],
    warnings: list[str],
) -> list[tuple[IntelligenceDimension, int, str, int]]:
    """Mark competing values as conflicted. Returns the conflict rows to write."""

    raw = answer.get("conflicts")
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append("`conflicts` was not a list and was ignored")
        return []

    known = {dimension.value: dimension for dimension in IntelligenceDimension}
    rows: list[tuple[IntelligenceDimension, int, str, int]] = []
    group = 0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"conflict {index} was not an object and was dropped")
            continue
        dimension = known.get(str(item.get("dimension", "")).strip().lower())
        statement = _text(item.get("statement"), limit=MAX_STATEMENT_CHARS)
        values = item.get("values")
        if dimension is None or statement is None or not isinstance(values, list):
            warnings.append(f"conflict {index} was incomplete and was dropped")
            continue
        wanted = {normalize_term(str(value)) for value in values}
        members = [
            candidate
            for candidate in candidates
            if candidate.dimension is dimension and candidate.normalized_value in wanted
        ]
        if len(members) < 2:
            # A conflict with one surviving side is not a conflict; it is an
            # assertion wearing the word. Recording it would tell an operator two
            # answers disagree when only one exists.
            warnings.append(
                f"conflict {index} on {dimension.value} named fewer than two stored values "
                "and was dropped"
            )
            continue
        for member in members:
            member.conflict_group = group
        rows.append((dimension, group, statement, len(members)))
        group += 1
    return rows


def _apply_caps(candidates: list[_Candidate], *, warnings: list[str]) -> list[_Candidate]:
    """Keep at most ``DIMENSION_CAPS[dimension]`` values, in answer order.

    A value that takes part in a conflict is never dropped by a cap: the whole
    point of recording the disagreement is that both sides stay visible.
    """

    kept: list[_Candidate] = []
    counts: dict[IntelligenceDimension, int] = {}
    for candidate in candidates:
        cap = DIMENSION_CAPS[candidate.dimension]
        count = counts.get(candidate.dimension, 0)
        if count >= cap and candidate.conflict_group is None:
            warnings.append(
                f"{candidate.dimension.value} exceeded its cap of {cap}; "
                f"dropped {candidate.value[:60]!r}"
            )
            continue
        counts[candidate.dimension] = count + 1
        kept.append(candidate)
    return kept


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def _persist(
    session: Session,
    *,
    company: Company,
    source: IntelligenceInput,
    candidates: list[_Candidate],
    conflicts: list[tuple[IntelligenceDimension, int, str, int]],
    unknown_dimensions: tuple[IntelligenceDimension, ...],
    raw_answer: str,
    job_id: uuid.UUID | None,
    created_by: str | None,
    warnings: list[str],
) -> CompanyIntelligenceVersion:
    next_number = (
        session.scalar(
            select(func.coalesce(func.max(CompanyIntelligenceVersion.version_number), 0)).where(
                CompanyIntelligenceVersion.company_id == company.id
            )
        )
        or 0
    ) + 1

    addressed = sorted(
        {candidate.dimension.value for candidate in candidates}
        | {dimension.value for dimension in unknown_dimensions}
    )

    version = CompanyIntelligenceVersion(
        company_id=company.id,
        version_number=next_number,
        dossier_version_id=source.dossier_version_id,
        dossier_version_number=source.dossier_version_number,
        sourced_fact_ids=list(source.fact_ids),
        sourced_fact_count=len(source.facts),
        taxonomy_versions=dict(source.taxonomy_versions),
        producer=source.producer,
        producer_version=source.producer_version,
        policy_version=source.policy_version,
        input_digest=source.digest,
        answer_digest=(
            hashlib.sha256(raw_answer.encode("utf-8")).hexdigest() if raw_answer else None
        ),
        dimensions_addressed=addressed,
        job_id=job_id,
        created_by=created_by,
    )
    try:
        with session.begin_nested():
            session.add(version)
            session.flush()
    except IntegrityError:
        # Another worker answered the identical question first. Its version is
        # as good as this one by construction, so take it rather than failing.
        winner = existing_version(session, company_id=company.id, input_digest=source.digest)
        if winner is None:  # pragma: no cover - defensive
            raise
        warnings.append("another worker produced this version concurrently; reused it")
        return winner

    supported = 0
    unresolved = 0
    ranks: dict[IntelligenceDimension, int] = {}

    for candidate in candidates:
        rank = ranks.get(candidate.dimension, 0)
        ranks[candidate.dimension] = rank + 1
        state, evidence_status, reason = _state_for(candidate)
        if evidence_status is IntelligenceEvidenceStatus.SUPPORTED:
            supported += 1
        if state in (IntelligenceValueState.UNRESOLVED, IntelligenceValueState.CONFLICTED):
            unresolved += 1

        term = candidate.resolution.term
        row = CompanyIntelligenceClassification(
            intelligence_version_id=version.id,
            company_id=company.id,
            dimension=candidate.dimension,
            rank=rank,
            is_primary=candidate.is_primary and rank == 0,
            model_value=candidate.value,
            rationale=candidate.rationale,
            taxonomy_id=(
                candidate.resolution.taxonomy.id
                if candidate.resolution.taxonomy is not None and term is not None
                else None
            ),
            taxonomy_version=(
                candidate.resolution.taxonomy.version
                if candidate.resolution.taxonomy is not None and term is not None
                else None
            ),
            term_id=term.id if term is not None else None,
            term_code=term.code if term is not None else None,
            term_label=term.canonical_label if term is not None else None,
            normalization=candidate.resolution.normalization,
            parent_term_code=candidate.resolution.parent_code,
            state=state,
            evidence_status=evidence_status,
            confidence=candidate.confidence,
            confidence_band=confidence_band(candidate.confidence),
            evidence_count=len(candidate.evidence),
            conflict_group=candidate.conflict_group,
            unresolved_reason=reason,
        )
        session.add(row)
        session.flush()

        for insight_id, evidence_id, source_url, excerpt in candidate.evidence:
            session.add(
                CompanyIntelligenceEvidenceLink(
                    classification_id=row.id,
                    intelligence_version_id=version.id,
                    insight_id=insight_id,
                    insight_evidence_id=evidence_id,
                    source_url=source_url,
                    excerpt=excerpt,
                    support=IntelligenceEvidenceSupport.SUPPORTS,
                )
            )

    # Dimensions the producer looked at and found nothing for. Stored as rows so
    # "we checked and the evidence is silent" is queryable, and so it cannot be
    # confused with a dimension nobody addressed.
    for dimension in unknown_dimensions:
        rank = ranks.get(dimension, 0)
        ranks[dimension] = rank + 1
        session.add(
            CompanyIntelligenceClassification(
                intelligence_version_id=version.id,
                company_id=company.id,
                dimension=dimension,
                rank=rank,
                model_value="(evidence is silent)",
                normalization=IntelligenceNormalization.NOT_APPLICABLE,
                state=IntelligenceValueState.UNKNOWN,
                evidence_status=IntelligenceEvidenceStatus.INSUFFICIENT,
                unresolved_reason=REASON_SILENT,
            )
        )

    for dimension, group, statement, member_count in conflicts:
        session.add(
            CompanyIntelligenceConflict(
                intelligence_version_id=version.id,
                dimension=dimension,
                conflict_group=group,
                member_count=member_count,
                statement=statement,
            )
        )

    version.classification_count = len(candidates) + len(unknown_dimensions)
    version.supported_count = supported
    version.unresolved_count = unresolved + len(unknown_dimensions)
    version.conflict_count = len(conflicts)
    version.warnings = list(warnings)
    session.flush()
    return version


def _state_for(
    candidate: _Candidate,
) -> tuple[IntelligenceValueState, IntelligenceEvidenceStatus, str | None]:
    if candidate.conflict_group is not None:
        status = (
            IntelligenceEvidenceStatus.SUPPORTED
            if candidate.evidence
            else IntelligenceEvidenceStatus.INSUFFICIENT
        )
        return IntelligenceValueState.CONFLICTED, status, REASON_CONFLICT
    if not candidate.evidence:
        return (
            IntelligenceValueState.UNRESOLVED,
            IntelligenceEvidenceStatus.INSUFFICIENT,
            REASON_NO_EVIDENCE,
        )
    if candidate.resolution.normalization is IntelligenceNormalization.UNMAPPED:
        return (
            IntelligenceValueState.UNRESOLVED,
            IntelligenceEvidenceStatus.SUPPORTED,
            REASON_UNMAPPED,
        )
    return IntelligenceValueState.RESOLVED, IntelligenceEvidenceStatus.SUPPORTED, None


def select_current(
    session: Session,
    *,
    company: Company,
    version: CompanyIntelligenceVersion,
    actor: str = PRODUCER_ACTOR,
) -> CompanyIntelligenceVersion:
    """Make one version the current understanding, superseding the previous.

    Supersedes rather than deletes, exactly like the dossier it reads. An
    operator who reviewed an earlier version can still see what they reviewed.
    """

    if version.company_id != company.id:
        raise IntelligenceProducerError(
            "cannot select an intelligence version belonging to another company"
        )
    previous = session.scalars(
        select(CompanyIntelligenceVersion).where(
            CompanyIntelligenceVersion.company_id == company.id,
            CompanyIntelligenceVersion.is_current.is_(True),
        )
    ).first()
    if previous is not None and previous.id == version.id:
        return version
    if previous is not None:
        previous.is_current = False
        previous.superseded_at = func.now()
        session.flush()
    version.is_current = True
    version.superseded_at = None
    session.flush()
    return version


def current_version(
    session: Session, *, company_id: uuid.UUID
) -> CompanyIntelligenceVersion | None:
    """The selected understanding, or None when nothing has been produced."""

    return session.scalars(
        select(CompanyIntelligenceVersion).where(
            CompanyIntelligenceVersion.company_id == company_id,
            CompanyIntelligenceVersion.is_current.is_(True),
        )
    ).first()


def _text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        return None
    return cleaned[:limit]


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if number < 0.0 or number > 1.0:
        return None
    return number
