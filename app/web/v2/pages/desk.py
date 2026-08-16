"""The inline sending desk: what to do with this person's current email.

An expanded region of Campaign Overview, never a page of its own. Vertical
movement is people, horizontal movement is the seven emails. Actions here are
explicit: Copy, Create one Gmail draft, Mark actioned, Edit, Skip follow-up,
Undo. Nothing in VMR claims an email was sent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.accounts import session_account_id
from app.core.auth.context import current_operator
from app.core.config import get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.email_sequence import EmailSequenceMessageVersion
from app.models.insight import Insight
from app.services import campaign_workspace, email_progress
from app.services.gmail import drafts as gmail_drafts
from app.services.gmail import mailbox as gmail_mailbox
from app.services.gmail import provider as gmail_provider
from app.services.sequences import read as sequence_read
from app.services.sequences import review as sequence_review
from app.web.v2 import shell
from app.web.v2.pages.emails import GMAIL_PROVIDER_STATE_KEY, gmail_draft_rows

router = shell.router

#: The Ready for Sending subfilters. "Due now" is the default: Email 1 before
#: Day 0, or a follow-up whose day has arrived.
READY_FILTERS: tuple[tuple[str, str], ...] = (
    ("due", "Due now"),
    ("all", "All ready"),
    ("first", "First email"),
    ("followups", "Follow-ups"),
    ("done", "Actioned"),
)
DEFAULT_FILTER = "due"


def desk_url(
    campaign_id: uuid.UUID,
    membership_id: uuid.UUID | None,
    *,
    email: int | None = None,
    section: str | None = None,
) -> str:
    params: dict[str, str] = {}
    if section and section != DEFAULT_FILTER:
        params["section"] = section
    if membership_id is not None:
        params["person"] = str(membership_id)
        if email:
            params["email"] = str(email)
    query = f"?{urlencode(params)}" if params else ""
    return f"/app/campaigns/{campaign_id}{query}#ready"


# ---------------------------------------------------------------------------
# Projection for the Overview
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadyRow:
    person: campaign_workspace.PersonRow
    progress: email_progress.PersonProgress | None

    @property
    def next_label(self) -> str:
        return self.progress.next_label if self.progress else "Email 1"

    @property
    def due_label(self) -> str:
        return self.progress.due_label if self.progress else "Ready"

    @property
    def progress_label(self) -> str:
        return self.progress.progress_label if self.progress else "0 of 7 actioned"

    @property
    def matches(self) -> dict[str, bool]:
        p = self.progress
        due_now = p.due_now if p else True
        first = (p.next_email is not None and p.next_email.position == 1) if p else True
        followup = p.follow_up_due if p else False
        done = p.complete if p else False
        return {"due": due_now, "all": True, "first": first, "followups": followup, "done": done}


def ready_rows(
    db: Session, *, campaign_id: uuid.UUID, limit: int
) -> tuple[list[ReadyRow], dict[uuid.UUID, email_progress.PersonProgress]]:
    people = campaign_workspace.ready_people(db, campaign_id=campaign_id, limit=limit)
    progress = email_progress.progress_for_memberships(db, [row.membership_id for row in people])
    rows = [ReadyRow(person=row, progress=progress.get(row.membership_id)) for row in people]
    # Due first, then by next due date, then name — the order the desk walks.
    rows.sort(
        key=lambda r: (
            0 if (r.progress is None or r.progress.due_now) else 1,
            r.progress.next_due_on.toordinal() if r.progress and r.progress.next_due_on else 0,
            r.person.name.lower(),
        )
    )
    return rows, progress


def filter_rows(rows: list[ReadyRow], section: str) -> list[ReadyRow]:
    key = section if section in {k for k, _ in READY_FILTERS} else DEFAULT_FILTER
    return [row for row in rows if row.matches.get(key, True)]


@dataclass(frozen=True)
class WhyItem:
    label: str
    text: str
    detail: str | None = None


def _why(db: Session, detail: sequence_read.MessageDetail) -> list[WhyItem]:
    """Why this email says what it says — the decision and its evidence, no ids."""

    items: list[WhyItem] = []
    row = detail.row
    items.append(
        WhyItem(
            "Angle",
            row.purpose_label,
            f"Email {row.position} · Day {row.recommended_elapsed_day}",
        )
    )
    used = (
        detail.context_used.get("context_used") if isinstance(detail.context_used, dict) else None
    )
    labels = [str(item) for item in used][:3] if isinstance(used, list) else []
    claims: list[str] = []
    ids: list[uuid.UUID] = []
    for raw in detail.evidence_insight_ids or ():
        parsed = shell.uuid_or_none(str(raw))
        if parsed is not None:
            ids.append(parsed)
    if ids:
        for insight in db.scalars(select(Insight).where(Insight.id.in_(ids))).all():
            claims.append(insight.claim)
    basis = claims[:3] or labels
    items.append(
        WhyItem(
            "Based on",
            "; ".join(basis)
            if basis
            else "No specific fact was cited; the email leans on the offering and role.",
        )
    )
    decision = detail.context_decision or {}
    block = decision.get("company_intelligence") if isinstance(decision, dict) else None
    company_used = bool(isinstance(block, dict) and block.get("used"))
    items.append(
        WhyItem(
            "Company context",
            "Company knowledge shaped the tone."
            if company_used
            else "No company classification was used.",
        )
    )
    research_used = decision.get("context_used") if isinstance(decision, dict) else None
    items.append(
        WhyItem(
            "Research",
            ", ".join(str(item) for item in research_used)
            if isinstance(research_used, list) and research_used
            else "No usable research was available.",
        )
    )
    warnings = list(detail.warnings or ())
    items.append(
        WhyItem(
            "Validation",
            "Passed"
            if not warnings
            else f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}",
            "; ".join(warnings) if warnings else None,
        )
    )
    return items


def build_desk(
    request: Request,
    db: Session,
    *,
    campaign: Campaign,
    rows: list[ReadyRow],
    progress: dict[uuid.UUID, email_progress.PersonProgress],
    person: str | None,
    email: str | None,
    section: str,
) -> dict[str, Any] | None:
    """Everything the inline workbook renders for the selected person, or None."""

    membership_id = shell.uuid_or_none(person) if person else None
    if membership_id is None:
        return None
    index = next(
        (i for i, row in enumerate(rows) if row.person.membership_id == membership_id), None
    )
    if index is None:
        return None
    row = rows[index]
    prog = progress.get(membership_id)
    if prog is None:
        return None
    sequence = sequence_read.get_sequence(db, prog.sequence_id)
    if sequence is None:
        return None

    try:
        wanted = int(email) if email else 0
    except ValueError:
        wanted = 0
    if not 1 <= wanted <= 7:
        nxt = prog.next_email
        wanted = nxt.position if nxt is not None else 1
    state = prog.email(wanted)
    detail = sequence_read.message_detail(db, sequence=sequence, position=wanted)
    settings = get_settings()
    drafts = gmail_draft_rows(db, settings, sequence=sequence)
    draft = drafts.get(detail.row.version_id) if detail is not None else None
    stale_draft = None
    if detail is not None and draft is None:
        # A draft may exist for an earlier version of this message.
        for version_id, candidate in drafts.items():
            if candidate.position == wanted and version_id != detail.row.version_id:
                stale_draft = candidate
                break

    prev_row = rows[index - 1] if index > 0 else None
    next_row = rows[index + 1] if index + 1 < len(rows) else None
    return {
        "row": row,
        "progress": prog,
        "sequence": sequence,
        "index": index + 1,
        "count": len(rows),
        "email": state,
        "detail": detail,
        "why": _why(db, detail) if detail is not None else [],
        "draft": draft,
        "stale_draft": stale_draft,
        "mailbox": shell.mailbox_state(db, settings),
        "gmail_on": shell.gmail_drafts_on(db, settings),
        "prev_url": desk_url(campaign.id, prev_row.person.membership_id, section=section)
        if prev_row
        else None,
        "next_url": desk_url(campaign.id, next_row.person.membership_id, section=section)
        if next_row
        else None,
        "close_url": desk_url(campaign.id, None, section=section),
        "email_url": lambda position: desk_url(
            campaign.id, membership_id, email=position, section=section
        ),
        "self_url": desk_url(campaign.id, membership_id, email=wanted, section=section),
        "history": email_progress.history(db, membership_id=membership_id),
        "versions": _versions(db, detail) if detail is not None else [],
    }


def _versions(
    db: Session, detail: sequence_read.MessageDetail
) -> list[EmailSequenceMessageVersion]:
    return list(
        db.scalars(
            select(EmailSequenceMessageVersion)
            .where(EmailSequenceMessageVersion.message_id == detail.row.message_id)
            .order_by(EmailSequenceMessageVersion.message_version.desc())
        ).all()
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _actor() -> str:
    operator = current_operator()
    return operator.email if operator is not None else "operator"


def _membership(db: Session, campaign_id: uuid.UUID, membership_id: str) -> CampaignContact | None:
    identifier = shell.uuid_or_none(membership_id)
    if identifier is None:
        return None
    membership = db.get(CampaignContact, identifier)
    if membership is None or membership.campaign_id != campaign_id:
        return None
    return membership


def _position(value: str) -> int | None:
    try:
        position = int(value)
    except ValueError:
        return None
    return position if 1 <= position <= 7 else None


@router.post("/campaigns/{campaign_id}/desk/{membership_id}/{position}/actioned")
def desk_mark_actioned(
    campaign_id: str,
    membership_id: str,
    position: str,
    request: Request,
    db: Session = Depends(get_db),
    section: str = Form(DEFAULT_FILTER),
) -> RedirectResponse:
    """Record that the manual sending-related step for this email is done."""

    identifier = shell.uuid_or_none(campaign_id)
    membership = _membership(db, identifier, membership_id) if identifier else None
    pos = _position(position)
    if identifier is None or membership is None or pos is None:
        return shell.redirect(f"/app/campaigns/{campaign_id}", err="That email could not be found.")
    back = desk_url(identifier, membership.id, email=pos, section=section)
    try:
        progress = email_progress.mark_actioned(
            db, membership_id=membership.id, position=pos, actor=_actor()
        )
    except email_progress.EmailActionError as exc:
        db.rollback()
        return shell.redirect(back, err=str(exc))
    db.commit()
    nxt = progress.next_email
    if pos == 1:
        message = "Email 1 marked actioned. Day 0 is today; follow-ups are due from here."
    elif progress.complete:
        message = f"Email {pos} marked actioned. All seven emails are done for this person."
    else:
        message = f"Email {pos} marked actioned."
    target = desk_url(
        identifier, membership.id, email=nxt.position if nxt else pos, section=section
    )
    return shell.redirect(target, ok=message)


@router.post("/campaigns/{campaign_id}/desk/{membership_id}/{position}/skip")
def desk_skip(
    campaign_id: str,
    membership_id: str,
    position: str,
    request: Request,
    db: Session = Depends(get_db),
    section: str = Form(DEFAULT_FILTER),
    confirm: str = Form(""),
) -> RedirectResponse:
    identifier = shell.uuid_or_none(campaign_id)
    membership = _membership(db, identifier, membership_id) if identifier else None
    pos = _position(position)
    if identifier is None or membership is None or pos is None:
        return shell.redirect(f"/app/campaigns/{campaign_id}", err="That email could not be found.")
    back = desk_url(identifier, membership.id, email=pos, section=section)
    if not shell.checkbox(confirm):
        return shell.redirect(back, err="Tick the confirmation to skip this follow-up.")
    try:
        progress = email_progress.skip_follow_up(
            db, membership_id=membership.id, position=pos, actor=_actor()
        )
    except email_progress.EmailActionError as exc:
        db.rollback()
        return shell.redirect(back, err=str(exc))
    db.commit()
    nxt = progress.next_email
    target = desk_url(
        identifier, membership.id, email=nxt.position if nxt else pos, section=section
    )
    return shell.redirect(target, ok=f"Email {pos} skipped for this person.")


@router.post("/campaigns/{campaign_id}/desk/{membership_id}/{position}/undo")
def desk_undo(
    campaign_id: str,
    membership_id: str,
    position: str,
    request: Request,
    db: Session = Depends(get_db),
    section: str = Form(DEFAULT_FILTER),
) -> RedirectResponse:
    identifier = shell.uuid_or_none(campaign_id)
    membership = _membership(db, identifier, membership_id) if identifier else None
    pos = _position(position)
    if identifier is None or membership is None or pos is None:
        return shell.redirect(f"/app/campaigns/{campaign_id}", err="That email could not be found.")
    back = desk_url(identifier, membership.id, email=pos, section=section)
    try:
        email_progress.undo(db, membership_id=membership.id, position=pos, actor=_actor())
    except email_progress.EmailActionError as exc:
        db.rollback()
        return shell.redirect(back, err=str(exc))
    db.commit()
    return shell.redirect(back, ok=f"Email {pos} is no longer marked. The history is kept.")


@router.post("/campaigns/{campaign_id}/desk/{membership_id}/{position}/edit")
def desk_edit(
    campaign_id: str,
    membership_id: str,
    position: str,
    request: Request,
    db: Session = Depends(get_db),
    version_id: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    section: str = Form(DEFAULT_FILTER),
) -> RedirectResponse:
    """Save a new version of one email. History underneath is kept."""

    identifier = shell.uuid_or_none(campaign_id)
    membership = _membership(db, identifier, membership_id) if identifier else None
    pos = _position(position)
    if identifier is None or membership is None or pos is None:
        return shell.redirect(f"/app/campaigns/{campaign_id}", err="That email could not be found.")
    back = desk_url(identifier, membership.id, email=pos, section=section)
    version = shell.uuid_or_none(version_id)
    if version is None:
        return shell.redirect(back, err="That email version could not be found.")
    try:
        sequence_review.edit_message(
            db,
            message_version_id=version,
            subject=subject,
            body=body,
            actor=_actor(),
            reason="edited on the sending desk",
        )
    except sequence_review.SequenceReviewError as exc:
        db.rollback()
        return shell.redirect(back, err=str(exc))
    db.commit()
    return shell.redirect(back, ok="Saved. The earlier text is kept in the history.")


@router.post("/campaigns/{campaign_id}/desk/{membership_id}/{position}/gmail-draft")
def desk_gmail_draft(
    campaign_id: str,
    membership_id: str,
    position: str,
    request: Request,
    db: Session = Depends(get_db),
    version_id: str = Form(""),
    section: str = Form(DEFAULT_FILTER),
) -> RedirectResponse:
    """Create one Gmail draft for this exact email. Nothing is sent or scheduled."""

    identifier = shell.uuid_or_none(campaign_id)
    membership = _membership(db, identifier, membership_id) if identifier else None
    pos = _position(position)
    if identifier is None or membership is None or pos is None:
        return shell.redirect(f"/app/campaigns/{campaign_id}", err="That email could not be found.")
    back = desk_url(identifier, membership.id, email=pos, section=section)
    settings = get_settings()
    if not shell.gmail_drafts_on(db, settings):
        return shell.redirect(back, err="Gmail drafts are switched off in this environment.")
    version = shell.uuid_or_none(version_id)
    if version is None:
        return shell.redirect(back, err="That email version could not be found.")
    operator = current_operator()
    owner = session_account_id(operator)
    if operator is None or owner is None:
        return shell.redirect(
            back, err="Creating a Gmail draft needs a signed-in account with Gmail connected."
        )
    grant = gmail_mailbox.connected_grant(db, user_id=owner)
    if grant is None:
        return shell.redirect(
            back, err="No Gmail mailbox is connected. Connect one under Account → Connections."
        )
    from app.web.gmail_routes import oauth_client

    try:
        oauth = oauth_client(request, settings)
    except ValueError:
        return shell.redirect(back, err="Gmail is not configured in this environment.")
    provider = getattr(request.app.state, GMAIL_PROVIDER_STATE_KEY, None) or (
        gmail_provider.HttpGmailProvider(settings.gmail)
    )
    try:
        run = gmail_drafts.create_draft(
            db,
            message_version_id=version,
            grant=grant,
            settings=settings.gmail,
            oauth_client=oauth,
            provider=provider,
            actor=operator.email,
        )
    except gmail_mailbox.GmailMailboxError as exc:
        return shell.redirect(back, err=str(exc))
    except gmail_drafts.GmailDraftError as exc:
        db.rollback()
        return shell.redirect(back, err=str(exc))
    if run.fully_successful:
        return shell.redirect(
            back,
            ok=f"One Gmail draft created in {run.mailbox_address}. Nothing is sent or scheduled.",
        )
    return shell.redirect(back, err=run.summary())


__all__ = ["router", "READY_FILTERS", "DEFAULT_FILTER", "build_desk", "ready_rows", "filter_rows"]
