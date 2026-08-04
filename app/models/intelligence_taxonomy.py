"""Controlled, versioned vocabularies for Company Intelligence (CI-001).

Three tables, and the reason there are three rather than one is that a
classification has to survive its own vocabulary.

:class:`IntelligenceTaxonomy` is **one released vocabulary for one dimension**.
It is immutable in the sense that matters: a new edition of the industry list is
a new row with a new ``version``, never an edit of the old one. Exactly one
edition per dimension may be active at a time, and the active one is what new
production normalizes against.

:class:`IntelligenceTaxonomyTerm` is **one canonical value** inside a taxonomy,
optionally the child of another term. The industry taxonomy is two levels —
category and subcategory — but nothing in the schema assumes two, so a later
release can go deeper without a migration.

:class:`IntelligenceTaxonomyAlias` is **another way of saying a term**. Aliases
are what turn "Pharmaceuticals", "Pharma" and "pharma & healthcare" into one
canonical answer without the producer having to guess. An alias carries its
source, because an alias an operator approved and an alias a model proposed are
not the same kind of claim and normalization only trusts the former.

**Why classifications do not store a label.** A classification points at a term
id and *also* keeps the producer's original wording. Retiring a term therefore
never rewrites history: the historical classification still resolves to the term
it meant, and still shows what the model actually said. Replacing a vocabulary
is adding a taxonomy and activating it — never deleting the old one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import IntelligenceDimension, TaxonomyAliasSource


class IntelligenceTaxonomy(Base):
    """One released, versioned vocabulary for one classified dimension."""

    __tablename__ = "intelligence_taxonomies"
    __table_args__ = (
        UniqueConstraint("dimension", "version", name="uq_intelligence_taxonomies_version"),
        # At most one active edition per dimension, enforced by the database
        # rather than by a service check: activating an edition is two row
        # updates, and a half-applied activation must not be representable.
        Index(
            "uq_intelligence_taxonomies_active",
            "dimension",
            unique=True,
            postgresql_where="is_active",
        ),
        CheckConstraint("btrim(version) <> ''", name="version_not_blank"),
        # Composite target for the term foreign key below, so a term and the
        # taxonomy it belongs to can never disagree about their dimension.
        UniqueConstraint(
            "id",
            "dimension",
            name="uq_intelligence_taxonomies_id_dimension",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dimension: Mapped[IntelligenceDimension] = mapped_column(
        Enum(IntelligenceDimension, name="intelligence_dimension"), nullable=False
    )
    #: An opaque release label such as ``"vmr-industry-2026.07"``. Recorded on
    #: every classification so a stored value can always say which vocabulary
    #: normalized it, even after a newer edition is active.
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Where the vocabulary came from — an operator-supplied file, a standards
    #: body, an internal decision. Opaque; nothing branches on it.
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"IntelligenceTaxonomy(dimension={self.dimension.value!r}, "
            f"version={self.version!r}, active={self.is_active!r})"
        )


class IntelligenceTaxonomyTerm(Base):
    """One canonical value inside one taxonomy edition."""

    __tablename__ = "intelligence_taxonomy_terms"
    __table_args__ = (
        UniqueConstraint("taxonomy_id", "code", name="uq_intelligence_taxonomy_terms_code"),
        Index("ix_intelligence_taxonomy_terms_taxonomy", "taxonomy_id"),
        Index("ix_intelligence_taxonomy_terms_parent", "parent_id"),
        CheckConstraint("btrim(code) <> ''", name="code_not_blank"),
        CheckConstraint(
            "btrim(canonical_label) <> ''",
            name="label_not_blank",
        ),
        CheckConstraint("depth >= 0", name="depth_non_negative"),
        CheckConstraint("parent_id <> id", name="not_own_parent"),
        # A term's parent must live in the same taxonomy edition. Cross-edition
        # parentage would make a hierarchy that no single vocabulary version can
        # describe, which is exactly the kind of quiet inconsistency that later
        # reads as a taxonomy bug.
        UniqueConstraint(
            "id",
            "taxonomy_id",
            name="uq_intelligence_taxonomy_terms_id_taxonomy",
        ),
        ForeignKeyConstraint(
            ["parent_id", "taxonomy_id"],
            ["intelligence_taxonomy_terms.id", "intelligence_taxonomy_terms.taxonomy_id"],
            name="fk_intelligence_taxonomy_terms_parent_same_taxonomy",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    taxonomy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "intelligence_taxonomies.id", ondelete="CASCADE", name="fk_taxonomy_terms_taxonomy"
        ),
        nullable=False,
    )
    #: Stable, slug-shaped identifier. Survives a label rewording within an
    #: edition, and is what documentation and fixtures refer to.
    code: Mapped[str] = mapped_column(String(160), nullable=False)
    canonical_label: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The normalized form of ``canonical_label``, so an exact match can be found
    #: with one indexed lookup instead of a scan.
    normalized_label: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Retiring a term hides it from new production without deleting it, so
    #: historical classifications keep resolving.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"IntelligenceTaxonomyTerm(code={self.code!r}, label={self.canonical_label!r})"


class IntelligenceTaxonomyAlias(Base):
    """One accepted alternative spelling of a canonical term."""

    __tablename__ = "intelligence_taxonomy_aliases"
    __table_args__ = (
        # One normalized alias means one thing within a taxonomy edition.
        # Ambiguity here would make normalization order-dependent.
        UniqueConstraint(
            "taxonomy_id",
            "normalized_alias",
            name="uq_intelligence_taxonomy_aliases_normalized",
        ),
        Index("ix_intelligence_taxonomy_aliases_term", "term_id"),
        CheckConstraint(
            "btrim(normalized_alias) <> ''",
            name="alias_not_blank",
        ),
        # The alias, its term and the taxonomy it is unique within must be one
        # consistent triple. Two separate keys would let an alias be unique
        # inside a taxonomy it does not actually belong to.
        ForeignKeyConstraint(
            ["term_id", "taxonomy_id"],
            ["intelligence_taxonomy_terms.id", "intelligence_taxonomy_terms.taxonomy_id"],
            name="fk_intelligence_taxonomy_aliases_term_owner",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    taxonomy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "intelligence_taxonomies.id", ondelete="CASCADE", name="fk_taxonomy_aliases_taxonomy"
        ),
        nullable=False,
    )
    term_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[TaxonomyAliasSource] = mapped_column(
        Enum(TaxonomyAliasSource, name="taxonomy_alias_source"),
        nullable=False,
        default=TaxonomyAliasSource.SEED,
        server_default=TaxonomyAliasSource.SEED.name,
    )
    #: A model-proposed alias is stored but not trusted for normalization until
    #: an operator approves it. Approval is a decision with an author and a time.
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"IntelligenceTaxonomyAlias(alias={self.alias!r}, source={self.source.value!r}, "
            f"approved={self.approved_at is not None!r})"
        )
