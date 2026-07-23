"""Contact workflow-state transitions (CMP-002).

A contact's per-campaign state lives on the :class:`CampaignContact` membership.
Transitions are validated against :data:`ALLOWED_CONTACT_TRANSITIONS`; an illegal
transition raises rather than silently corrupting state, and every accepted
transition records an audit event with the previous and new state.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.campaign import CampaignContact
from app.models.enums import ALLOWED_CONTACT_TRANSITIONS, ContactWorkflowState
from app.services.audit import record_audit_event


class InvalidStateTransition(Exception):
    """Raised when a contact workflow-state transition is not permitted."""

    def __init__(self, current: ContactWorkflowState, target: ContactWorkflowState) -> None:
        super().__init__(f"illegal contact state transition: {current.value} -> {target.value}")
        self.current = current
        self.target = target


def is_transition_allowed(current: ContactWorkflowState, target: ContactWorkflowState) -> bool:
    """Return True if moving from *current* to *target* is permitted."""

    return target in ALLOWED_CONTACT_TRANSITIONS.get(current, frozenset())


def transition_contact_state(
    session: Session,
    membership: CampaignContact,
    *,
    target: ContactWorkflowState,
    actor: str = "system",
    reason: str | None = None,
) -> CampaignContact:
    """Validate and apply a workflow-state transition, recording an audit event.

    A no-op transition (target == current) is allowed and simply re-affirms the
    state without an audit event. Any other transition must appear in the allowed
    map or :class:`InvalidStateTransition` is raised.
    """

    current = membership.state
    if target == current:
        return membership
    if not is_transition_allowed(current, target):
        raise InvalidStateTransition(current, target)

    membership.state = target
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="contact.state_changed",
        entity_type="campaign_contact",
        entity_id=str(membership.id),
        previous_state=current.value,
        new_state=target.value,
        reason=reason,
    )
    return membership
