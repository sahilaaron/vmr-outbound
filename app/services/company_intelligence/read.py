"""The typed read model for Company Intelligence (CI-001).

Everything outside this package reads Company Intelligence through here.
Campaign targeting, Saved Audiences, Company pages, reporting, scoring and — in
its own branch, later — Personalization all get the same frozen dataclasses, and
none of them touches an intelligence table directly. That is deliberate: the
storage layout will change (a second taxonomy release, a richer conflict model, a
producer that emits something new), and a consumer that joined to
``company_intelligence_classifications`` would break when it did.

Four states this model keeps strictly apart, because collapsing any two of them
is how an unverified guess becomes a filter somebody targets on:

``latest_model_version``
    The newest thing the producer wrote, reviewed or not.

``latest_reviewed_version``
    The newest version an operator actually made a decision against. Null when
    nobody has looked yet — which is the common case, and worth saying out loud.

``effective``
    What the system currently believes: the current model version with the
    Company's current operator decisions applied on top. Every value says who is
    responsible for it — model, operator-confirmed, or operator-corrected.

``unresolved`` / ``conflicted``
    Values that are explicitly *not* settled. They are returned, not hidden. A
    read model that only surfaced clean answers would let a caller filter on
    "industry = Manufacturing" and never learn that the evidence also said
    Chemicals.

One rule for every consumer, stated here because this is the file they will
read: **nothing in this model makes a Contact outreach-eligible.** A
classification is understanding. Eligibility, verification, suppression and
sending are decided elsewhere and are not weakened by anything here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_intelligence import (
    CompanyIntelligenceClassification,
    CompanyIntelligenceConflict,
    CompanyIntelligenceDecision,
    CompanyIntelligenceEvidenceLink,
    CompanyIntelligenceVersion,
)
from app.models.enums import (
    IntelligenceConfidenceBand,
    IntelligenceDecisionAction,
    IntelligenceDimension,
    IntelligenceEvidenceStatus,
    IntelligenceNormalization,
    IntelligenceValueSource,
    IntelligenceValueState,
)
from app.services.company_intelligence.review import target_key_for


@dataclass(frozen=True)
class EvidenceView:
    """One citation behind one effective value."""

    insight_id: uuid.UUID | None
    insight_evidence_id: uuid.UUID | None
    source_url: str | None
    excerpt: str | None
    dossier_section: str | None
    contradicts: bool = False


@dataclass(frozen=True)
class ClassificationView:
    """One value, everything a reader needs to judge it, nothing internal."""

    dimension: IntelligenceDimension
    #: The producer's own wording, always preserved.
    model_value: str
    #: The canonical value when one was resolved, otherwise ``None``.
    term_code: str | None
    term_label: str | None
    parent_term_code: str | None
    #: What a screen should print: the canonical label when there is one, the
    #: producer's wording otherwise. Never a blank.
    display_value: str
    normalization: IntelligenceNormalization
    taxonomy_version: str | None
    state: IntelligenceValueState
    evidence_status: IntelligenceEvidenceStatus
    source: IntelligenceValueSource
    confidence: float | None
    confidence_band: IntelligenceConfidenceBand | None
    rank: int
    is_primary: bool
    conflict_group: int | None
    unresolved_reason: str | None
    evidence: tuple[EvidenceView, ...] = ()
    classification_id: uuid.UUID | None = None
    #: True when this value exists only because an operator asserted it and the
    #: current model version does not propose it. Surfaced rather than hidden:
    #: it is exactly the case where model and human disagree.
    operator_only: bool = False
    #: The operator note attached to the decision responsible for this value.
    decision_note: str | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None

    @property
    def settled(self) -> bool:
        """Resolved *and* backed by evidence or a human. Nothing else counts."""

        return self.state is IntelligenceValueState.RESOLVED and (
            self.evidence_status is IntelligenceEvidenceStatus.SUPPORTED
            or self.source is not IntelligenceValueSource.MODEL
        )

    @property
    def operator_confirmed(self) -> bool:
        return self.source in (
            IntelligenceValueSource.OPERATOR_CONFIRMED,
            IntelligenceValueSource.OPERATOR_CORRECTED,
        )


@dataclass(frozen=True)
class ConflictView:
    """One disagreement, kept as a disagreement."""

    dimension: IntelligenceDimension
    conflict_group: int
    statement: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class VersionView:
    """One produced version's identity and shape, without its rows."""

    version_id: uuid.UUID
    version_number: int
    producer: str
    producer_version: str
    policy_version: str
    dossier_version_id: uuid.UUID
    dossier_version_number: int
    sourced_fact_count: int
    taxonomy_versions: dict[str, str]
    input_digest: str
    classification_count: int
    supported_count: int
    unresolved_count: int
    conflict_count: int
    dimensions_addressed: tuple[str, ...]
    warnings: tuple[str, ...]
    is_current: bool
    created_at: datetime
    superseded_at: datetime | None = None


@dataclass(frozen=True)
class CompanyIntelligenceRead:
    """The whole stable contract for one Company."""

    company_id: uuid.UUID
    company_name: str
    latest_model_version: VersionView | None
    latest_reviewed_version: VersionView | None
    current_version: VersionView | None
    classifications: tuple[ClassificationView, ...] = ()
    conflicts: tuple[ConflictView, ...] = ()
    decision_count: int = 0
    #: Decisions in force that concern a value the current version does not
    #: propose. Named so a caller can show "a person decided this, the newest
    #: model run disagrees" rather than quietly preferring one of them.
    stale_decision_count: int = 0
    dimensions_addressed: frozenset[IntelligenceDimension] = field(default_factory=frozenset)

    @property
    def has_intelligence(self) -> bool:
        return self.current_version is not None

    def for_dimension(self, dimension: IntelligenceDimension) -> tuple[ClassificationView, ...]:
        """Every effective value on one dimension, in rank order."""

        return tuple(item for item in self.classifications if item.dimension is dimension)

    def primary_industry(self) -> ClassificationView | None:
        """The single primary industry, or None when there is not exactly one.

        Returns ``None`` rather than a best guess when the industry is
        conflicted or unresolved. A caller that wants "whatever we have" can ask
        :meth:`for_dimension`; a caller that wants a value it can act on gets
        nothing rather than a coin toss.
        """

        for item in self.for_dimension(IntelligenceDimension.INDUSTRY):
            if item.is_primary and item.state is IntelligenceValueState.RESOLVED:
                return item
        return None

    def settled_values(self, dimension: IntelligenceDimension) -> tuple[str, ...]:
        """Display values on one dimension that are resolved and backed.

        The one method a targeting or scoring feature should normally use.
        """

        return tuple(item.display_value for item in self.for_dimension(dimension) if item.settled)

    def unresolved(self) -> tuple[ClassificationView, ...]:
        return tuple(
            item
            for item in self.classifications
            if item.state
            in (
                IntelligenceValueState.UNRESOLVED,
                IntelligenceValueState.CONFLICTED,
                IntelligenceValueState.UNKNOWN,
            )
        )


def get_company_intelligence(
    session: Session, *, company_id: uuid.UUID
) -> CompanyIntelligenceRead | None:
    """The full read model for one Company, or None when the Company is gone."""

    company = session.get(Company, company_id)
    if company is None:
        return None

    versions = list(
        session.scalars(
            select(CompanyIntelligenceVersion)
            .where(CompanyIntelligenceVersion.company_id == company_id)
            .order_by(CompanyIntelligenceVersion.version_number.desc())
        ).all()
    )
    decisions = list(
        session.scalars(
            select(CompanyIntelligenceDecision)
            .where(
                CompanyIntelligenceDecision.company_id == company_id,
                CompanyIntelligenceDecision.is_current.is_(True),
            )
            .order_by(CompanyIntelligenceDecision.created_at.asc())
        ).all()
    )
    all_decision_count = (
        session.scalar(
            select(CompanyIntelligenceDecision.id)
            .where(CompanyIntelligenceDecision.company_id == company_id)
            .limit(1)
        )
        is not None
    )

    if not versions:
        return CompanyIntelligenceRead(
            company_id=company_id,
            company_name=company.name,
            latest_model_version=None,
            latest_reviewed_version=None,
            current_version=None,
            decision_count=len(decisions) if all_decision_count else 0,
        )

    current = next((item for item in versions if item.is_current), versions[0])
    reviewed_ids = {
        decision.intelligence_version_id
        for decision in decisions
        if decision.intelligence_version_id is not None
    }
    reviewed = next((item for item in versions if item.id in reviewed_ids), None)

    classifications = list(
        session.scalars(
            select(CompanyIntelligenceClassification)
            .where(CompanyIntelligenceClassification.intelligence_version_id == current.id)
            .order_by(
                CompanyIntelligenceClassification.dimension,
                CompanyIntelligenceClassification.rank,
            )
        ).all()
    )
    evidence = _evidence_by_classification(session, version_id=current.id)
    conflicts = _conflicts(session, version_id=current.id, classifications=classifications)

    views, stale = _apply_decisions(classifications, evidence, decisions)

    return CompanyIntelligenceRead(
        company_id=company_id,
        company_name=company.name,
        latest_model_version=_version_view(versions[0]),
        latest_reviewed_version=_version_view(reviewed) if reviewed is not None else None,
        current_version=_version_view(current),
        classifications=views,
        conflicts=conflicts,
        decision_count=len(decisions),
        stale_decision_count=stale,
        dimensions_addressed=frozenset(item.dimension for item in classifications),
    )


def get_many(
    session: Session, *, company_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, CompanyIntelligenceRead]:
    """Read models for several Companies.

    Deliberately a bounded loop rather than one wide join. Correctness first:
    the effective-value resolution below is one place, and a second hand-tuned
    query would be a second place for it to be subtly different. When a caller
    with a real performance need arrives, this is where the optimisation goes,
    behind the same contract.
    """

    out: dict[uuid.UUID, CompanyIntelligenceRead] = {}
    for company_id in company_ids:
        view = get_company_intelligence(session, company_id=company_id)
        if view is not None:
            out[company_id] = view
    return out


def _version_view(version: CompanyIntelligenceVersion) -> VersionView:
    return VersionView(
        version_id=version.id,
        version_number=version.version_number,
        producer=version.producer,
        producer_version=version.producer_version,
        policy_version=version.policy_version,
        dossier_version_id=version.dossier_version_id,
        dossier_version_number=version.dossier_version_number,
        sourced_fact_count=version.sourced_fact_count,
        taxonomy_versions={
            str(key): str(value) for key, value in (version.taxonomy_versions or {}).items()
        },
        input_digest=version.input_digest,
        classification_count=version.classification_count,
        supported_count=version.supported_count,
        unresolved_count=version.unresolved_count,
        conflict_count=version.conflict_count,
        dimensions_addressed=tuple(str(item) for item in (version.dimensions_addressed or [])),
        warnings=tuple(str(item) for item in (version.warnings or [])),
        is_current=version.is_current,
        created_at=version.created_at,
        superseded_at=version.superseded_at,
    )


def _evidence_by_classification(
    session: Session, *, version_id: uuid.UUID
) -> dict[uuid.UUID, list[EvidenceView]]:
    out: dict[uuid.UUID, list[EvidenceView]] = {}
    for link in session.scalars(
        select(CompanyIntelligenceEvidenceLink)
        .where(CompanyIntelligenceEvidenceLink.intelligence_version_id == version_id)
        .order_by(CompanyIntelligenceEvidenceLink.created_at, CompanyIntelligenceEvidenceLink.id)
    ).all():
        out.setdefault(link.classification_id, []).append(
            EvidenceView(
                insight_id=link.insight_id,
                insight_evidence_id=link.insight_evidence_id,
                source_url=link.source_url,
                excerpt=link.excerpt,
                dossier_section=link.dossier_section,
                contradicts=link.support.value == "contradicts",
            )
        )
    return out


def _conflicts(
    session: Session,
    *,
    version_id: uuid.UUID,
    classifications: Sequence[CompanyIntelligenceClassification],
) -> tuple[ConflictView, ...]:
    rows = session.scalars(
        select(CompanyIntelligenceConflict)
        .where(CompanyIntelligenceConflict.intelligence_version_id == version_id)
        .order_by(CompanyIntelligenceConflict.dimension, CompanyIntelligenceConflict.conflict_group)
    ).all()
    out: list[ConflictView] = []
    for row in rows:
        values = tuple(
            item.term_label or item.model_value
            for item in classifications
            if item.dimension is row.dimension and item.conflict_group == row.conflict_group
        )
        out.append(
            ConflictView(
                dimension=row.dimension,
                conflict_group=row.conflict_group,
                statement=row.statement,
                values=values,
            )
        )
    return tuple(out)


def _apply_decisions(
    classifications: Sequence[CompanyIntelligenceClassification],
    evidence: dict[uuid.UUID, list[EvidenceView]],
    decisions: Sequence[CompanyIntelligenceDecision],
) -> tuple[tuple[ClassificationView, ...], int]:
    """Resolve effective values: the model version with decisions applied.

    The whole of the operator-authority policy lives in this one function, on
    purpose. Four actions, and each one has exactly one meaning:

    * ``CONFIRM``   — the value stands, and a human is now responsible for it.
    * ``CORRECT``   — the value is replaced. The original disappears from the
      effective set and remains in the stored version, where it always was.
    * ``MARK_UNRESOLVED`` — the value is kept but explicitly unsettled.
    * ``REJECT``    — the value is removed from the effective set.

    A decision whose target the current version does not propose still applies
    (except ``REJECT``, which has nothing to remove) and is flagged
    ``operator_only``, because a person deciding something the newest model run
    did not offer is a disagreement worth seeing, not an error to swallow.
    """

    by_target: dict[tuple[IntelligenceDimension, str], CompanyIntelligenceDecision] = {
        (decision.dimension, decision.target_key): decision for decision in decisions
    }
    consumed: set[tuple[IntelligenceDimension, str]] = set()
    views: list[ClassificationView] = []
    ranks: dict[IntelligenceDimension, int] = {}

    def next_rank(dimension: IntelligenceDimension) -> int:
        rank = ranks.get(dimension, 0)
        ranks[dimension] = rank + 1
        return rank

    for row in classifications:
        key = (row.dimension, target_key_for(row))
        decision = by_target.get(key)
        if decision is not None:
            consumed.add(key)
        view = _view_for(row, evidence.get(row.id, []), decision, rank=None)
        if view is None:
            continue
        views.append(view)

    for key, decision in by_target.items():
        if key in consumed:
            continue
        view = _view_from_decision(decision)
        if view is not None:
            views.append(view)

    # Re-rank deterministically after decisions have added and removed values, so
    # "rank 0" means the same thing to every reader.
    ordered: list[ClassificationView] = []
    for dimension in IntelligenceDimension:
        members = [item for item in views if item.dimension is dimension]
        primary_index = next(
            (index for index, item in enumerate(members) if item.is_primary),
            None,
        )
        if primary_index is not None and primary_index != 0:
            members.insert(0, members.pop(primary_index))
        for item in members:
            rank = next_rank(dimension)
            ordered.append(
                ClassificationView(
                    **{
                        **item.__dict__,
                        "rank": rank,
                        "is_primary": item.is_primary and rank == 0,
                    }
                )
            )

    stale = sum(1 for item in ordered if item.operator_only)
    return tuple(ordered), stale


def _view_for(
    row: CompanyIntelligenceClassification,
    evidence: list[EvidenceView],
    decision: CompanyIntelligenceDecision | None,
    *,
    rank: int | None,
) -> ClassificationView | None:
    state = row.state
    source = IntelligenceValueSource.MODEL
    display = row.term_label or row.model_value
    term_code = row.term_code
    term_label = row.term_label
    normalization = row.normalization
    is_primary = row.is_primary
    note = None
    decided_at = None
    decided_by = None

    if decision is not None:
        note = decision.note
        decided_at = decision.created_at
        decided_by = decision.actor
        if decision.action is IntelligenceDecisionAction.REJECT:
            return None
        if decision.action is IntelligenceDecisionAction.CONFIRM:
            state = IntelligenceValueState.RESOLVED
            source = IntelligenceValueSource.OPERATOR_CONFIRMED
        elif decision.action is IntelligenceDecisionAction.MARK_UNRESOLVED:
            state = IntelligenceValueState.UNRESOLVED
            source = IntelligenceValueSource.OPERATOR_UNRESOLVED
        elif decision.action is IntelligenceDecisionAction.CORRECT:
            state = IntelligenceValueState.RESOLVED
            source = IntelligenceValueSource.OPERATOR_CORRECTED
            term_code = decision.corrected_term_code or None
            term_label = decision.corrected_term_label or None
            display = term_label or decision.corrected_value or display
            normalization = (
                IntelligenceNormalization.CANONICAL
                if decision.corrected_term_code
                else IntelligenceNormalization.UNMAPPED
            )
            is_primary = decision.set_primary or is_primary

    return ClassificationView(
        dimension=row.dimension,
        model_value=row.model_value,
        term_code=term_code,
        term_label=term_label,
        parent_term_code=row.parent_term_code,
        display_value=display,
        normalization=normalization,
        taxonomy_version=row.taxonomy_version,
        state=state,
        evidence_status=row.evidence_status,
        source=source,
        confidence=row.confidence,
        confidence_band=row.confidence_band,
        rank=rank if rank is not None else row.rank,
        is_primary=is_primary,
        conflict_group=row.conflict_group,
        unresolved_reason=row.unresolved_reason,
        evidence=tuple(evidence),
        classification_id=row.id,
        decision_note=note,
        decided_at=decided_at,
        decided_by=decided_by,
    )


def _view_from_decision(decision: CompanyIntelligenceDecision) -> ClassificationView | None:
    """An effective value that exists only because an operator said so."""

    if decision.action is IntelligenceDecisionAction.REJECT:
        # Nothing to remove from the current version, so nothing to show as a
        # value. The decision is still visible in the correction history.
        return None
    if decision.action is IntelligenceDecisionAction.MARK_UNRESOLVED:
        state = IntelligenceValueState.UNRESOLVED
        source = IntelligenceValueSource.OPERATOR_UNRESOLVED
    elif decision.action is IntelligenceDecisionAction.CORRECT:
        state = IntelligenceValueState.RESOLVED
        source = IntelligenceValueSource.OPERATOR_CORRECTED
    else:
        state = IntelligenceValueState.RESOLVED
        source = IntelligenceValueSource.OPERATOR_CONFIRMED

    display = (
        decision.corrected_term_label
        or decision.corrected_value
        or decision.target_label
        or decision.target_key.removeprefix("text:")
    )
    return ClassificationView(
        dimension=decision.dimension,
        model_value=display,
        term_code=decision.corrected_term_code,
        term_label=decision.corrected_term_label,
        parent_term_code=None,
        display_value=display,
        normalization=(
            IntelligenceNormalization.CANONICAL
            if decision.corrected_term_code
            else IntelligenceNormalization.UNMAPPED
        ),
        taxonomy_version=None,
        state=state,
        # An operator assertion is not evidence. It is authority, which the
        # `source` field carries; claiming SUPPORTED here would put a human's
        # judgement in the same column as a cited page.
        evidence_status=IntelligenceEvidenceStatus.INSUFFICIENT,
        source=source,
        confidence=None,
        confidence_band=None,
        rank=0,
        is_primary=decision.set_primary,
        conflict_group=None,
        unresolved_reason=None,
        evidence=(),
        classification_id=decision.classification_id,
        operator_only=True,
        decision_note=decision.note,
        decided_at=decision.created_at,
        decided_by=decision.actor,
    )
