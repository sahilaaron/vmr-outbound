"""The selling organisation's profile (KB-001).

One row, read and written whole. There is no partial-update API and no field
provenance ledger: the operator is the only source, so "who said this and how
sure are we" has one answer everywhere and recording it per field would be
ceremony without information.

The caller owns the transaction boundary; nothing here commits.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.seller_profile import SellerProfile
from app.services.audit import record_audit_event
from app.services.seller.common import (
    OPERATOR_ACTOR,
    clean_list,
    optional_text,
    required_text,
)


def get_profile(session: Session) -> SellerProfile | None:
    """Return the current seller profile, or ``None`` if none has been entered.

    ``None`` is a real answer, not an error. A system with no profile yet is
    the normal starting state, and readiness reports it as such.
    """

    return session.scalars(
        select(SellerProfile).where(SellerProfile.is_current.is_(True))
    ).one_or_none()


def save_profile(
    session: Session,
    *,
    name: str,
    short_description: str | None = None,
    description: str | None = None,
    positioning: str | None = None,
    communication_guidance: str | None = None,
    notes: str | None = None,
    industries_served: list[str] | None = None,
    geographies_served: list[str] | None = None,
    capabilities: list[str] | None = None,
    differentiators: list[str] | None = None,
    updated_by: str | None = None,
) -> tuple[SellerProfile, bool]:
    """Create or update the single seller profile.

    Returns ``(profile, created)``. Saving is idempotent in the sense that
    matters: there is never a second profile, because the database index makes
    one impossible and this function updates in place.
    """

    cleaned_name = required_text(name, field="name", label="Company name")
    profile = get_profile(session)
    created = profile is None
    if profile is None:
        profile = SellerProfile(name=cleaned_name, is_current=True)
        session.add(profile)
    else:
        profile.name = cleaned_name

    profile.short_description = optional_text(
        short_description, field="short_description", label="Short description"
    )
    profile.description = optional_text(description, field="description", label="Description")
    profile.positioning = optional_text(positioning, field="positioning", label="Positioning")
    profile.communication_guidance = optional_text(
        communication_guidance,
        field="communication_guidance",
        label="Communication guidance",
    )
    profile.notes = optional_text(notes, field="notes", label="Notes")
    profile.industries_served = clean_list(industries_served, label="Industries served")
    profile.geographies_served = clean_list(geographies_served, label="Geographies served")
    profile.capabilities = clean_list(capabilities, label="Capabilities")
    profile.differentiators = clean_list(differentiators, label="Differentiators")
    profile.updated_by = optional_text(updated_by, field="created_by", label="Updated by")

    session.flush()

    record_audit_event(
        session,
        actor=updated_by or OPERATOR_ACTOR,
        action="seller_profile.created" if created else "seller_profile.updated",
        entity_type="seller_profile",
        entity_id=str(profile.id),
        reason="Operator entered the seller company profile."
        if created
        else "Operator edited the seller company profile.",
        context={"name": profile.name},
    )
    return profile, created
