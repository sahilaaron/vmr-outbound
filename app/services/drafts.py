"""Draft review: the read model behind the Review queue, and the human decision.

The Personalization Agent already writes an immutable :class:`DraftVersion` for
every Contact it finishes. Nothing read those rows and nothing acted on them, so a
finished draft had no surface — the operator could see that a stage completed but
not the message it produced, and the ``DraftApproval`` table sat unused.

This module is that surface, and nothing more:

* **It never generates or edits a draft.** A draft is written by the Agent and is
  immutable by design; an edit would be a new version, and this module has no
  command that creates one.
* **It never sends.** Approval records a human decision against one exact version.
  The Sending Agent has no adapter, so an approved draft goes nowhere until one
  exists — which is why :func:`approve` says so in its audit reason rather than
  implying delivery.
* **Approval is per version, and an edit invalidates it.** The table's unique
  constraint is on ``draft_version_id``, so a decision is a single row that is
  flipped rather than a history of contradictions. Approving a *superseded*
  version is refused: the operator would be approving text that is no longer the
  current draft.
* **A discard is a decision, not a deletion.** It is stored as ``INVALIDATED``
  against the same version, so "you looked at this and said no" stays
  distinguishable from "nobody has looked yet".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.draft import DraftApproval, DraftVersion
from app.models.enums import ApprovalStatus
from app.services.audit import record_audit_event

#: The operator acting through the customer-facing UI. Matches the actor string
#: the rest of the web layer records.
OPERATOR_ACTOR = "operator"

MAX_REASON_LEN = 500


class DraftReviewError(RuntimeError):
    """A review decision was refused, with a reason the page can show."""


@dataclass(frozen=True)
class DraftRow:
    """One draft in the review queue.

    ``is_current`` is the honest answer to "is this the latest thing the Agent
    wrote for this person in this campaign" — a superseded version stays visible
    (it may already carry a decision) but cannot be approved.
    """

    draft_version_id: uuid.UUID
    contact_id: uuid.UUID
    campaign_id: uuid.UUID
    campaign_name: str
    contact_name: str
    contact_title: str | None
    company_name: str | None
    email: str | None
    subject: str
    body: str
    rationale: str | None
    version_number: int
    created_at: datetime
    created_by: str | None
    is_current: bool
    decision: ApprovalStatus | None
    decided_at: datetime | None
    decided_by: str | None
    decision_reason: str | None
    campaign_contact_id: uuid.UUID | None

    @property
    def awaiting_decision(self) -> bool:
        return self.decision is None

    @property
    def approved(self) -> bool:
        return self.decision is ApprovalStatus.APPROVED

    @property
    def discarded(self) -> bool:
        return self.decision is ApprovalStatus.INVALIDATED

    @property
    def word_count(self) -> int:
        return len(self.body.split())

    @property
    def preview(self) -> str:
        first = next((line for line in self.body.splitlines() if line.strip()), "")
        return first[:160]


@dataclass(frozen=True)
class QueueCounts:
    """What the queue holds, by decision state."""

    awaiting: int = 0
    approved: int = 0
    discarded: int = 0

    @property
    def total(self) -> int:
        return self.awaiting + self.approved + self.discarded

    @property
    def decided(self) -> int:
        return self.approved + self.discarded

    @property
    def progress_percent(self) -> int:
        if self.total <= 0:
            return 0
        return max(0, min(100, round(self.decided * 100 / self.total)))


@dataclass(frozen=True)
class ReviewQueue:
    """A page of the queue plus the counts behind its filters."""

    rows: tuple[DraftRow, ...] = ()
    counts: QueueCounts = field(default_factory=QueueCounts)
    total: int = 0
    campaign_id: uuid.UUID | None = None
    view: str = "awaiting"


VIEW_AWAITING = "awaiting"
VIEW_APPROVED = "approved"
VIEW_DISCARDED = "discarded"
VIEW_ALL = "all"

VIEWS: tuple[tuple[str, str], ...] = (
    (VIEW_AWAITING, "Waiting for you"),
    (VIEW_APPROVED, "Approved"),
    (VIEW_DISCARDED, "Discarded"),
    (VIEW_ALL, "All"),
)


def _base_statement() -> Select[tuple[DraftVersion, Contact, Campaign, DraftApproval]]:
    """Every draft with its person, its campaign, and its decision if one exists.

    The outer join means the ``DraftApproval`` column is ``None`` for a draft nobody
    has decided on. SQLAlchemy types the column by the entity, not by the join, so
    the row is unpacked as optional at every call site — which is the distinction
    that matters here: no approval row is "nobody has looked yet", and that is a
    different answer from any stored status.
    """

    return (
        select(DraftVersion, Contact, Campaign, DraftApproval)
        .join(Contact, Contact.id == DraftVersion.contact_id)
        .join(Campaign, Campaign.id == DraftVersion.campaign_id)
        .outerjoin(DraftApproval, DraftApproval.draft_version_id == DraftVersion.id)
    )


def _current_version_numbers(
    session: Session, *, campaign_id: uuid.UUID | None
) -> dict[tuple[uuid.UUID, uuid.UUID], int]:
    """Highest version number per (contact, campaign).

    Computed once for the page rather than per row: "is this the current draft" is
    a question about the set, and asking it row by row would issue a query per
    draft.
    """

    statement = select(
        DraftVersion.contact_id,
        DraftVersion.campaign_id,
        func.max(DraftVersion.version_number),
    ).group_by(DraftVersion.contact_id, DraftVersion.campaign_id)
    if campaign_id is not None:
        statement = statement.where(DraftVersion.campaign_id == campaign_id)
    return {
        (contact_id, campaign): int(highest)
        for contact_id, campaign, highest in session.execute(statement).all()
    }


def _membership_ids(
    session: Session, *, pairs: set[tuple[uuid.UUID, uuid.UUID]]
) -> dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID]:
    """Map (contact, campaign) to the membership that carries pipeline state.

    A draft references the permanent Contact and the Campaign, not the membership,
    so the link to execution state has to be looked up. A draft whose membership
    was archived away simply has none, and the page says so.
    """

    if not pairs:
        return {}
    contact_ids = {contact_id for contact_id, _ in pairs}
    rows = session.execute(
        select(CampaignContact.contact_id, CampaignContact.campaign_id, CampaignContact.id).where(
            CampaignContact.contact_id.in_(contact_ids)
        )
    ).all()
    found = {
        (contact_id, campaign_id): membership_id for contact_id, campaign_id, membership_id in rows
    }
    return {pair: found[pair] for pair in pairs if pair in found}


def _row(
    draft: DraftVersion,
    contact: Contact,
    campaign: Campaign,
    approval: DraftApproval | None,
    *,
    current: dict[tuple[uuid.UUID, uuid.UUID], int],
    memberships: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID],
) -> DraftRow:
    key = (draft.contact_id, draft.campaign_id)
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part).strip()
    return DraftRow(
        draft_version_id=draft.id,
        contact_id=draft.contact_id,
        campaign_id=draft.campaign_id,
        campaign_name=campaign.name,
        contact_name=name or "Unnamed contact",
        contact_title=contact.title,
        company_name=contact.company_name,
        email=contact.email,
        subject=draft.subject,
        body=draft.body,
        rationale=draft.rationale,
        version_number=draft.version_number,
        created_at=draft.created_at,
        created_by=draft.created_by,
        is_current=current.get(key) == draft.version_number,
        decision=approval.status if approval is not None else None,
        decided_at=approval.approved_at if approval is not None else None,
        decided_by=approval.approved_by if approval is not None else None,
        decision_reason=approval.reason if approval is not None else None,
        campaign_contact_id=memberships.get(key),
    )


def queue_counts(session: Session, *, campaign_id: uuid.UUID | None = None) -> QueueCounts:
    """Counts by decision state, for the filter chips and the progress bar."""

    statement = (
        select(DraftApproval.status, func.count(DraftVersion.id))
        .select_from(DraftVersion)
        .outerjoin(DraftApproval, DraftApproval.draft_version_id == DraftVersion.id)
    )
    if campaign_id is not None:
        statement = statement.where(DraftVersion.campaign_id == campaign_id)
    counts = {
        status: int(count)
        for status, count in session.execute(statement.group_by(DraftApproval.status)).all()
    }
    return QueueCounts(
        awaiting=counts.get(None, 0),
        approved=counts.get(ApprovalStatus.APPROVED, 0),
        discarded=counts.get(ApprovalStatus.INVALIDATED, 0),
    )


def list_queue(
    session: Session,
    *,
    campaign_id: uuid.UUID | None = None,
    view: str = VIEW_AWAITING,
    limit: int = 50,
    offset: int = 0,
) -> ReviewQueue:
    """A page of the review queue, newest draft first."""

    if view not in {name for name, _ in VIEWS}:
        view = VIEW_AWAITING

    statement = _base_statement()
    count_statement = (
        select(func.count(DraftVersion.id))
        .select_from(DraftVersion)
        .outerjoin(DraftApproval, DraftApproval.draft_version_id == DraftVersion.id)
    )
    if campaign_id is not None:
        statement = statement.where(DraftVersion.campaign_id == campaign_id)
        count_statement = count_statement.where(DraftVersion.campaign_id == campaign_id)
    if view == VIEW_AWAITING:
        statement = statement.where(DraftApproval.id.is_(None))
        count_statement = count_statement.where(DraftApproval.id.is_(None))
    elif view == VIEW_APPROVED:
        statement = statement.where(DraftApproval.status == ApprovalStatus.APPROVED)
        count_statement = count_statement.where(DraftApproval.status == ApprovalStatus.APPROVED)
    elif view == VIEW_DISCARDED:
        statement = statement.where(DraftApproval.status == ApprovalStatus.INVALIDATED)
        count_statement = count_statement.where(DraftApproval.status == ApprovalStatus.INVALIDATED)

    total = int(session.scalar(count_statement) or 0)
    records = session.execute(
        statement.order_by(DraftVersion.created_at.desc(), DraftVersion.version_number.desc())
        .limit(max(0, limit))
        .offset(max(0, offset))
    ).all()

    current = _current_version_numbers(session, campaign_id=campaign_id)
    pairs = {(draft.contact_id, draft.campaign_id) for draft, _, _, _ in records}
    memberships = _membership_ids(session, pairs=pairs)
    rows = tuple(
        _row(draft, contact, campaign, approval, current=current, memberships=memberships)
        for draft, contact, campaign, approval in records
    )
    return ReviewQueue(
        rows=rows,
        counts=queue_counts(session, campaign_id=campaign_id),
        total=total,
        campaign_id=campaign_id,
        view=view,
    )


def get_draft(session: Session, draft_version_id: uuid.UUID) -> DraftRow | None:
    """One draft, with its decision state and its link to execution."""

    record = session.execute(_base_statement().where(DraftVersion.id == draft_version_id)).first()
    if record is None:
        return None
    draft, contact, campaign, approval = record
    current = _current_version_numbers(session, campaign_id=draft.campaign_id)
    memberships = _membership_ids(session, pairs={(draft.contact_id, draft.campaign_id)})
    return _row(draft, contact, campaign, approval, current=current, memberships=memberships)


def first_awaiting(session: Session, *, campaign_id: uuid.UUID | None = None) -> DraftRow | None:
    """The draft the queue opens on."""

    page = list_queue(session, campaign_id=campaign_id, view=VIEW_AWAITING, limit=1)
    if page.rows:
        return page.rows[0]
    page = list_queue(session, campaign_id=campaign_id, view=VIEW_ALL, limit=1)
    return page.rows[0] if page.rows else None


def _clean_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    text = reason.strip()
    if not text:
        return None
    return text[:MAX_REASON_LEN]


def _decide(
    session: Session,
    *,
    draft_version_id: uuid.UUID,
    status: ApprovalStatus,
    actor: str,
    reason: str | None,
    action: str,
    audit_note: str,
) -> DraftApproval:
    draft = session.get(DraftVersion, draft_version_id)
    if draft is None:
        raise DraftReviewError("That draft no longer exists.")

    highest = session.scalar(
        select(func.max(DraftVersion.version_number)).where(
            DraftVersion.contact_id == draft.contact_id,
            DraftVersion.campaign_id == draft.campaign_id,
        )
    )
    if highest is not None and int(highest) != draft.version_number:
        raise DraftReviewError(
            f"Version {draft.version_number} has been superseded by version {int(highest)}. "
            "Decide on the current draft instead — approving text the Agent has already "
            "rewritten would approve something nobody is going to send."
        )

    approval = session.scalar(
        select(DraftApproval).where(DraftApproval.draft_version_id == draft.id)
    )
    previous = approval.status.value if approval is not None else None
    now = datetime.now(UTC)
    cleaned = _clean_reason(reason)

    if approval is None:
        approval = DraftApproval(
            draft_version_id=draft.id,
            approved_by=actor,
            approved_at=now,
            status=status,
            reason=cleaned,
        )
        session.add(approval)
    else:
        approval.status = status
        approval.approved_by = actor
        approval.approved_at = now
        approval.reason = cleaned

    record_audit_event(
        session,
        actor=actor,
        action=action,
        entity_type="draft_version",
        entity_id=str(draft.id),
        previous_state=previous,
        new_state=status.value,
        reason=cleaned,
        context={
            "contact_id": str(draft.contact_id),
            "campaign_id": str(draft.campaign_id),
            "version_number": draft.version_number,
            "note": audit_note,
        },
    )
    session.flush()
    return approval


def approve(
    session: Session,
    *,
    draft_version_id: uuid.UUID,
    actor: str = OPERATOR_ACTOR,
    reason: str | None = None,
) -> DraftApproval:
    """Record that a human approved this exact version.

    Approval is the human authorisation the pipeline requires before outreach. It
    does not schedule, queue or send anything: the Sending Agent has no adapter, so
    an approved draft waits for one. The audit note says that explicitly, because a
    record that reads as "sent" when nothing was sent is worse than no record.
    """

    return _decide(
        session,
        draft_version_id=draft_version_id,
        status=ApprovalStatus.APPROVED,
        actor=actor,
        reason=reason,
        action="draft.approve",
        audit_note=(
            "Human approval recorded against an immutable draft version. No message was "
            "sent: no Sending adapter is registered."
        ),
    )


def discard(
    session: Session,
    *,
    draft_version_id: uuid.UUID,
    actor: str = OPERATOR_ACTOR,
    reason: str | None = None,
) -> DraftApproval:
    """Record that a human read this version and declined it."""

    return _decide(
        session,
        draft_version_id=draft_version_id,
        status=ApprovalStatus.INVALIDATED,
        actor=actor,
        reason=reason,
        action="draft.discard",
        audit_note="Human declined this draft version. The version itself is kept.",
    )
