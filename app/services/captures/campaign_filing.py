"""Optional capture-to-Campaign filing isolated from permanent Contact capture."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, CaptureCampaignFilingStatus
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.pipeline import CaptureCampaignFiling
from app.services.campaign_contacts import CampaignContactError, enrol_contact


@dataclass(frozen=True)
class FilingResult:
    filing: CaptureCampaignFiling
    applied: bool

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "status": self.filing.status.value,
            "requested_campaign_id": str(self.filing.requested_campaign_id),
            "campaign_id": (str(self.filing.campaign_id) if self.filing.campaign_id else None),
            "campaign_contact_id": (
                str(self.filing.campaign_contact_id) if self.filing.campaign_contact_id else None
            ),
            "attempts": self.filing.attempts,
            "error_code": self.filing.error_code,
            "error_detail": self.filing.error_detail,
        }


def get_filing(session: Session, *, capture_id: uuid.UUID) -> CaptureCampaignFiling | None:
    return session.scalars(
        select(CaptureCampaignFiling).where(CaptureCampaignFiling.capture_id == capture_id)
    ).one_or_none()


def ensure_filing(
    session: Session,
    *,
    snapshot: LinkedInProfileSnapshot,
    requested_campaign_id: uuid.UUID,
) -> CaptureCampaignFiling:
    """Create the durable filing intent once, even when Campaign lookup fails."""

    existing = session.scalars(
        select(CaptureCampaignFiling).where(CaptureCampaignFiling.capture_id == snapshot.id)
    ).one_or_none()
    if existing is not None:
        if existing.requested_campaign_id != requested_campaign_id:
            raise ValueError("capture filing Campaign does not match its original request")
        return existing
    campaign = session.get(Campaign, requested_campaign_id)
    filing = CaptureCampaignFiling(
        capture_id=snapshot.id,
        submission_id=snapshot.submission_id,
        requested_campaign_id=requested_campaign_id,
        campaign_id=campaign.id if campaign else None,
        status=(
            CaptureCampaignFilingStatus.PENDING
            if campaign is not None and campaign.status is not CampaignStatus.ARCHIVED
            else CaptureCampaignFilingStatus.FAILED
        ),
        error_code=(
            None
            if campaign is not None and campaign.status is not CampaignStatus.ARCHIVED
            else ("campaign_archived" if campaign is not None else "campaign_not_found")
        ),
        error_detail=(
            None
            if campaign is not None and campaign.status is not CampaignStatus.ARCHIVED
            else (
                "The selected Campaign is archived."
                if campaign is not None
                else "The selected Campaign does not exist."
            )
        ),
    )
    try:
        with session.begin_nested():
            session.add(filing)
            session.flush()
    except IntegrityError as exc:
        winner = session.scalars(
            select(CaptureCampaignFiling).where(CaptureCampaignFiling.capture_id == snapshot.id)
        ).one()
        if winner.requested_campaign_id != requested_campaign_id:
            raise ValueError("capture filing Campaign does not match its original request") from exc
        return winner
    return filing


def apply_filing(
    session: Session,
    *,
    filing: CaptureCampaignFiling,
    snapshot: LinkedInProfileSnapshot,
    contact: Contact,
    actor: str,
) -> FilingResult:
    """Attempt Campaign Contact upsert in a SAVEPOINT.

    Any failure updates only the filing record. The caller's Contact/capture
    transaction remains committable, which is the product's required boundary.
    """

    if (
        filing.status is CaptureCampaignFilingStatus.APPLIED
        and filing.campaign_contact_id is not None
    ):
        return FilingResult(filing=filing, applied=True)
    if filing.campaign_id is None:
        return FilingResult(filing=filing, applied=False)

    filing.attempts += 1
    try:
        with session.begin_nested():
            result = enrol_contact(
                session,
                campaign_id=filing.campaign_id,
                contact_id=contact.id,
                source_type="capture",
                source_reference=str(snapshot.id),
                source_context={
                    "submission_id": (
                        str(snapshot.submission_id) if snapshot.submission_id else None
                    ),
                    "capture_mode": snapshot.capture_mode,
                    "source_surface": snapshot.source_surface,
                },
                capture_id=snapshot.id,
                idempotency_key=(f"capture-filing:{snapshot.id}:{filing.requested_campaign_id}"),
                actor=actor,
                enqueue=True,
            )
    except CampaignContactError as exc:
        filing.status = CaptureCampaignFilingStatus.FAILED
        filing.error_code = type(exc).__name__
        filing.error_detail = str(exc)
        filing.campaign_contact_id = None
        session.flush()
        return FilingResult(filing=filing, applied=False)
    except Exception as exc:  # noqa: BLE001 - filing is isolated from capture success
        filing.status = CaptureCampaignFilingStatus.FAILED
        filing.error_code = "filing_error"
        filing.error_detail = (
            f"Campaign filing failed; the permanent Contact was still saved ({type(exc).__name__})."
        )
        filing.campaign_contact_id = None
        session.flush()
        return FilingResult(filing=filing, applied=False)

    filing.status = CaptureCampaignFilingStatus.APPLIED
    filing.campaign_contact_id = result.membership.id
    filing.error_code = None
    filing.error_detail = None
    filing.applied_at = datetime.now(UTC)
    session.flush()
    return FilingResult(filing=filing, applied=True)
