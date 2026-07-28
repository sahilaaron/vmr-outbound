"""The selling organisation's own profile (KB-001).

This is the one record that says who *we* are. It is deliberately a different
table from ``companies``: that table holds prospect companies, which are
externally researched, provenance-tracked, and never authored by us. Putting
the seller in the same table would have made "is this us or a prospect?" a
column value rather than a schema fact, and every later query would have had
to remember the difference.

Everything here is typed by an operator. There is no research pipeline behind
it, no evidence rows, no confidence, and no provenance ledger, because there is
no external source to attribute: entering the value IS the authority for it
(KB-001). That is the whole reason the seller side and the prospect side are
structurally separate rather than sharing one "knowledge" abstraction.

The list-shaped fields (industries, geographies, capabilities, differentiators)
are JSONB arrays of trimmed strings rather than child tables. They are genuinely
lists of short labels with no attributes of their own, nothing joins to them,
and no rule keys off an individual entry; a child table would have bought
nothing but joins. They are still separate, named columns — the knowledge base
is not one JSON blob.

Only one profile row may exist. That is enforced by the database through a
partial unique index on ``is_current`` rather than by service code, so a second
row is impossible rather than merely unexpected.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SellerProfile(Base):
    """Operator-entered facts about the selling organisation."""

    __tablename__ = "seller_profiles"
    __table_args__ = (
        # Exactly one current profile. A partial unique index rather than a
        # single-row check, so the shape stays open to a superseded-history
        # model later without a rewrite.
        Index(
            "uq_seller_profiles_current",
            "is_current",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # The legal or trading name used when the system refers to us.
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # A one-or-two sentence description, for places with no room for the long one.
    short_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The standard, reusable description of the company.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How we position ourselves against the alternatives a buyer has.
    positioning: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How the system should and should not sound. This is guidance for future
    # drafting; it is not a prohibition list — prohibitions are restricted
    # claims, which are separate rows for exactly that reason.
    communication_guidance: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Anything that does not have a field of its own yet.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lists of short labels. ``None`` means nobody has filled this in; ``[]``
    # means an operator looked and said "none applies". The readiness rules
    # read those two differently and so should anything else.
    industries_served: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    geographies_served: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    capabilities: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    differentiators: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)

    updated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"SellerProfile(id={self.id!r}, name={self.name!r})"
