"""Contact-first capture models (DAT-013).

The Chrome extension is the contact-acquisition edge of the system. A single
operator-reviewed *submission* carries one or more captured people; each person
is persisted as an immutable capture (a
:class:`~app.models.linkedin_profile.LinkedInProfileSnapshot`) and reconciled by
the backend. Nothing here belongs to a campaign: a campaign consumes a saved
audience much later in the workflow.

Four tables complete that picture:

* :class:`ContactCaptureSubmission` — one row per accepted submission. It is the
  idempotency anchor (``client_submission_id``), stores the operator's
  submission-level labels and note verbatim, and keeps the exact response body
  so a retry replays the original truthful outcome instead of recomputing one.
* :class:`~app.models.collection.Collection` — the canonical backend-owned
  Collection registry. The extension calls these Labels.
* :class:`~app.models.collection.CollectionMembership` — a Collection applied
  to a permanent contact or pending capture.
* :class:`ContactCaptureNote` — append-only operator notes. A refresh never
  rewrites an earlier note; it appends another row.

None of these tables can make a contact outreach-eligible. They classify and
annotate; eligibility, verification, approval, and scheduling live elsewhere and
are unaffected by anything recorded here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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

# Note scopes. A submission-level note is recorded against every capture in the
# submission so a capture is always self-contained evidence; the scope records
# where the operator actually typed it.
NOTE_SCOPE_SUBMISSION = "submission"
NOTE_SCOPE_CONTACT = "contact"


class ContactCaptureSubmission(Base):
    """One operator-reviewed contact-capture submission (one or many people)."""

    __tablename__ = "contact_capture_submissions"
    __table_args__ = (
        UniqueConstraint(
            "client_submission_id", name="uq_contact_capture_submissions_client_submission_id"
        ),
        Index("ix_contact_capture_submissions_received_at", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # --- Idempotency / integrity ---------------------------------------------
    client_submission_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- Contract provenance --------------------------------------------------
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    # "linkedin_profile" | "salesnav_people_search" — which reviewed workflow.
    capture_mode: Mapped[str] = mapped_column(String(48), nullable=False)
    extension_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    contact_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Operator metadata (verbatim; label resolution happens separately) ----
    requested_labels: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Truthful outcome -----------------------------------------------------
    # The exact response body returned when the submission was accepted. A retry
    # of the same client_submission_id with identical content replays this.
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<ContactCaptureSubmission id={self.id} mode={self.capture_mode!r} "
            f"contacts={self.contact_count}>"
        )


class ContactCaptureNote(Base):
    """An append-only operator note attached to a capture, a contact, or both.

    Notes are never updated or deleted: refreshing a contact appends a new note
    row and leaves every earlier note intact, and a correction is another note
    rather than an edit.

    A note written by the capture path is anchored to the capture it came from.
    A note written by an operator in the contact CRM is anchored to the contact,
    which may have been created by a spreadsheet import and have no capture at
    all (APP-002). At least one anchor is always present.
    """

    __tablename__ = "contact_capture_notes"
    __table_args__ = (
        CheckConstraint(
            "capture_id IS NOT NULL OR contact_id IS NOT NULL",
            name="ck_contact_capture_notes_anchor",
        ),
        Index("ix_contact_capture_notes_capture_id", "capture_id"),
        Index("ix_contact_capture_notes_contact_id", "contact_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    capture_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("linkedin_profile_snapshots.id", ondelete="CASCADE"),
        nullable=True,
    )
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contact_capture_submissions.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Set only when the capture matched exactly one existing contact.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    note_text: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ContactCaptureNote capture={self.capture_id} scope={self.scope!r}>"


# Compatibility import names for existing capture, CRM, and extension callers.
# New backend code should import Collection/CollectionMembership directly.
from app.models.collection import Collection as ContactLabel  # noqa: E402,F401
from app.models.collection import (  # noqa: E402,F401
    CollectionMembership as ContactLabelAssignment,
)

__all__ = [
    "ContactCaptureNote",
    "ContactCaptureSubmission",
    "ContactLabel",
    "ContactLabelAssignment",
    "NOTE_SCOPE_CONTACT",
    "NOTE_SCOPE_SUBMISSION",
]
