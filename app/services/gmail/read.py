"""What the operator surface may read about Gmail drafts.

A read model of its own, and a small one, for the same reason the sequence card
has one: a template must not be able to reach a token. Nothing here exposes a
grant's encrypted columns, and the only fields it carries are ones an operator
could read off their own Drafts folder anyway.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.email_sequence import EmailSequence
from app.models.enums import GmailDraftStatus
from app.models.gmail import GmailDraftRecord


@dataclass(frozen=True)
class DraftRow:
    """One message's Gmail draft state, keyed by exact message version."""

    message_version_id: uuid.UUID
    position: int
    status: GmailDraftStatus
    gmail_draft_id: str | None
    mailbox_address: str
    created_at: datetime
    updated_at: datetime

    @property
    def label(self) -> str:
        """What the operator is told, in the same vocabulary as the status.

        ``RESERVED`` deliberately does not read as "in progress": from the
        operator's side an attempt that never recorded an outcome is exactly as
        unresolved as an ambiguous one, and calling it anything softer would
        invite a second click.
        """

        if self.status is GmailDraftStatus.CREATED:
            return f"drafted in {self.mailbox_address}"
        if self.status is GmailDraftStatus.FAILED:
            return "not drafted — the last attempt was refused"
        return "not confirmed — check your Gmail Drafts folder"

    @property
    def tone(self) -> str:
        if self.status is GmailDraftStatus.CREATED:
            return "ok"
        if self.status is GmailDraftStatus.FAILED:
            return "err"
        return "warn"


def draft_rows(
    session: Session, *, sequence: EmailSequence, mailbox_account_subject: str
) -> dict[uuid.UUID, DraftRow]:
    """Gmail draft state for one sequence *in one mailbox*, keyed by version id.

    One statement for the whole sequence: "has this been drafted?" is a question
    about the set, and asking it seven times would issue seven queries for one
    page section. Keyed by version rather than by position, because an edit
    creates a new version and the answer for the new text is legitimately
    different from the answer for the text it replaced.

    Scoped to the caller's own mailbox account, and that is a privacy boundary
    rather than a filter. A sequence belongs to a Campaign Contact, not to an
    operator, so a query by ``sequence_key`` alone would show operator A the
    address of the mailbox operator B drafted into. It also answers the question
    the page is actually asking -- "has *this* mailbox got this draft?" -- which
    is the same question the idempotency key answers.
    """

    if not mailbox_account_subject:
        return {}
    rows = session.scalars(
        select(GmailDraftRecord).where(
            GmailDraftRecord.sequence_key == sequence.sequence_key,
            GmailDraftRecord.mailbox_account_subject == mailbox_account_subject,
        )
    ).all()
    return {
        row.message_version_id: DraftRow(
            message_version_id=row.message_version_id,
            position=row.position,
            status=row.status,
            gmail_draft_id=row.gmail_draft_id,
            mailbox_address=row.mailbox_address,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    }
