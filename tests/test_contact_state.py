"""Contact workflow-state transition tests (CMP-002)."""

from __future__ import annotations

import pytest
from app.models.campaign import CampaignContact
from app.models.enums import ContactWorkflowState
from app.services.campaigns import create_campaign
from app.services.contact_state import (
    InvalidStateTransition,
    is_transition_allowed,
    transition_contact_state,
)
from app.services.imports.importer import _create_contact
from sqlalchemy.orm import Session


def _membership(db_session: Session, state: ContactWorkflowState) -> CampaignContact:
    campaign = create_campaign(db_session, name=f"C {state.value}")
    contact = _create_contact(
        db_session,
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "company_name": "Analytical Engines",
            "company_domain": "analyticalengines.example",
            "email": f"ada-{state.value}@analyticalengines.example",
            "title": None,
            "linkedin_url": None,
            "country": None,
            "industry": None,
            "company_size": None,
        },
        "ada|lovelace|analyticalengines.example",
    )
    membership = CampaignContact(campaign_id=campaign.id, contact_id=contact.id, state=state)
    db_session.add(membership)
    db_session.flush()
    return membership


def test_allowed_transition_table() -> None:
    assert is_transition_allowed(
        ContactWorkflowState.IMPORTED, ContactWorkflowState.AWAITING_VERIFICATION
    )
    assert is_transition_allowed(ContactWorkflowState.IMPORTED, ContactWorkflowState.SUPPRESSED)
    # Terminal states cannot leave.
    assert not is_transition_allowed(ContactWorkflowState.SUPPRESSED, ContactWorkflowState.IMPORTED)
    assert not is_transition_allowed(
        ContactWorkflowState.AWAITING_VERIFICATION, ContactWorkflowState.IMPORTED
    )


def test_valid_transition_updates_and_audits(db_session: Session) -> None:
    membership = _membership(db_session, ContactWorkflowState.IMPORTED)
    transition_contact_state(
        db_session,
        membership,
        target=ContactWorkflowState.AWAITING_VERIFICATION,
        reason="advanced",
    )
    assert membership.state is ContactWorkflowState.AWAITING_VERIFICATION


def test_invalid_transition_raises(db_session: Session) -> None:
    membership = _membership(db_session, ContactWorkflowState.SUPPRESSED)
    with pytest.raises(InvalidStateTransition):
        transition_contact_state(db_session, membership, target=ContactWorkflowState.IMPORTED)


def test_noop_transition_is_allowed(db_session: Session) -> None:
    membership = _membership(db_session, ContactWorkflowState.IMPORTED)
    result = transition_contact_state(db_session, membership, target=ContactWorkflowState.IMPORTED)
    assert result.state is ContactWorkflowState.IMPORTED
