"""Where each ready person stands across their seven emails, and the manual acts.

The customer model is: ready person -> current email -> manual action ->
continue when the next email is due. This module is the authority on that
projection and on the three explicit acts behind it (Mark actioned, Skip
follow-up, Undo). It reads the immutable sequence tables and the append-only
``sequence_email_actions`` ledger, and it never sends anything.

Cadence rule (locked): package offsets are 0, 3, 7, 12, 18, 25, 35 days.
**Email 1 marked Actioned establishes Day 0.** Emails 2-7 are due relative to
that anchor, on whole local days, not relative to the previous action. Acting
late does not slide the future cadence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.campaign import CampaignContact
from app.models.email_action import SequenceEmailAction
from app.models.email_sequence import SEQUENCE_LENGTH, EmailSequence
from app.models.enums import EmailActionKind
from app.services.customer_status import CustomerContactStatus, status_for_membership
from app.services.sequences import read as sequence_read

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

STATE_READY = "ready"  # Email 1 before Day 0: ready to action, no date claimed
STATE_UPCOMING = "upcoming"
STATE_DUE = "due"
STATE_OVERDUE = "overdue"
STATE_ACTIONED = "actioned"
STATE_SKIPPED = "skipped"

STATE_LABELS: dict[str, str] = {
    STATE_READY: "Ready",
    STATE_UPCOMING: "Upcoming",
    STATE_DUE: "Due today",
    STATE_OVERDUE: "Overdue",
    STATE_ACTIONED: "Actioned",
    STATE_SKIPPED: "Skipped",
}


class EmailActionError(Exception):
    """A manual act that cannot be recorded, in the customer's words."""


def local_zone() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().app_timezone)
    except Exception:  # pragma: no cover - a bad setting must not take the desk down
        return ZoneInfo("UTC")


def local_today() -> date:
    return datetime.now(UTC).astimezone(local_zone()).date()


def _local_day(moment: datetime) -> date:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware.astimezone(local_zone()).date()


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailState:
    """One of the seven emails, as the desk shows it."""

    position: int
    message_id: uuid.UUID
    version_id: uuid.UUID
    day: int
    state: str
    due_on: date | None = None
    acted_at: datetime | None = None
    acted_by: str | None = None
    #: The action row that produced ``state`` (for Undo).
    action_id: uuid.UUID | None = None
    #: The action was recorded against an older text version.
    stale_version: bool = False

    @property
    def label(self) -> str:
        return STATE_LABELS[self.state]

    @property
    def done(self) -> bool:
        return self.state in (STATE_ACTIONED, STATE_SKIPPED)

    @property
    def actionable(self) -> bool:
        return not self.done


@dataclass(frozen=True)
class PersonProgress:
    """A ready person's whole seven-email position."""

    membership_id: uuid.UUID
    sequence_id: uuid.UUID
    sequence_key: uuid.UUID
    emails: tuple[EmailState, ...]
    day_zero: date | None
    last_action: EmailState | None = None
    today: date = field(default_factory=local_today, repr=False)

    @property
    def actioned_count(self) -> int:
        return sum(1 for email in self.emails if email.state == STATE_ACTIONED)

    @property
    def done_count(self) -> int:
        return sum(1 for email in self.emails if email.done)

    @property
    def complete(self) -> bool:
        return all(email.done for email in self.emails)

    @property
    def next_email(self) -> EmailState | None:
        """The email to work on next: the first not yet actioned or skipped."""

        return next((email for email in self.emails if not email.done), None)

    @property
    def next_due_on(self) -> date | None:
        nxt = self.next_email
        return nxt.due_on if nxt is not None else None

    @property
    def due_now(self) -> bool:
        """Something is actionable today: Email 1 before Day 0, or a due/overdue follow-up."""

        nxt = self.next_email
        return nxt is not None and nxt.state in (STATE_READY, STATE_DUE, STATE_OVERDUE)

    @property
    def follow_up_due(self) -> bool:
        """A follow-up (Email 2-7) is due today or overdue."""

        nxt = self.next_email
        return nxt is not None and nxt.state in (STATE_DUE, STATE_OVERDUE)

    @property
    def overdue(self) -> bool:
        nxt = self.next_email
        return nxt is not None and nxt.state == STATE_OVERDUE

    @property
    def progress_label(self) -> str:
        return f"{self.actioned_count} of {SEQUENCE_LENGTH} actioned"

    @property
    def next_label(self) -> str:
        nxt = self.next_email
        if nxt is None:
            return "All done"
        return f"Email {nxt.position}"

    @property
    def due_label(self) -> str:
        nxt = self.next_email
        if nxt is None:
            return "—"
        if nxt.state == STATE_READY:
            return "Ready"
        if nxt.state == STATE_DUE:
            return "Today"
        if nxt.due_on is None:
            return "—"
        delta = (nxt.due_on - self.today).days
        if delta < 0:
            days = -delta
            return f"{days} day{'s' if days != 1 else ''} overdue"
        if delta == 1:
            return "Tomorrow"
        return f"In {delta} days"

    def email(self, position: int) -> EmailState | None:
        return next((email for email in self.emails if email.position == position), None)


def _effective_actions(
    session: Session, membership_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[int, SequenceEmailAction]]:
    """Per membership and position, the standing act (latest not undone)."""

    if not membership_ids:
        return {}
    rows = session.scalars(
        select(SequenceEmailAction)
        .where(SequenceEmailAction.campaign_contact_id.in_(membership_ids))
        .order_by(SequenceEmailAction.occurred_at, SequenceEmailAction.id)
    ).all()
    undone: set[uuid.UUID] = {
        row.undoes_action_id
        for row in rows
        if row.kind is EmailActionKind.UNDONE and row.undoes_action_id is not None
    }
    standing: dict[uuid.UUID, dict[int, SequenceEmailAction]] = {}
    for row in rows:
        if row.kind is EmailActionKind.UNDONE or row.id in undone:
            continue
        standing.setdefault(row.campaign_contact_id, {})[row.position] = row
    return standing


def progress_for_sequence(
    session: Session,
    *,
    sequence: EmailSequence,
    standing: dict[int, SequenceEmailAction] | None = None,
    today: date | None = None,
) -> PersonProgress:
    """The projection for one live sequence."""

    today = today or local_today()
    if standing is None:
        standing = _effective_actions(session, [sequence.campaign_contact_id]).get(
            sequence.campaign_contact_id, {}
        )
    rows = sequence_read.message_rows(session, sequence=sequence)
    first = standing.get(1)
    day_zero = (
        _local_day(first.occurred_at)
        if first is not None and first.kind is EmailActionKind.ACTIONED
        else None
    )

    emails: list[EmailState] = []
    for row in rows:
        due_on = day_zero + timedelta(days=row.recommended_elapsed_day) if day_zero else None
        act = standing.get(row.position)
        if act is not None:
            emails.append(
                EmailState(
                    position=row.position,
                    message_id=row.message_id,
                    version_id=row.version_id,
                    day=row.recommended_elapsed_day,
                    state=(
                        STATE_ACTIONED if act.kind is EmailActionKind.ACTIONED else STATE_SKIPPED
                    ),
                    due_on=due_on,
                    acted_at=act.occurred_at,
                    acted_by=act.actor,
                    action_id=act.id,
                    stale_version=(
                        act.message_version_id is not None
                        and act.message_version_id != row.version_id
                    ),
                )
            )
            continue
        if due_on is None:
            state = STATE_READY if row.position == 1 else STATE_UPCOMING
        elif due_on > today:
            state = STATE_UPCOMING
        elif due_on == today:
            state = STATE_DUE
        else:
            state = STATE_OVERDUE
        emails.append(
            EmailState(
                position=row.position,
                message_id=row.message_id,
                version_id=row.version_id,
                day=row.recommended_elapsed_day,
                state=state,
                due_on=due_on,
            )
        )

    last = None
    latest = max(standing.values(), key=lambda act: (act.occurred_at, act.id), default=None)
    if latest is not None:
        last = next((email for email in emails if email.position == latest.position), None)

    return PersonProgress(
        membership_id=sequence.campaign_contact_id,
        sequence_id=sequence.id,
        sequence_key=sequence.sequence_key,
        emails=tuple(emails),
        day_zero=day_zero,
        last_action=last,
        today=today,
    )


def progress_for_memberships(
    session: Session, membership_ids: list[uuid.UUID]
) -> dict[uuid.UUID, PersonProgress]:
    """The projection for many ready people, in a bounded number of queries."""

    if not membership_ids:
        return {}
    sequences = session.scalars(
        select(EmailSequence).where(
            EmailSequence.campaign_contact_id.in_(membership_ids),
            EmailSequence.superseded_at.is_(None),
        )
    ).all()
    standing = _effective_actions(session, membership_ids)
    today = local_today()
    return {
        sequence.campaign_contact_id: progress_for_sequence(
            session,
            sequence=sequence,
            standing=standing.get(sequence.campaign_contact_id, {}),
            today=today,
        )
        for sequence in sequences
    }


# ---------------------------------------------------------------------------
# Manual acts
# ---------------------------------------------------------------------------


def _live_sequence(session: Session, membership_id: uuid.UUID) -> EmailSequence:
    sequence = sequence_read.sequence_for_membership(session, campaign_contact_id=membership_id)
    if sequence is None:
        raise EmailActionError("This person has no emails yet.")
    return sequence


def _require_ready(session: Session, membership_id: uuid.UUID) -> None:
    if status_for_membership(session, campaign_contact_id=membership_id) is not (
        CustomerContactStatus.READY_FOR_SENDING
    ):
        raise EmailActionError("This person is not Ready for Sending, so nothing can be actioned.")


def _record(
    session: Session,
    *,
    sequence: EmailSequence,
    email: EmailState,
    kind: EmailActionKind,
    actor: str,
    note: str | None,
    undoes: uuid.UUID | None = None,
) -> SequenceEmailAction:
    membership = session.get(CampaignContact, sequence.campaign_contact_id)
    if membership is None:  # pragma: no cover - the sequence's FK guarantees it
        raise EmailActionError("This person no longer exists.")
    row = SequenceEmailAction(
        campaign_contact_id=membership.id,
        campaign_id=membership.campaign_id,
        sequence_key=sequence.sequence_key,
        message_id=email.message_id,
        message_version_id=None if kind is EmailActionKind.UNDONE else email.version_id,
        position=email.position,
        kind=kind,
        undoes_action_id=undoes,
        actor=actor.strip(),
        note=(note or "").strip() or None,
    )
    session.add(row)
    session.flush()
    return row


def mark_actioned(
    session: Session,
    *,
    membership_id: uuid.UUID,
    position: int,
    actor: str,
    note: str | None = None,
) -> PersonProgress:
    """Record that the manual sending-related step for this email is done.

    Records person, Campaign, email position, exact message version, actor and
    time. On Email 1 this establishes Day 0. Never claims anything was sent.
    """

    _require_ready(session, membership_id)
    sequence = _live_sequence(session, membership_id)
    progress = progress_for_sequence(session, sequence=sequence)
    email = progress.email(position)
    if email is None:
        raise EmailActionError("That email does not exist.")
    if email.state == STATE_ACTIONED:
        raise EmailActionError(f"Email {position} is already marked actioned.")
    if email.state == STATE_SKIPPED:
        raise EmailActionError(f"Email {position} was skipped. Undo the skip first.")
    _record(
        session,
        sequence=sequence,
        email=email,
        kind=EmailActionKind.ACTIONED,
        actor=actor,
        note=note,
    )
    return progress_for_sequence(session, sequence=sequence)


def skip_follow_up(
    session: Session,
    *,
    membership_id: uuid.UUID,
    position: int,
    actor: str,
    note: str | None = None,
) -> PersonProgress:
    """Deliberately remove one follow-up (Emails 2-7) from the manual cycle."""

    if position == 1:
        raise EmailActionError("Email 1 cannot be skipped; it is the first email.")
    _require_ready(session, membership_id)
    sequence = _live_sequence(session, membership_id)
    progress = progress_for_sequence(session, sequence=sequence)
    email = progress.email(position)
    if email is None:
        raise EmailActionError("That email does not exist.")
    if email.done:
        raise EmailActionError(f"Email {position} is already {email.label.lower()}.")
    _record(
        session,
        sequence=sequence,
        email=email,
        kind=EmailActionKind.SKIPPED,
        actor=actor,
        note=note,
    )
    return progress_for_sequence(session, sequence=sequence)


def undo(
    session: Session,
    *,
    membership_id: uuid.UUID,
    position: int,
    actor: str,
    note: str | None = None,
) -> PersonProgress:
    """Reverse the standing Actioned/Skipped on one email. History is kept."""

    sequence = _live_sequence(session, membership_id)
    progress = progress_for_sequence(session, sequence=sequence)
    email = progress.email(position)
    if email is None:
        raise EmailActionError("That email does not exist.")
    if not email.done or email.action_id is None:
        raise EmailActionError(f"Email {position} has nothing to undo.")
    _record(
        session,
        sequence=sequence,
        email=email,
        kind=EmailActionKind.UNDONE,
        actor=actor,
        note=note,
        undoes=email.action_id,
    )
    return progress_for_sequence(session, sequence=sequence)


def history(session: Session, *, membership_id: uuid.UUID) -> list[SequenceEmailAction]:
    return list(
        session.scalars(
            select(SequenceEmailAction)
            .where(SequenceEmailAction.campaign_contact_id == membership_id)
            .order_by(SequenceEmailAction.occurred_at.desc(), SequenceEmailAction.id.desc())
        ).all()
    )


__all__ = [
    "STATE_ACTIONED",
    "STATE_DUE",
    "STATE_LABELS",
    "STATE_OVERDUE",
    "STATE_READY",
    "STATE_SKIPPED",
    "STATE_UPCOMING",
    "EmailActionError",
    "EmailState",
    "PersonProgress",
    "history",
    "local_today",
    "mark_actioned",
    "progress_for_memberships",
    "progress_for_sequence",
    "skip_follow_up",
    "undo",
]
