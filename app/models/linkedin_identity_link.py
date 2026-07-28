"""One identifier, one contact, one decision — the DAT-019 identity link.

A LinkedIn person reaches this system under two different identifier forms: the
published ``/in/`` handle observed on a profile page, and the opaque Sales
Navigator member id observed on a results row. Identity matching is exact-string,
so before this table existed those were two keys for one human and the person
fragmented into two contacts (#195).

This table is the join. Each row says: *this identifier currently speaks for this
contact, on this evidence, decided this way, at this time* — which is enough to
answer every question the issue asked of it:

* what raw identifiers were observed          → ``identifier_kind`` / ``identifier_value``
* from which capture surface                  → ``source_surface`` / ``capture_id``
* which contact each identifier resolves to   → ``contact_id`` where state is ACTIVE
* why the resolution was accepted             → ``decision_kind`` / ``reason``
* what corroborating evidence was used        → ``corroboration``
* automatic or operator-reviewed              → ``decision_kind`` / ``decided_by``
* when it happened                            → ``decided_at``
* how to reverse it without losing history    → supersede, never delete

Two properties are load-bearing.

**Uniqueness is partial.** At most one ACTIVE, non-suspect row may hold a given
(kind, value): that is what stops two contacts claiming one identifier, and it is
enforced by the database rather than by a service being careful. Superseded
history and rows under review sit outside the constraint so they can coexist.

**Suspected aliases are excluded.** A legacy ``/in/<lowercased-member-id>`` value
is flagged rather than rewritten (the stored value is evidence and is left
alone), and being flagged takes it out of both matching and uniqueness — so it
cannot answer a lookup, and it cannot block the real handle from being recorded
when one is finally observed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import IdentityLinkDecision, IdentityLinkState, LinkedInIdentifierKind


class LinkedInIdentityLink(Base):
    """An identifier's current (or historical) claim on a contact."""

    __tablename__ = "linkedin_identity_links"

    __table_args__ = (
        # The duplicate-prevention guarantee. Partial, so that superseded rows
        # and rows awaiting review remain queryable beside the live claim, and
        # so that a flagged legacy alias never occupies the slot belonging to a
        # real handle.
        Index(
            "uq_linkedin_identity_links_active_identifier",
            "identifier_kind",
            "identifier_value",
            unique=True,
            postgresql_where=text("state = 'active' AND suspected_alias = false"),
        ),
        # Deterministic lookup. Identifier resolution is an indexed read, not the
        # O(n) Python scan that exact-URL matching used to perform.
        Index(
            "ix_linkedin_identity_links_lookup",
            "identifier_kind",
            "identifier_value",
        ),
        Index("ix_linkedin_identity_links_contact_id", "contact_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )

    # --- the identifier ---------------------------------------------------------
    identifier_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Vanity URLs are stored normalized. Member ids are stored VERBATIM, with
    # their original casing: they are case-sensitive, and folding them would
    # corrupt the identifier this row exists to name.
    identifier_value: Mapped[str] = mapped_column(String(512), nullable=False)

    # --- state and decision -----------------------------------------------------
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default=IdentityLinkState.ACTIVE.value
    )
    decision_kind: Mapped[str] = mapped_column(String(32), nullable=False)

    # A value carried over from data that already existed and which the
    # deterministic legacy test believes is a member-id alias rather than a
    # published handle. Flagged, never rewritten; excluded from matching.
    suspected_alias: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )

    # --- evidence ---------------------------------------------------------------
    capture_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_profile_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_surface: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # For a same-capture bridge this holds the other identifier observed on the
    # same person, so the co-occurrence that justified the link stays provable
    # after the fact.
    corroboration: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- audit ------------------------------------------------------------------
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<LinkedInIdentityLink {self.identifier_kind}={self.identifier_value!r} "
            f"state={self.state} contact={self.contact_id}>"
        )

    @property
    def is_active(self) -> bool:
        return self.state == IdentityLinkState.ACTIVE and not self.suspected_alias

    @property
    def is_same_capture_bridge(self) -> bool:
        return self.decision_kind == IdentityLinkDecision.SAME_CAPTURE_OBSERVED

    @property
    def kind(self) -> LinkedInIdentifierKind:
        return LinkedInIdentifierKind(self.identifier_kind)
