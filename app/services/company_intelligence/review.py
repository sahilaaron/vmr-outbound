"""Operator decisions over produced Company Intelligence (CI-001).

A decision is a record, not an edit. Nothing in this module writes to a
:class:`~app.models.company_intelligence.CompanyIntelligenceVersion` or to any
classification row it owns; a produced version stays exactly as it was produced,
forever, and what an operator decided is stored beside it and applied when the
effective value is resolved. That is what makes "the model said X, a person said
Y, here is when and why" a thing you can read six months later.

Decisions are **company-scoped**, with version-scoped lineage. The distinction
took some thought and it matters:

* If a decision belonged to a version, every new production run would silently
  discard the operator's work, and the second run of a backfill would ask people
  to re-confirm two thousand values they had already confirmed.
* If a decision had no lineage at all, nobody could tell what the operator was
  looking at when they made it.

So a decision names the version and the classification it was made against, and
also carries a ``target_key`` — the identity of the *value*, independent of any
version. A confirmation of "Manufacturing" survives the next production run
because the next run still produces "Manufacturing"; a confirmation of a value
the newest run no longer proposes is still honoured, and reported as not present
in the current version rather than applied invisibly.

Superseding is two row updates and never a delete. Change your mind about a
correction and both decisions remain, in order, with their authors.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_intelligence import (
    CompanyIntelligenceClassification,
    CompanyIntelligenceDecision,
    CompanyIntelligenceVersion,
)
from app.models.enums import (
    IntelligenceDecisionAction,
    IntelligenceDimension,
    TaxonomyAliasSource,
)
from app.models.intelligence_taxonomy import IntelligenceTaxonomyTerm
from app.services.audit import record_audit_event
from app.services.company_intelligence import taxonomy as taxonomy_service
from app.services.company_intelligence.normalization import normalize_term

OPERATOR_ACTOR = "operator"

#: Prefix for the target key of a value that never mapped onto a canonical term.
#: Keeping the two namespaces apart means a free-text "Pumps" and a canonical
#: term whose code happens to be ``pumps`` can never collide.
TEXT_TARGET_PREFIX = "text:"


class IntelligenceReviewError(ValueError):
    """A decision that cannot be recorded as asked."""


@dataclass(frozen=True)
class DecisionRequest:
    """One operator judgement, before it is validated."""

    dimension: IntelligenceDimension
    action: IntelligenceDecisionAction
    target_key: str
    #: What the operator actually saw. Falls back to the reviewed
    #: classification's display value, then to the key itself.
    target_label: str | None = None
    classification_id: uuid.UUID | None = None
    corrected_term_id: uuid.UUID | None = None
    corrected_value: str | None = None
    set_primary: bool = False
    note: str | None = None


def target_key_for(classification: CompanyIntelligenceClassification) -> str:
    """The version-independent identity of one classified value."""

    if classification.term_code:
        return classification.term_code
    return f"{TEXT_TARGET_PREFIX}{normalize_term(classification.model_value)}"[:320]


def target_key_for_term(term: IntelligenceTaxonomyTerm) -> str:
    return term.code


def target_key_for_text(value: str) -> str:
    return f"{TEXT_TARGET_PREFIX}{normalize_term(value)}"[:320]


def record_decision(
    session: Session,
    *,
    company: Company,
    request: DecisionRequest,
    version: CompanyIntelligenceVersion | None,
    actor: str = OPERATOR_ACTOR,
    now: datetime | None = None,
) -> CompanyIntelligenceDecision:
    """Store one append-only decision, superseding any current one it replaces."""

    moment = now or datetime.now(UTC)
    target = request.target_key.strip()
    if not target:
        raise IntelligenceReviewError("a decision must name the value it concerns")

    classification: CompanyIntelligenceClassification | None = None
    if request.classification_id is not None:
        classification = session.get(CompanyIntelligenceClassification, request.classification_id)
        if classification is None:
            raise IntelligenceReviewError("the classification being reviewed no longer exists")
        if classification.company_id != company.id:
            raise IntelligenceReviewError(
                "cannot review a classification belonging to another company"
            )

    corrected_term: IntelligenceTaxonomyTerm | None = None
    corrected_value: str | None = None
    if request.action is IntelligenceDecisionAction.CORRECT:
        corrected_term, corrected_value = _correction(session, request)
        if corrected_term is None and corrected_value is None:
            raise IntelligenceReviewError(
                "a correction must name either a canonical term or a replacement value"
            )

    previous = session.scalars(
        select(CompanyIntelligenceDecision).where(
            CompanyIntelligenceDecision.company_id == company.id,
            CompanyIntelligenceDecision.dimension == request.dimension,
            CompanyIntelligenceDecision.target_key == target,
            CompanyIntelligenceDecision.is_current.is_(True),
        )
    ).first()

    decision = CompanyIntelligenceDecision(
        company_id=company.id,
        intelligence_version_id=version.id if version is not None else None,
        classification_id=classification.id if classification is not None else None,
        dimension=request.dimension,
        target_key=target[:320],
        target_label=_label_for(request, classification),
        action=request.action,
        corrected_term_id=corrected_term.id if corrected_term is not None else None,
        corrected_term_code=corrected_term.code if corrected_term is not None else None,
        corrected_term_label=(
            corrected_term.canonical_label if corrected_term is not None else None
        ),
        corrected_value=corrected_value,
        set_primary=(request.set_primary and request.dimension is IntelligenceDimension.INDUSTRY),
        note=request.note,
        actor=actor,
        is_current=True,
    )

    if previous is not None:
        # Clear first and flush: the partial unique index permits exactly one
        # current decision per (company, dimension, value) at any instant.
        previous.is_current = False
        previous.superseded_at = moment
        session.flush()

    session.add(decision)
    session.flush()

    if previous is not None:
        previous.superseded_by_id = decision.id
        session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="company_intelligence.decision_recorded",
        entity_type="company",
        entity_id=str(company.id),
        previous_state=previous.action.value if previous is not None else None,
        new_state=request.action.value,
        reason=request.note or f"{request.action.value} on {request.dimension.value}",
        context={
            "decision_id": str(decision.id),
            "dimension": request.dimension.value,
            "target_key": decision.target_key,
            "intelligence_version_id": str(version.id) if version is not None else None,
            "classification_id": (str(classification.id) if classification is not None else None),
        },
    )
    return decision


def _label_for(
    request: DecisionRequest,
    classification: CompanyIntelligenceClassification | None,
) -> str | None:
    """The human-readable value a decision concerns.

    The reviewed classification's own display value first, because that is
    literally what was on the operator's screen. The request's label second, for
    a decision about a value no classification carries. Never the target key: a
    canonical key is a slug, and printing it would show the operator a different
    string from the one they clicked.
    """

    if classification is not None:
        return (classification.term_label or classification.model_value)[:500]
    if request.target_label:
        return request.target_label.strip()[:500] or None
    return None


def _correction(
    session: Session, request: DecisionRequest
) -> tuple[IntelligenceTaxonomyTerm | None, str | None]:
    term: IntelligenceTaxonomyTerm | None = None
    if request.corrected_term_id is not None:
        term = taxonomy_service.get_term(session, request.corrected_term_id)
        if term is None:
            raise IntelligenceReviewError("the corrected value names a term that does not exist")
        if request.dimension not in taxonomy_service.NORMALIZING_DIMENSION:
            raise IntelligenceReviewError(
                f"{request.dimension.value} has no controlled vocabulary, so a correction "
                "must be written as free text rather than chosen from a list"
            )
        edition = taxonomy_service.active_taxonomy(session, dimension=request.dimension)
        if edition is None or term.taxonomy_id != edition.id:
            # Correcting onto a retired edition's term would put a value in front
            # of an operator that the next production run cannot reproduce.
            raise IntelligenceReviewError(
                "the chosen term is not part of the vocabulary currently active for "
                f"{request.dimension.value}"
            )
    value = (request.corrected_value or "").strip()
    return term, (value[:500] or None)


def map_alias(
    session: Session,
    *,
    dimension: IntelligenceDimension,
    alias: str,
    term_id: uuid.UUID,
    actor: str = OPERATOR_ACTOR,
) -> IntelligenceTaxonomyTerm:
    """Teach the vocabulary that ``alias`` means ``term``.

    The operator-facing half of "a value the producer wrote did not map".
    Recorded as an operator alias, which is authoritative immediately — unlike a
    model suggestion, which stays inert until somebody does exactly this.

    Mapping an alias does **not** retro-fit existing classifications. Stored
    versions are immutable; the alias changes what the *next* production run
    resolves, and the value in front of the operator is fixed by a correction.
    """

    term = taxonomy_service.get_term(session, term_id)
    if term is None:
        raise IntelligenceReviewError("cannot map an alias onto a term that does not exist")
    edition = taxonomy_service.active_taxonomy(session, dimension=dimension)
    if edition is None:
        raise IntelligenceReviewError(
            f"{dimension.value} has no active vocabulary to add an alias to"
        )
    if term.taxonomy_id != edition.id:
        raise IntelligenceReviewError(
            "the chosen term belongs to a different vocabulary than the active one"
        )
    taxonomy_service.add_alias(
        session,
        term=term,
        alias=alias,
        source=TaxonomyAliasSource.OPERATOR,
        created_by=actor,
        approved=True,
    )
    record_audit_event(
        session,
        actor=actor,
        action="company_intelligence.alias_mapped",
        entity_type="intelligence_taxonomy_term",
        entity_id=str(term.id),
        new_state=term.code,
        reason=f"{alias!r} mapped to {term.canonical_label!r}",
        context={"dimension": dimension.value, "taxonomy_version": edition.version},
    )
    return term


def current_decisions(
    session: Session, *, company_id: uuid.UUID
) -> list[CompanyIntelligenceDecision]:
    """Every decision currently in force for one Company."""

    return list(
        session.scalars(
            select(CompanyIntelligenceDecision)
            .where(
                CompanyIntelligenceDecision.company_id == company_id,
                CompanyIntelligenceDecision.is_current.is_(True),
            )
            .order_by(
                CompanyIntelligenceDecision.dimension,
                CompanyIntelligenceDecision.created_at,
            )
        ).all()
    )


def decision_history(
    session: Session, *, company_id: uuid.UUID
) -> list[CompanyIntelligenceDecision]:
    """Every decision ever made for one Company, newest first."""

    return list(
        session.scalars(
            select(CompanyIntelligenceDecision)
            .where(CompanyIntelligenceDecision.company_id == company_id)
            .order_by(
                CompanyIntelligenceDecision.created_at.desc(),
                CompanyIntelligenceDecision.id.desc(),
            )
        ).all()
    )
