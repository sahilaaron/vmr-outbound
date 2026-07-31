"""Versioned, evidence-linked Company Intelligence (CI-001).

Company Intelligence is what the system *understands* about a Company, derived
only from Research evidence that has already been committed. It is not a summary
and it is not prose: it is a set of individually stored, individually reviewable
classifications, each of which can point at the evidence that produced it or say
plainly that there was none.

Six tables, and the split between them is the design.

:class:`CompanyIntelligenceVersion` is **one production run's answer**, whole and
immutable. It records exactly which inputs it read — one dossier version, one set
of sourced facts, one taxonomy edition per dimension, one producer version — and
a digest of all of that. Re-running the identical input under the identical
producer returns the existing version rather than making a second one; changing
any input makes a new version beside the old one.

:class:`CompanyIntelligenceClassification` is **one classified value**. Rows, not
a JSON blob, because these are the things an operator reviews one at a time, the
things a future audience filters on, and the things evidence attaches to.

:class:`CompanyIntelligenceEvidenceLink` is **why a classification exists**. It
points back into the Research artifacts — an insight, an evidence row, a dossier
section, a source URL — rather than copying their text. A classification with no
links is not a quiet failure: it is stored with
``evidence_status = INSUFFICIENT`` and states that in the read model.

:class:`CompanyIntelligenceConflict` is **a disagreement that was not flattened**.
Competing values keep their own rows and share a conflict group, so "the evidence
says two different things" survives as a first-class outcome instead of becoming
whichever value scored a hundredth higher.

:class:`CompanyIntelligenceDecision` is **an operator's judgement**, append-only.
It never edits a model-produced classification. The historical version stays
exactly as produced, and the effective value is the model version with current
decisions applied on top. That is what makes a correction reviewable rather than
merely done — and what lets a later model version arrive without erasing the work
a human already did.

:class:`CompanyIntelligenceJob` is **durable production work**, company-scoped.
It deliberately does not live in the Campaign Contact Agent queue: classifying a
company once is not something to repeat per contact, and putting it in that queue
would make it look like a pipeline stage that Contacts wait behind. See
``docs/decisions/ADR-CI-001-pipeline-placement.md``.

Two boundaries this module holds, and neither is negotiable:

* **No canonical Company field is written from here.** ``companies.industry`` and
  its neighbours are operational values with their own provenance model. A
  classification is evidence about a Company, never an overwrite of it.
* **Nothing here makes a Contact outreach-eligible.** There is no path from a
  classification to eligibility, verification, suppression release or sending.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    IntelligenceBackfillOutcome,
    IntelligenceBackfillStatus,
    IntelligenceConfidenceBand,
    IntelligenceDecisionAction,
    IntelligenceDimension,
    IntelligenceEvidenceStatus,
    IntelligenceEvidenceSupport,
    IntelligenceJobStatus,
    IntelligenceNormalization,
    IntelligenceValueState,
)

#: Job statuses in which a Company Intelligence job holds the single active slot
#: for its Company. Kept here so the model, the partial index predicate and the
#: queue service cannot drift apart.
ACTIVE_INTELLIGENCE_JOB_STATUSES: tuple[IntelligenceJobStatus, ...] = (
    IntelligenceJobStatus.PENDING,
    IntelligenceJobStatus.LEASED,
    IntelligenceJobStatus.IN_PROGRESS,
    IntelligenceJobStatus.RETRY_SCHEDULED,
)

# The native enum stores member *names* (uppercase) as its labels, matching every
# other enum in this repository. The predicate compares against those, cast to
# the enum type so the expression is IMMUTABLE (required for a partial index).
_ACTIVE_JOB_SQL = (
    "status IN ("
    "'PENDING'::intelligence_job_status,"
    "'LEASED'::intelligence_job_status,"
    "'IN_PROGRESS'::intelligence_job_status,"
    "'RETRY_SCHEDULED'::intelligence_job_status)"
)


class CompanyIntelligenceVersion(Base):
    """One immutable, fully-attributed reading of one Company's evidence."""

    __tablename__ = "company_intelligence_versions"
    __table_args__ = (
        Index("ix_company_intelligence_versions_company", "company_id"),
        Index("ix_company_intelligence_versions_dossier", "dossier_version_id"),
        UniqueConstraint(
            "company_id",
            "version_number",
            name="uq_company_intelligence_versions_number",
        ),
        # Idempotency, enforced by the database. ``input_digest`` covers the
        # dossier version, the exact set of sourced facts, the taxonomy editions
        # and the producer identity, so an identical re-run cannot create a
        # second version even if two workers race.
        UniqueConstraint(
            "company_id",
            "input_digest",
            name="uq_company_intelligence_versions_input",
        ),
        # At most one current version per Company. Selecting a different one is
        # two row updates, never a delete: superseded versions stay readable.
        Index(
            "uq_company_intelligence_versions_current",
            "company_id",
            unique=True,
            postgresql_where="is_current",
        ),
        CheckConstraint("version_number > 0", name="version_number_positive"),
        CheckConstraint(
            "btrim(producer) <> '' AND btrim(producer_version) <> ''",
            name="producer_named",
        ),
        # Ownership, enforced by the database rather than by a service check.
        # A version must read a dossier belonging to the SAME company: a
        # cross-company classification is a claim attributed to the wrong
        # organisation, which is the kind of wrong that reads as fact.
        ForeignKeyConstraint(
            ["dossier_version_id", "company_id"],
            ["company_dossier_versions.id", "company_dossier_versions.company_id"],
            name="fk_company_intelligence_versions_dossier_owner",
            ondelete="NO ACTION",
        ),
        # Composite target so classifications, conflicts and decisions can all
        # prove they belong to the same (version, company) pair.
        UniqueConstraint(
            "id",
            "company_id",
            name="uq_company_intelligence_versions_id_company",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- What it read ---------------------------------------------------------
    #
    # The dossier cannot be removed while an interpretation of it survives: a
    # classification whose input has vanished is an unfalsifiable claim.
    dossier_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dossier_version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Ids of the ``insights`` rows that were offered to the producer, in stable
    #: order. The link table records which ones each value actually used; this
    #: records what was *available*, which is what makes "the producer never saw
    #: it" distinguishable from "the producer saw it and did not use it".
    sourced_fact_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    sourced_fact_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: ``{dimension: taxonomy_version}`` at production time. A later vocabulary
    #: release cannot retroactively change what normalized this version.
    taxonomy_versions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # --- Who produced it ------------------------------------------------------
    producer: Mapped[str] = mapped_column(String(255), nullable=False)
    producer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The deterministic validation/normalization policy applied to the answer,
    #: versioned separately from the producer so a rules change is visible even
    #: when the model behind it did not move.
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    #: SHA-256 over the exact inputs plus producer and policy versions. The
    #: idempotency key of the whole area.
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    #: SHA-256 of the raw model answer. The answer itself is not stored: the
    #: parsed classifications are the record, and a raw transcript is where
    #: prompt content and configuration would leak into the database.
    answer_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- Quality signals ------------------------------------------------------
    classification_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    supported_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    unresolved_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    conflict_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Dimensions the producer addressed at all. A dimension absent from this
    #: list was never looked at, which is different from looked-at-and-unknown.
    dimensions_addressed: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    warnings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    #: The durable job that produced this version, when one did. Null for a
    #: version produced through a direct service call.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "company_intelligence_jobs.id",
            ondelete="SET NULL",
            name="fk_ci_versions_job",
        ),
        nullable=True,
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CompanyIntelligenceVersion(company_id={self.company_id!r}, "
            f"v={self.version_number!r}, current={self.is_current!r})"
        )


class CompanyIntelligenceClassification(Base):
    """One classified value on one dimension of one intelligence version."""

    __tablename__ = "company_intelligence_classifications"
    __table_args__ = (
        Index("ix_company_intelligence_classifications_version", "intelligence_version_id"),
        Index("ix_company_intelligence_classifications_company_dim", "company_id", "dimension"),
        Index("ix_company_intelligence_classifications_term", "term_id"),
        UniqueConstraint(
            "intelligence_version_id",
            "dimension",
            "rank",
            name="uq_company_intelligence_classifications_rank",
        ),
        CheckConstraint("rank >= 0", name="rank_non_negative"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
        CheckConstraint(
            "btrim(model_value) <> ''",
            name="model_value_not_blank",
        ),
        # A resolved value must have resolved to something. Either it normalized
        # onto a controlled term, or the dimension has no controlled vocabulary
        # and free text is the intended representation. Anything else claiming
        # RESOLVED would be a value with no meaning behind it.
        CheckConstraint(
            "state <> 'RESOLVED' OR term_id IS NOT NULL OR normalization = 'NOT_APPLICABLE'",
            name="resolved_has_value",
        ),
        # Only a conflicted value belongs to a conflict group.
        CheckConstraint(
            "conflict_group IS NULL OR state = 'CONFLICTED'",
            name="conflict_group_state",
        ),
        # Primary is a property of the industry dimension's top-ranked value.
        # Enforcing it here keeps "primary industry" a rank rather than a second
        # dimension that could disagree with the first.
        CheckConstraint(
            "is_primary = false OR rank = 0",
            name="primary_is_rank_zero",
        ),
        ForeignKeyConstraint(
            ["intelligence_version_id", "company_id"],
            [
                "company_intelligence_versions.id",
                "company_intelligence_versions.company_id",
            ],
            name="fk_company_intelligence_classifications_version_owner",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id",
            "intelligence_version_id",
            name="uq_company_intelligence_classifications_id_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    intelligence_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dimension: Mapped[IntelligenceDimension] = mapped_column(
        Enum(IntelligenceDimension, name="intelligence_dimension"), nullable=False
    )
    #: Position within the dimension. 0 is the primary/strongest value. Dense and
    #: deterministic, so two runs over the same evidence order values the same.
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # --- What the producer said, preserved verbatim ---------------------------
    #
    # Never overwritten by normalization. An operator has to be able to see the
    # model's own wording next to what it was mapped onto, because that is the
    # only way to notice a mapping that is technically valid and wrong.
    model_value: Mapped[str] = mapped_column(String(500), nullable=False)
    #: One short sentence from the producer about why. Untrusted text: displayed,
    #: never obeyed, never treated as evidence in its own right.
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- What it normalized to ------------------------------------------------
    taxonomy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "intelligence_taxonomies.id", ondelete="RESTRICT", name="fk_ci_classifications_taxonomy"
        ),
        nullable=True,
    )
    taxonomy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    term_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "intelligence_taxonomy_terms.id", ondelete="RESTRICT", name="fk_ci_classifications_term"
        ),
        nullable=True,
    )
    #: Denormalized so a stored classification still reads correctly if the
    #: vocabulary edition is later retired. The id remains authoritative.
    term_code: Mapped[str | None] = mapped_column(String(160), nullable=True)
    term_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalization: Mapped[IntelligenceNormalization] = mapped_column(
        Enum(IntelligenceNormalization, name="intelligence_normalization"),
        nullable=False,
        default=IntelligenceNormalization.UNMAPPED,
        server_default=IntelligenceNormalization.UNMAPPED.name,
    )
    #: The parent term when the producer supplied a subindustry, so the industry
    #: hierarchy is queryable without re-walking the taxonomy at read time.
    parent_term_code: Mapped[str | None] = mapped_column(String(160), nullable=True)

    # --- How settled it is ----------------------------------------------------
    state: Mapped[IntelligenceValueState] = mapped_column(
        Enum(IntelligenceValueState, name="intelligence_value_state"),
        nullable=False,
        default=IntelligenceValueState.RESOLVED,
        server_default=IntelligenceValueState.RESOLVED.name,
    )
    evidence_status: Mapped[IntelligenceEvidenceStatus] = mapped_column(
        Enum(IntelligenceEvidenceStatus, name="intelligence_evidence_status"),
        nullable=False,
        default=IntelligenceEvidenceStatus.INSUFFICIENT,
        server_default=IntelligenceEvidenceStatus.INSUFFICIENT.name,
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_band: Mapped[IntelligenceConfidenceBand | None] = mapped_column(
        Enum(IntelligenceConfidenceBand, name="intelligence_confidence_band"), nullable=True
    )
    evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    conflict_group: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Truthful reason a value is not resolved: ``no_evidence``,
    #: ``unmapped_value``, ``conflicting_evidence``, ``evidence_silent``.
    unresolved_reason: Mapped[str | None] = mapped_column(String(96), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CompanyIntelligenceClassification(dimension={self.dimension.value!r}, "
            f"value={self.term_code or self.model_value!r}, state={self.state.value!r})"
        )


class CompanyIntelligenceEvidenceLink(Base):
    """One reference from a classification back into committed Research."""

    __tablename__ = "company_intelligence_evidence_links"
    __table_args__ = (
        Index("ix_company_intelligence_evidence_links_classification", "classification_id"),
        Index("ix_company_intelligence_evidence_links_insight", "insight_id"),
        # The same insight cited twice for the same value is one citation.
        UniqueConstraint(
            "classification_id",
            "insight_id",
            "source_url",
            name="uq_company_intelligence_evidence_links_source",
        ),
        # A link has to point at something real. An "evidence" row that names
        # neither a persisted insight nor a URL is decoration.
        CheckConstraint(
            "insight_id IS NOT NULL OR source_url IS NOT NULL OR dossier_section IS NOT NULL",
            name="points_somewhere",
        ),
        ForeignKeyConstraint(
            ["classification_id", "intelligence_version_id"],
            [
                "company_intelligence_classifications.id",
                "company_intelligence_classifications.intelligence_version_id",
            ],
            name="fk_company_intelligence_evidence_links_classification_owner",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    classification_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    intelligence_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: The INS-001 claim this value rests on. SET NULL rather than CASCADE: if a
    #: claim is ever removed, the classification must still be able to say that
    #: it once rested on something, rather than silently losing its support.
    insight_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("insights.id", ondelete="SET NULL", name="fk_ci_evidence_links_insight"),
        nullable=True,
    )
    insight_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "insight_evidence.id", ondelete="SET NULL", name="fk_ci_evidence_links_insight_evidence"
        ),
        nullable=True,
    )
    #: Which dossier section the supporting material came from, when the support
    #: is the dossier itself rather than a discrete claim.
    dossier_section: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: A short quotation of what the source actually said. Untrusted text.
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    support: Mapped[IntelligenceEvidenceSupport] = mapped_column(
        Enum(IntelligenceEvidenceSupport, name="intelligence_evidence_support"),
        nullable=False,
        default=IntelligenceEvidenceSupport.SUPPORTS,
        server_default=IntelligenceEvidenceSupport.SUPPORTS.name,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CompanyIntelligenceEvidenceLink(classification_id={self.classification_id!r}, "
            f"support={self.support.value!r})"
        )


class CompanyIntelligenceConflict(Base):
    """One recorded disagreement between competing values on one dimension."""

    __tablename__ = "company_intelligence_conflicts"
    __table_args__ = (
        Index("ix_company_intelligence_conflicts_version", "intelligence_version_id"),
        UniqueConstraint(
            "intelligence_version_id",
            "dimension",
            "conflict_group",
            name="uq_company_intelligence_conflicts_group",
        ),
        CheckConstraint(
            "conflict_group >= 0",
            name="group_non_negative",
        ),
        CheckConstraint(
            "member_count >= 2",
            name="needs_two_members",
        ),
        ForeignKeyConstraint(
            ["intelligence_version_id"],
            ["company_intelligence_versions.id"],
            name="fk_company_intelligence_conflicts_version",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    intelligence_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dimension: Mapped[IntelligenceDimension] = mapped_column(
        Enum(IntelligenceDimension, name="intelligence_dimension"), nullable=False
    )
    #: Shared by every classification row taking part in this disagreement.
    conflict_group: Mapped[int] = mapped_column(Integer, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: A factual statement of what disagrees, in the producer's words. Not a
    #: resolution: this table exists precisely because nothing resolved it.
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CompanyIntelligenceConflict(dimension={self.dimension.value!r}, "
            f"group={self.conflict_group!r}, members={self.member_count!r})"
        )


class CompanyIntelligenceDecision(Base):
    """One append-only operator judgement about one classified value.

    Company-scoped rather than version-scoped **authority**, version-scoped
    *lineage*. The decision records which version and which classification it was
    made against, so the reasoning stays inspectable; but the decision itself is
    a statement about the Company, so a later model version does not silently
    discard a human's work. A decision that concerns a value the newest version
    no longer proposes is reported as such rather than applied invisibly.
    """

    __tablename__ = "company_intelligence_decisions"
    __table_args__ = (
        Index("ix_company_intelligence_decisions_company", "company_id", "dimension"),
        Index("ix_company_intelligence_decisions_version", "intelligence_version_id"),
        Index("ix_company_intelligence_decisions_classification", "classification_id"),
        # One current decision per (company, dimension, target). Superseding a
        # decision is two row updates; the superseded row keeps its author, its
        # reason and its time.
        Index(
            "uq_company_intelligence_decisions_current",
            "company_id",
            "dimension",
            "target_key",
            unique=True,
            postgresql_where="is_current",
        ),
        CheckConstraint(
            "btrim(target_key) <> ''",
            name="target_key_not_blank",
        ),
        # A correction has to say what the value should be.
        CheckConstraint(
            "action <> 'CORRECT' OR corrected_term_id IS NOT NULL OR corrected_value IS NOT NULL",
            name="correction_has_value",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The version the operator was looking at. SET NULL is deliberate: deleting
    #: a company cascades, but a decision must never be silently destroyed just
    #: because the version that prompted it was removed.
    intelligence_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "company_intelligence_versions.id", ondelete="SET NULL", name="fk_ci_decisions_version"
        ),
        nullable=True,
    )
    #: The exact classification row. Null when the operator added a value the
    #: producer never proposed.
    classification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "company_intelligence_classifications.id",
            ondelete="SET NULL",
            name="fk_ci_decisions_classification",
        ),
        nullable=True,
    )
    dimension: Mapped[IntelligenceDimension] = mapped_column(
        Enum(IntelligenceDimension, name="intelligence_dimension"), nullable=False
    )
    #: Stable identity of the *value* this decision concerns, independent of any
    #: version: a term code when the value normalized, otherwise the normalized
    #: text. This is what lets a confirmation survive a new production run.
    target_key: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[IntelligenceDecisionAction] = mapped_column(
        Enum(IntelligenceDecisionAction, name="intelligence_decision_action"), nullable=False
    )
    corrected_term_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "intelligence_taxonomy_terms.id",
            ondelete="RESTRICT",
            name="fk_ci_decisions_corrected_term",
        ),
        nullable=True,
    )
    corrected_term_code: Mapped[str | None] = mapped_column(String(160), nullable=True)
    corrected_term_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    corrected_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Applies only to the industry dimension: whether the corrected value is the
    #: primary industry. Ignored elsewhere.
    set_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "company_intelligence_decisions.id",
            ondelete="SET NULL",
            name="fk_ci_decisions_superseded_by",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CompanyIntelligenceDecision(dimension={self.dimension.value!r}, "
            f"action={self.action.value!r}, target={self.target_key!r})"
        )


class CompanyIntelligenceJob(Base):
    """One durable, idempotent Company Intelligence production job."""

    __tablename__ = "company_intelligence_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_company_intelligence_jobs_idempotency_key"),
        # At most one active job per Company. Two concurrent productions over the
        # same evidence would spend two model calls to reach one version.
        Index(
            "uq_company_intelligence_jobs_active_company",
            "company_id",
            unique=True,
            postgresql_where=_ACTIVE_JOB_SQL,
        ),
        Index("ix_company_intelligence_jobs_claimable", "status", "priority", "next_run_at"),
        Index("ix_company_intelligence_jobs_backfill", "backfill_run_id"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 100",
            name="max_attempts_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_kind: Mapped[str] = mapped_column(
        String(96),
        nullable=False,
        default="produce_company_intelligence",
        server_default="produce_company_intelligence",
    )
    idempotency_key: Mapped[str] = mapped_column(String(400), nullable=False)
    status: Mapped[IntelligenceJobStatus] = mapped_column(
        Enum(IntelligenceJobStatus, name="intelligence_job_status"),
        nullable=False,
        default=IntelligenceJobStatus.PENDING,
        server_default=IntelligenceJobStatus.PENDING.name,
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    producer_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The digest the job expects to produce for. Recorded at enqueue time so a
    #: job that becomes stale (new research landed) is visible rather than
    #: silently producing against different input than it was queued for.
    expected_input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backfill_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "company_intelligence_backfill_runs.id",
            ondelete="SET NULL",
            name="fk_ci_jobs_backfill_run",
        ),
        nullable=True,
    )
    input_reference: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(96), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Deliberately no `intelligence_version_id` here. The produced version points
    # back at its job (``CompanyIntelligenceVersion.job_id``), and one direction
    # is enough: two mutually-referencing foreign keys would need a deferred
    # constraint to create at all, and would then be two places that can disagree
    # about the same fact.
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CompanyIntelligenceJob(company_id={self.company_id!r}, "
            f"status={self.status.value!r}, attempts={self.attempts!r})"
        )


class CompanyIntelligenceBackfillRun(Base):
    """One bounded, resumable pass over Companies that already have Research."""

    __tablename__ = "company_intelligence_backfill_runs"
    __table_args__ = (
        Index("ix_company_intelligence_backfill_runs_status", "status", "created_at"),
        CheckConstraint(
            "batch_size >= 1 AND batch_size <= 1000",
            name="batch_size_range",
        ),
        CheckConstraint(
            "max_companies IS NULL OR max_companies >= 1",
            name="max_companies_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[IntelligenceBackfillStatus] = mapped_column(
        Enum(IntelligenceBackfillStatus, name="intelligence_backfill_status"),
        nullable=False,
        default=IntelligenceBackfillStatus.PREVIEW,
        server_default=IntelligenceBackfillStatus.PREVIEW.name,
    )
    #: A dry run enqueues nothing. It walks the same ordering, applies the same
    #: eligibility rules and records the same per-company outcomes, so the report
    #: an operator reads before committing is produced by the code that will run.
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    batch_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=25, server_default="25"
    )
    #: Hard ceiling on companies considered by this run, so an operator can start
    #: with fifty before committing to fifty thousand.
    max_companies: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Deterministic resume point: the last Company id processed in the run's
    #: fixed ordering. Restarting continues from here rather than from scratch.
    cursor_company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    considered_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    enqueued_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    skipped_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: ``{reason_code: count}``. What an operator reads to learn why a run did
    #: less than they expected; a silent skip is indistinguishable from success.
    skip_reasons: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    producer_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CompanyIntelligenceBackfillRun(label={self.label!r}, "
            f"status={self.status.value!r}, dry_run={self.dry_run!r})"
        )


class CompanyIntelligenceBackfillItem(Base):
    """What one backfill run decided about one Company, with a truthful reason."""

    __tablename__ = "company_intelligence_backfill_items"
    __table_args__ = (
        # One row per company per run. This is what makes a resumed run
        # idempotent: re-walking an already-processed company is a no-op rather
        # than a second job.
        UniqueConstraint(
            "backfill_run_id",
            "company_id",
            name="uq_company_intelligence_backfill_items_company",
        ),
        Index("ix_company_intelligence_backfill_items_run", "backfill_run_id", "sequence"),
        CheckConstraint(
            "outcome <> 'SKIPPED' OR skip_reason IS NOT NULL",
            name="skip_has_reason",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    backfill_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "company_intelligence_backfill_runs.id",
            ondelete="CASCADE",
            name="fk_ci_backfill_items_run",
        ),
        nullable=False,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[IntelligenceBackfillOutcome] = mapped_column(
        Enum(IntelligenceBackfillOutcome, name="intelligence_backfill_outcome"), nullable=False
    )
    skip_reason: Mapped[str | None] = mapped_column(String(96), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "company_intelligence_jobs.id", ondelete="SET NULL", name="fk_ci_backfill_items_job"
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"CompanyIntelligenceBackfillItem(company_id={self.company_id!r}, "
            f"outcome={self.outcome.value!r}, skip_reason={self.skip_reason!r})"
        )
