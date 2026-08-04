"""Controlled vocabulary service for Company Intelligence (CI-001).

Three jobs, and keeping them apart is what makes the vocabulary replaceable.

**Publishing.** :func:`create_taxonomy` stores one *edition* of a vocabulary.
:func:`activate_taxonomy` makes exactly one edition per dimension the one new
production normalizes against. Neither ever edits a published edition — a
corrected industry list is a new edition, and the old one stays readable so
every classification made under it still resolves.

**Resolving.** :func:`resolve` turns a written value into a
:class:`TermResolution`: canonical hit, alias hit, or nothing. It is
deterministic — same input, same active edition, same answer — and it never
guesses. There is no fuzzy match, no nearest neighbour, no "close enough":
either something in the vocabulary says this string means that term, or the
value stays unmapped and says so.

**Learning.** :func:`add_alias` records another way of saying a term. An alias
an operator wrote is authoritative immediately. An alias a *model* proposed is
stored unapproved and is invisible to :func:`resolve` until a human approves it,
because a producer that can widen its own vocabulary can quietly redefine what
any classification means.

Which vocabulary a dimension uses is declared once, in
:data:`NORMALIZING_DIMENSION`. Industry and subindustry share one edition on
purpose: they are two levels of a single hierarchy, and modelling them as two
vocabularies would let the parent list and the child list disagree about which
categories exist.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import IntelligenceDimension, IntelligenceNormalization, TaxonomyAliasSource
from app.models.intelligence_taxonomy import (
    IntelligenceTaxonomy,
    IntelligenceTaxonomyAlias,
    IntelligenceTaxonomyTerm,
)
from app.services.audit import record_audit_event
from app.services.company_intelligence.normalization import normalize_term

TAXONOMY_ACTOR = "system:company-intelligence"

#: Which vocabulary edition a dimension normalizes against.
#:
#: A dimension absent from this mapping has **no controlled vocabulary in this
#: release**. That is a decision, not an omission: products, services,
#: specialties, capabilities and specific geographies are company-specific by
#: nature, and a partial list of them would reject correct values while looking
#: authoritative. Those dimensions record the producer's own wording with
#: ``normalization = NOT_APPLICABLE``, which is honest and stays queryable.
NORMALIZING_DIMENSION: dict[IntelligenceDimension, IntelligenceDimension] = {
    IntelligenceDimension.INDUSTRY: IntelligenceDimension.INDUSTRY,
    # Subindustries are the child level of the industry hierarchy, not a second
    # vocabulary. One edition, two depths.
    IntelligenceDimension.SUBINDUSTRY: IntelligenceDimension.INDUSTRY,
    IntelligenceDimension.BUSINESS_MODEL: IntelligenceDimension.BUSINESS_MODEL,
    IntelligenceDimension.COMPANY_TYPE: IntelligenceDimension.COMPANY_TYPE,
    IntelligenceDimension.CUSTOMER_SEGMENT: IntelligenceDimension.CUSTOMER_SEGMENT,
    IntelligenceDimension.OPERATING_MARKET: IntelligenceDimension.OPERATING_MARKET,
    # CI-002. Geography was free text in the first release, which was honest and
    # useless: "EMEA", "our London office" and "Berlin" all landed in the same
    # shapeless column. It now normalizes against a versioned reference edition
    # of countries and cities. Both depths belong to one edition, exactly as
    # industry and subindustry do — a country list and a city list that could
    # disagree about which countries exist would be two vocabularies pretending
    # to be one.
    IntelligenceDimension.GEOGRAPHY: IntelligenceDimension.GEOGRAPHY,
}

#: Dimensions that read the *category* level of a hierarchy, and those that read
#: the *child* level. A value resolved at the wrong depth would put a
#: subcategory where a category belongs, which reads as a taxonomy that
#: contradicts itself.
#: GEOGRAPHY is deliberately absent: a geography value may legitimately be a
#: country (depth 0) or a city (depth 1), and pinning it to one depth would make
#: "United Kingdom" or "London" unresolvable depending on which was chosen.
_DEPTH_FOR_DIMENSION: dict[IntelligenceDimension, int | None] = {
    IntelligenceDimension.INDUSTRY: 0,
    IntelligenceDimension.SUBINDUSTRY: 1,
}


class TaxonomyError(ValueError):
    """A vocabulary operation that cannot be performed as asked."""


@dataclass(frozen=True)
class TermResolution:
    """What resolving one written value produced.

    ``term`` is ``None`` when nothing matched. ``normalization`` says which of
    the four outcomes this was, and the caller stores it verbatim: an operator
    has to be able to see that a value was matched by alias rather than written
    canonically, because that is where a wrong mapping hides.
    """

    normalization: IntelligenceNormalization
    term: IntelligenceTaxonomyTerm | None = None
    taxonomy: IntelligenceTaxonomy | None = None
    parent_code: str | None = None

    @property
    def mapped(self) -> bool:
        return self.term is not None


def create_taxonomy(
    session: Session,
    *,
    dimension: IntelligenceDimension,
    version: str,
    title: str,
    description: str | None = None,
    source: str | None = None,
    created_by: str | None = None,
) -> IntelligenceTaxonomy:
    """Store one new, inactive vocabulary edition."""

    clean_version = version.strip()
    if not clean_version:
        raise TaxonomyError("a taxonomy edition must carry a version label")
    existing = session.scalars(
        select(IntelligenceTaxonomy).where(
            IntelligenceTaxonomy.dimension == dimension,
            IntelligenceTaxonomy.version == clean_version,
        )
    ).first()
    if existing is not None:
        raise TaxonomyError(
            f"taxonomy {dimension.value}/{clean_version} already exists; "
            "publish a new version rather than editing a released one"
        )
    taxonomy = IntelligenceTaxonomy(
        dimension=dimension,
        version=clean_version,
        title=title.strip() or clean_version,
        description=description,
        source=source,
        created_by=created_by,
    )
    session.add(taxonomy)
    session.flush()
    return taxonomy


def add_term(
    session: Session,
    *,
    taxonomy: IntelligenceTaxonomy,
    code: str,
    canonical_label: str,
    parent: IntelligenceTaxonomyTerm | None = None,
    sort_order: int = 0,
    description: str | None = None,
) -> IntelligenceTaxonomyTerm:
    """Add one canonical term to an edition."""

    clean_code = code.strip()
    clean_label = canonical_label.strip()
    if not clean_code or not clean_label:
        raise TaxonomyError("a term needs both a code and a canonical label")
    if parent is not None and parent.taxonomy_id != taxonomy.id:
        raise TaxonomyError("a term's parent must belong to the same taxonomy edition")
    term = IntelligenceTaxonomyTerm(
        taxonomy_id=taxonomy.id,
        code=clean_code,
        canonical_label=clean_label,
        normalized_label=normalize_term(clean_label),
        parent_id=parent.id if parent is not None else None,
        depth=0 if parent is None else parent.depth + 1,
        sort_order=sort_order,
        description=description,
    )
    session.add(term)
    session.flush()
    return term


def add_alias(
    session: Session,
    *,
    term: IntelligenceTaxonomyTerm,
    alias: str,
    source: TaxonomyAliasSource = TaxonomyAliasSource.OPERATOR,
    created_by: str | None = None,
    approved: bool | None = None,
    now: datetime | None = None,
) -> IntelligenceTaxonomyAlias:
    """Record another way of saying ``term``.

    ``approved`` defaults to "yes for a human, no for a model". An unapproved
    alias is stored and visible in the vocabulary browser but is not used to
    resolve anything, so a model cannot widen the vocabulary it is scored
    against.
    """

    clean_alias = alias.strip()
    normalized = normalize_term(clean_alias)
    if not normalized:
        raise TaxonomyError("an alias must contain at least one alphanumeric character")

    if approved is None:
        approved = source is not TaxonomyAliasSource.MODEL_SUGGESTION

    existing = session.scalars(
        select(IntelligenceTaxonomyAlias).where(
            IntelligenceTaxonomyAlias.taxonomy_id == term.taxonomy_id,
            IntelligenceTaxonomyAlias.normalized_alias == normalized,
        )
    ).first()
    if existing is not None:
        if existing.term_id != term.id:
            raise TaxonomyError(
                f"{clean_alias!r} already resolves to a different term in this taxonomy; "
                "one alias cannot mean two things"
            )
        if approved and existing.approved_at is None:
            existing.approved_at = now or datetime.now(UTC)
            existing.approved_by = created_by
            session.flush()
        return existing

    record = IntelligenceTaxonomyAlias(
        taxonomy_id=term.taxonomy_id,
        term_id=term.id,
        alias=clean_alias[:255],
        normalized_alias=normalized[:255],
        source=source,
        created_by=created_by,
        approved_by=created_by if approved else None,
        approved_at=(now or datetime.now(UTC)) if approved else None,
    )
    session.add(record)
    session.flush()
    return record


def activate_taxonomy(
    session: Session,
    *,
    taxonomy: IntelligenceTaxonomy,
    actor: str = TAXONOMY_ACTOR,
    now: datetime | None = None,
) -> IntelligenceTaxonomy:
    """Make one edition the active vocabulary for its dimension.

    Supersedes rather than deletes. Every classification produced under the
    previous edition keeps pointing at the terms it actually used, and the
    edition it used stays readable — which is what "future vocabulary
    replacement without destroying historical classifications" means in practice.
    """

    moment = now or datetime.now(UTC)
    previous = session.scalars(
        select(IntelligenceTaxonomy).where(
            IntelligenceTaxonomy.dimension == taxonomy.dimension,
            IntelligenceTaxonomy.is_active.is_(True),
        )
    ).first()
    if previous is not None and previous.id == taxonomy.id:
        return taxonomy
    if previous is not None:
        previous.is_active = False
        previous.retired_at = moment
        session.flush()
    taxonomy.is_active = True
    taxonomy.activated_at = moment
    taxonomy.retired_at = None
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="company_intelligence.taxonomy_activated",
        entity_type="intelligence_taxonomy",
        entity_id=str(taxonomy.id),
        previous_state=previous.version if previous is not None else None,
        new_state=taxonomy.version,
        reason=f"active vocabulary for {taxonomy.dimension.value} changed",
    )
    return taxonomy


def active_taxonomy(
    session: Session, *, dimension: IntelligenceDimension
) -> IntelligenceTaxonomy | None:
    """The active edition for the vocabulary a dimension normalizes against."""

    vocabulary = NORMALIZING_DIMENSION.get(dimension)
    if vocabulary is None:
        return None
    return session.scalars(
        select(IntelligenceTaxonomy).where(
            IntelligenceTaxonomy.dimension == vocabulary,
            IntelligenceTaxonomy.is_active.is_(True),
        )
    ).first()


def active_versions(session: Session) -> dict[str, str]:
    """``{dimension: version}`` for every dimension with an active vocabulary.

    Snapshotted onto each produced version, so a later vocabulary release cannot
    retroactively change what normalized an existing classification.
    """

    rows = session.scalars(
        select(IntelligenceTaxonomy).where(IntelligenceTaxonomy.is_active.is_(True))
    ).all()
    by_vocabulary = {row.dimension: row.version for row in rows}
    return {
        dimension.value: by_vocabulary[vocabulary]
        for dimension, vocabulary in NORMALIZING_DIMENSION.items()
        if vocabulary in by_vocabulary
    }


def resolve(
    session: Session,
    *,
    dimension: IntelligenceDimension,
    value: str,
    taxonomy: IntelligenceTaxonomy | None = None,
) -> TermResolution:
    """Map one written value onto a canonical term, or say it did not.

    Order is canonical label first, then approved alias. Never fuzzy, never
    partial: an unrecognised value comes back ``UNMAPPED`` with the caller's
    original wording intact, which is a reviewable outcome. A near-miss silently
    accepted is not.
    """

    if dimension not in NORMALIZING_DIMENSION:
        return TermResolution(normalization=IntelligenceNormalization.NOT_APPLICABLE)

    normalized = normalize_term(value)
    if not normalized:
        return TermResolution(normalization=IntelligenceNormalization.UNMAPPED)

    edition = taxonomy if taxonomy is not None else active_taxonomy(session, dimension=dimension)
    if edition is None:
        # A dimension that *should* normalize but has no active edition is not
        # the same as a dimension with no vocabulary. Report it as unmapped so
        # the gap shows up in review rather than passing as free text.
        return TermResolution(normalization=IntelligenceNormalization.UNMAPPED)

    depth = _DEPTH_FOR_DIMENSION.get(dimension)

    statement = select(IntelligenceTaxonomyTerm).where(
        IntelligenceTaxonomyTerm.taxonomy_id == edition.id,
        IntelligenceTaxonomyTerm.normalized_label == normalized,
        IntelligenceTaxonomyTerm.is_active.is_(True),
    )
    if depth is not None:
        statement = statement.where(IntelligenceTaxonomyTerm.depth == depth)
    term = session.scalars(statement).first()
    if term is not None:
        return TermResolution(
            normalization=IntelligenceNormalization.CANONICAL,
            term=term,
            taxonomy=edition,
            parent_code=_parent_code(session, term),
        )

    alias_statement = (
        select(IntelligenceTaxonomyTerm)
        .join(
            IntelligenceTaxonomyAlias,
            IntelligenceTaxonomyAlias.term_id == IntelligenceTaxonomyTerm.id,
        )
        .where(
            IntelligenceTaxonomyAlias.taxonomy_id == edition.id,
            IntelligenceTaxonomyAlias.normalized_alias == normalized,
            # Unapproved model suggestions are stored but never resolve.
            IntelligenceTaxonomyAlias.approved_at.is_not(None),
            IntelligenceTaxonomyTerm.is_active.is_(True),
        )
    )
    if depth is not None:
        alias_statement = alias_statement.where(IntelligenceTaxonomyTerm.depth == depth)
    aliased = session.scalars(alias_statement).first()
    if aliased is not None:
        return TermResolution(
            normalization=IntelligenceNormalization.ALIAS,
            term=aliased,
            taxonomy=edition,
            parent_code=_parent_code(session, aliased),
        )

    return TermResolution(normalization=IntelligenceNormalization.UNMAPPED, taxonomy=edition)


def _parent_code(session: Session, term: IntelligenceTaxonomyTerm) -> str | None:
    if term.parent_id is None:
        return None
    parent = session.get(IntelligenceTaxonomyTerm, term.parent_id)
    return parent.code if parent is not None else None


def get_term(session: Session, term_id: uuid.UUID) -> IntelligenceTaxonomyTerm | None:
    """One term by id, for an operator correction that names a canonical value."""

    return session.get(IntelligenceTaxonomyTerm, term_id)


def list_terms(
    session: Session,
    *,
    taxonomy: IntelligenceTaxonomy,
    depth: int | None = None,
) -> list[IntelligenceTaxonomyTerm]:
    """Every term in one edition, in display order."""

    statement = select(IntelligenceTaxonomyTerm).where(
        IntelligenceTaxonomyTerm.taxonomy_id == taxonomy.id
    )
    if depth is not None:
        statement = statement.where(IntelligenceTaxonomyTerm.depth == depth)
    return list(
        session.scalars(
            statement.order_by(
                IntelligenceTaxonomyTerm.depth,
                IntelligenceTaxonomyTerm.sort_order,
                IntelligenceTaxonomyTerm.canonical_label,
            )
        ).all()
    )


def list_aliases(
    session: Session, *, term: IntelligenceTaxonomyTerm
) -> list[IntelligenceTaxonomyAlias]:
    """Every recorded alias of one term, approved or not."""

    return list(
        session.scalars(
            select(IntelligenceTaxonomyAlias)
            .where(IntelligenceTaxonomyAlias.term_id == term.id)
            .order_by(IntelligenceTaxonomyAlias.alias)
        ).all()
    )


def term_by_code(
    session: Session, *, dimension: IntelligenceDimension, code: str
) -> IntelligenceTaxonomyTerm | None:
    """One term of the active edition, by its stable code.

    Used where a caller already knows exactly which canonical value it means —
    geography, where deterministic extraction has already resolved the place —
    so there is nothing to match and nothing to guess.
    """

    edition = active_taxonomy(session, dimension=dimension)
    if edition is None:
        return None
    return session.scalars(
        select(IntelligenceTaxonomyTerm).where(
            IntelligenceTaxonomyTerm.taxonomy_id == edition.id,
            IntelligenceTaxonomyTerm.code == code,
        )
    ).first()
