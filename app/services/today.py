"""Today: what is due now, and where to continue.

The return surface. It brings due follow-ups and ready first emails back to
the relevant Campaign; it is not a notification centre and it counts no
machine backlog. Everything here is derived from the same projections the
Campaign pages use (``customer_status``, ``email_progress``), so the numbers
reconcile.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.email_action import TodayDismissal
from app.models.enums import CampaignStatus
from app.services import campaign_workspace, customer_status, email_progress
from app.services.seller import campaign_offerings as seller_campaign_offerings


@dataclass(frozen=True)
class DueCard:
    """One Campaign's due follow-ups, grouped rather than one card per person."""

    campaign: Campaign
    due: int
    overdue: int
    next_position: int
    first_membership_id: uuid.UUID | None

    @property
    def open_url(self) -> str:
        base = f"/app/campaigns/{self.campaign.id}"
        if self.first_membership_id is None:
            return f"{base}#ready"
        return f"{base}?person={self.first_membership_id}#ready"


@dataclass(frozen=True)
class FirstEmailCard:
    campaign: Campaign
    ready: int
    first_membership_id: uuid.UUID | None

    @property
    def open_url(self) -> str:
        base = f"/app/campaigns/{self.campaign.id}"
        if self.first_membership_id is None:
            return f"{base}#ready"
        return f"{base}?section=first&person={self.first_membership_id}#ready"


@dataclass(frozen=True)
class MotionRow:
    campaign: Campaign
    lifecycle: str
    progress: customer_status.CustomerProgress
    last_change: object

    @property
    def lifecycle_label(self) -> str:
        return campaign_workspace.LIFECYCLE_LABELS[self.lifecycle]


@dataclass(frozen=True)
class SetupNeed:
    campaign: Campaign
    text: str
    href: str


@dataclass(frozen=True)
class TodayView:
    due: list[DueCard] = field(default_factory=list)
    first: list[FirstEmailCard] = field(default_factory=list)
    motion: list[MotionRow] = field(default_factory=list)
    needs: list[SetupNeed] = field(default_factory=list)
    dismissed: int = 0
    total_people: int = 0
    total_ready: int = 0
    total_processing: int = 0

    @property
    def quiet(self) -> bool:
        return not self.due and not self.first


def dismissed_campaign_ids(
    session: Session, *, user_id: uuid.UUID | None, day: date
) -> set[uuid.UUID]:
    if user_id is None:
        return set()
    rows = session.scalars(
        select(TodayDismissal.campaign_id).where(
            TodayDismissal.user_id == user_id, TodayDismissal.local_day == day
        )
    ).all()
    return set(rows)


def dismiss(session: Session, *, user_id: uuid.UUID, campaign_id: uuid.UUID, day: date) -> None:
    """Hide one Campaign's due card for this user until the next local day."""

    existing = session.scalars(
        select(TodayDismissal).where(
            TodayDismissal.user_id == user_id,
            TodayDismissal.campaign_id == campaign_id,
            TodayDismissal.local_day == day,
        )
    ).first()
    if existing is None:
        session.add(TodayDismissal(user_id=user_id, campaign_id=campaign_id, local_day=day))
        session.flush()


def build(
    session: Session,
    *,
    campaigns: list[Campaign],
    user_id: uuid.UUID | None,
    kb_on: bool,
) -> TodayView:
    """Everything the Today page shows, from the campaigns this user may see."""

    day = email_progress.local_today()
    hidden = dismissed_campaign_ids(session, user_id=user_id, day=day)
    due_cards: list[DueCard] = []
    first_cards: list[FirstEmailCard] = []
    motion: list[MotionRow] = []
    needs: list[SetupNeed] = []
    dismissed = 0
    total_people = total_ready = total_processing = 0
    for campaign in campaigns:
        lifecycle = campaign_workspace.lifecycle(campaign)
        if campaign.status is CampaignStatus.ARCHIVED:
            continue
        progress = customer_status.progress(session, campaign_id=campaign.id)
        rows = campaign_workspace.list_rows(session, [campaign])
        motion.append(
            MotionRow(
                campaign=campaign,
                lifecycle=lifecycle,
                progress=progress,
                last_change=rows[0].last_change if rows else None,
            )
        )
        total_people += progress.total
        total_ready += progress.ready_for_sending
        total_processing += progress.processing

        if progress.total == 0 and lifecycle in ("active", "draft"):
            needs.append(
                SetupNeed(
                    campaign,
                    "No people added yet.",
                    f"/app/campaigns/{campaign.id}/add-people",
                )
            )
        if kb_on and not seller_campaign_offerings.offerings_for_campaign(session, campaign.id):
            needs.append(
                SetupNeed(
                    campaign,
                    "No offering chosen — the emails cannot lean on what you sell.",
                    f"/app/campaigns/{campaign.id}/setup",
                )
            )

        if progress.ready_for_sending == 0:
            continue
        ready = campaign_workspace.ready_people(session, campaign_id=campaign.id, limit=500)
        prog = email_progress.progress_for_memberships(
            session, [row.membership_id for row in ready]
        )
        due_ids: list[uuid.UUID] = []
        overdue = 0
        positions: Counter[int] = Counter()
        first_ids: list[uuid.UUID] = []
        for row in ready:
            p = prog.get(row.membership_id)
            if p is None or p.next_email is None:
                continue
            if p.follow_up_due:
                due_ids.append(row.membership_id)
                positions[p.next_email.position] += 1
                if p.overdue:
                    overdue += 1
            elif p.next_email.position == 1:
                first_ids.append(row.membership_id)
        if due_ids:
            if campaign.id in hidden:
                dismissed += 1
            else:
                due_cards.append(
                    DueCard(
                        campaign=campaign,
                        due=len(due_ids),
                        overdue=overdue,
                        next_position=positions.most_common(1)[0][0],
                        first_membership_id=due_ids[0],
                    )
                )
        if first_ids:
            first_cards.append(
                FirstEmailCard(
                    campaign=campaign, ready=len(first_ids), first_membership_id=first_ids[0]
                )
            )
    return TodayView(
        due=due_cards,
        first=first_cards,
        motion=motion,
        needs=needs,
        dismissed=dismissed,
        total_people=total_people,
        total_ready=total_ready,
        total_processing=total_processing,
    )


__all__ = ["DueCard", "FirstEmailCard", "MotionRow", "SetupNeed", "TodayView", "build", "dismiss"]
