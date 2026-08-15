"""Company-domain resolution decisions (DAT-017A).

One row per decision, append-only, one current decision per **subject**.

A subject is whatever the acquisition surface actually produced. A Chrome
capture produces a ``linkedin_profile_snapshots`` row before any Contact exists,
so the decision hangs off the capture. Google Sheets produces a permanent
``Contact`` directly — there is no capture and inventing one would be a lie
about where the evidence came from — so the decision hangs off the Contact.
Exactly one of the two is set, enforced by a check constraint.

That is the whole of what makes this ledger acquisition-source-agnostic, and it
is deliberately a *second* subject column rather than a nullable capture plus a
convention: a decision with neither owner would be unattributable evidence, and
one with both would let two surfaces disagree about the same decision row.
Everything downstream — the policy, the gates, the approved-mapping store, the
Company link — is already keyed on the company and the evidence, not on how the
person arrived, and none of it changes.

DAT-010 stores what the provider *returned* and DAT-014 stores what the operator
*confirmed*. Neither can answer the question DAT-017A has to answer afterwards:
"why does this contact carry this domain, how sure were we, and what changed
when somebody disagreed?" That question needs the reasoning kept, not just the
conclusion, so this table records the decision itself:

* the state reached — ``confirmed``, ``provisional`` or ``unresolved``;
* the policy version that reached it, so an old decision stays interpretable
  after the rules change;
* the company name as captured and as normalized for matching;
* every candidate that was considered, with why each was kept or rejected;
* the selected candidate, its provider and its provider rank;
* the deterministic reason codes and the warnings;
* whether a paid provider call actually happened;
* when the decision was made, by whom, and when it was superseded.

Three properties are structural rather than conventional:

**Append-only.** A correction never rewrites a decision. It marks the current
row superseded and inserts a new one, so the earlier evidence — including the
candidate set that was live at the time — survives the disagreement. Nothing in
this module offers an update path for a decided row.

**One current decision per subject**, enforced by a partial unique index per
subject column. That is what makes a retry idempotent at the database rather
than only in the service that happens to be writing.

**A state cannot contradict its domain.** A check constraint requires
``unresolved`` to carry no selected domain and the other two states to carry
one, so "resolved, but to nothing" is unrepresentable rather than merely
unwritten.

The raw acquisition evidence is untouched, as always: this row points at its
subject and at the DAT-010 candidate record, and rewrites neither.
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
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import DomainResolutionKind, DomainResolutionState

# Both halves of the invariant, in the database's own terms. PostgreSQL stores
# the enum LABEL, which SQLAlchemy emits as the member NAME (upper-case), so the
# literal here is 'UNRESOLVED' rather than the Python value 'unresolved'.
_STATE_MATCHES_DOMAIN = (
    "(state = 'UNRESOLVED' AND selected_domain IS NULL) OR "
    "(state <> 'UNRESOLVED' AND selected_domain IS NOT NULL)"
)

#: Exactly one acquisition subject. Neither would be unattributable evidence;
#: both would let two surfaces claim the same decision.
_SINGLE_SUBJECT = "(capture_id IS NULL) <> (contact_id IS NULL)"


class CompanyDomainResolution(Base):
    """One company-domain resolution decision for one acquisition subject."""

    __tablename__ = "company_domain_resolutions"
    __table_args__ = (
        # Decisions are numbered per subject, so history reads in order and a
        # concurrent double-write collides instead of interleaving. PostgreSQL
        # treats NULLs as distinct in a unique constraint, so the capture
        # constraint simply does not apply to contact-subject rows, and vice
        # versa — which is why two constraints express the rule rather than one
        # coalesced key that would have to invent a placeholder value.
        UniqueConstraint(
            "capture_id", "decision_number", name="uq_company_domain_resolutions_number"
        ),
        UniqueConstraint(
            "contact_id", "decision_number", name="uq_company_domain_resolutions_contact_number"
        ),
        # Exactly one live decision per subject. A retry that reaches the same
        # answer writes nothing; a correction supersedes before it inserts.
        Index(
            "uq_company_domain_resolutions_current",
            "capture_id",
            unique=True,
            postgresql_where="is_current AND capture_id IS NOT NULL",
        ),
        Index(
            "uq_company_domain_resolutions_contact_current",
            "contact_id",
            unique=True,
            postgresql_where="is_current AND contact_id IS NOT NULL",
        ),
        Index("ix_company_domain_resolutions_capture", "capture_id"),
        Index(
            "ix_company_domain_resolutions_contact",
            "contact_id",
            postgresql_where="contact_id IS NOT NULL",
        ),
        Index(
            "ix_company_domain_resolutions_company",
            "resolved_company_id",
            postgresql_where="resolved_company_id IS NOT NULL",
        ),
        Index("ix_company_domain_resolutions_state", "state"),
        # Named without the table prefix: the metadata naming convention adds
        # ``ck_<table>_`` for us, and repeating it here would produce
        # ``ck_company_domain_resolutions_ck_company_domain_resolutions_...``.
        CheckConstraint("decision_number > 0", name="decision_number_positive"),
        CheckConstraint(_STATE_MATCHES_DOMAIN, name="state_matches_domain"),
        CheckConstraint(_SINGLE_SUBJECT, name="single_subject"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # --- What this decision is about ------------------------------------------
    # The capture this decision is about, for a decision reached before any
    # permanent Contact existed. NULL for a contact-subject decision.
    capture_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_profile_snapshots.id", ondelete="CASCADE"),
        nullable=True,
    )
    # The permanent Contact this decision is about, for a surface that produced
    # the person directly. CASCADE for the same reason the capture edge does: a
    # decision about a record that no longer exists explains nothing.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=True,
    )
    # The DAT-010 record holding the provider candidates this decision read.
    # SET NULL rather than CASCADE: losing the candidate store must not silently
    # delete the decision that explains a live company link.
    enrichment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("salesnav_company_enrichments.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The permanent company this decision resolved to, when it resolved to one.
    resolved_company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )

    # --- History position ------------------------------------------------------
    decision_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- The decision ----------------------------------------------------------
    state: Mapped[DomainResolutionState] = mapped_column(
        Enum(DomainResolutionState, name="domain_resolution_state"), nullable=False
    )
    decision_kind: Mapped[DomainResolutionKind] = mapped_column(
        Enum(DomainResolutionKind, name="domain_resolution_kind"),
        nullable=False,
        default=DomainResolutionKind.AUTOMATIC,
    )
    # The exact policy that produced this state. Kept per decision, not read from
    # code at display time, so a decision made under v1 never gets re-explained
    # by v2's rules.
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- What was matched ------------------------------------------------------
    # The employer name exactly as the capture showed it, and the normalized form
    # the policy actually compared. Both, because the normalization is lossy and
    # a reviewer needs to see what was thrown away.
    company_name_original: Mapped[str | None] = mapped_column(String(512), nullable=True)
    company_name_normalized: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Every candidate considered, each with ``domain``, ``name``, ``rank``,
    # ``eligible``, ``aligned``, ``alignment`` and ``rejection_reason``. A
    # rejected candidate is kept: "we looked at this and said no" is evidence,
    # and a later reviewer cannot re-derive it once the provider's answer moves.
    candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    selected_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    selected_candidate: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The provider's own 1-based ordering of the selected candidate. Recorded
    # because a reviewer should see it — never because it justifies anything.
    provider_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Why -------------------------------------------------------------------
    # Stable deterministic reason codes, in the order the policy applied them.
    reasons: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    warnings: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    # True only when this evaluation actually spent a provider call. A decision
    # that reused stored candidates or an existing mapping records False, which
    # is what makes "we do not re-buy what we already know" auditable rather
    # than asserted.
    provider_call_made: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Free text an operator supplies when correcting a decision.
    correction_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    @property
    def is_resolved(self) -> bool:
        """True when a domain was selected, at either level of certainty."""

        return self.state is not DomainResolutionState.UNRESOLVED

    @property
    def is_research_ready(self) -> bool:
        """True when company research may start from this decision.

        Both ``confirmed`` and ``provisional`` qualify — that is the entire
        point of ``provisional``. Everything *after* research asks a different
        question, and asks it of :mod:`app.services.resolution.gates`.
        """

        return self.is_resolved

    @property
    def subject_label(self) -> str:
        """Which acquisition surface's record this decision is about."""

        return "capture" if self.capture_id is not None else "contact"

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        subject = self.capture_id if self.capture_id is not None else self.contact_id
        return (
            f"CompanyDomainResolution({self.subject_label}_id={subject!r}, "
            f"n={self.decision_number}, state={self.state.value!r}, "
            f"domain={self.selected_domain!r})"
        )
