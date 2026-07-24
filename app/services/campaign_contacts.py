"""Campaign-contact membership and outreach-history service (CMP-003).

Builds on the stable CMP-001 :class:`~app.models.campaign.Campaign` model and
the pre-existing :class:`~app.models.campaign.CampaignContact` membership table
(unique on ``(campaign_id, contact_id)`` since the Phase 1 schema — see
``migrations/versions/c11379ba2041_*.py``). CMP-003 does not touch that
constraint or introduce a parallel membership table; it adds the missing piece
identified during reconciliation: outreach *history* attributable to a
membership, plus one canonical, idempotent, suppression-respecting entry point
for joining a contact to a campaign outside the import pipeline.

## The "duplicate active outreach" rule (precise)

A contact has **at most one membership row per campaign, ever** — enforced by
the database via the pre-existing unique index
``uq_campaign_contacts_campaign_contact`` on ``campaign_contacts(campaign_id,
contact_id)``. :data:`app.models.enums.ALLOWED_CONTACT_TRANSITIONS` is a
strict DAG with two terminal states (``SUPPRESSED``, ``EXCLUDED``) and no
transition ever re-enters a non-terminal state, so that one membership row's
current state is always the single, unambiguous answer to "is this contact
under active outreach in this campaign, and if not, why." There is
structurally no way for a contact to acquire a second, conflicting active
outreach path in the same campaign — not a race condition to defend against at
the application layer, but a schema-level impossibility, verified here under
real concurrent inserts (see ``tests/test_campaign_contacts.py``).

Individual outreach *events* recorded against that one membership (a send
attempt, a bounce, a reply, a stop) are a different, narrower duplicate risk:
the same real-world event (e.g. a webhook) arriving twice must not create two
history rows. That is deduplicated by the pre-existing
``uq_external_events_provider_event_id`` unique index on
``external_events(provider, external_event_id)`` — CMP-003 reuses
:class:`~app.models.external_event.ExternalEvent` as the outreach-history
table (see its module docstring) rather than inventing a new one.

Sending identity/channel (e.g. a specific mailbox) is deliberately **not**
part of either key: no such concept exists anywhere in the repository yet
(Saleshandy integration is unbuilt), so adding it now would be an invented,
untested dimension rather than one "the existing architecture already
requires."
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import ContactWorkflowState
from app.models.external_event import ExternalEvent
from app.services.audit import record_audit_event
from app.services.suppressions import evaluate_suppression

# Terminal contact-workflow states (mirrors app.models.enums.ALLOWED_CONTACT_TRANSITIONS,
# where both map to an empty transition set). A membership in one of these states
# is never eligible for new outbound outreach.
_TERMINAL_STATES = frozenset({ContactWorkflowState.SUPPRESSED, ContactWorkflowState.EXCLUDED})


class CampaignContactNotFound(Exception):
    """Raised when the referenced campaign or contact does not exist."""


class OutreachError(Exception):
    """Raised when an outreach-event request is invalid or would be unsafe.

    The message is always safe to show an operator: it states which rule
    blocked the request, never a database error or internal identifier.
    """


def ensure_membership(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    contact_id: uuid.UUID,
    source_batch_id: uuid.UUID | None = None,
    actor: str = "operator",
) -> tuple[CampaignContact, bool]:
    """Return the ``(membership, created)`` for ``(campaign_id, contact_id)``.

    Idempotent and non-destructive: a repeat call for a pair that already has a
    membership returns the existing row completely unchanged — it never creates
    a second membership (invariant 4) and never resets an already-progressed
    membership back to ``IMPORTED``, so joining campaign B never disturbs
    activity already recorded in campaign A (invariants 2, 3, 6).

    The suppression ledger (DAT-006) is evaluated fresh on every call, so a
    suppression recorded after the contact's last import is never bypassed: a
    brand-new membership for a currently-suppressed identity starts
    ``SUPPRESSED``, never ``IMPORTED`` (invariant 7). This mirrors the
    suppression check the import pipeline already performs
    (``app/services/imports/importer.py``); it does not replace or weaken it.

    Raises :class:`CampaignContactNotFound` if either id does not resolve to an
    existing row.
    """

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignContactNotFound(f"campaign {campaign_id} does not exist")
    contact = session.get(Contact, contact_id)
    if contact is None:
        raise CampaignContactNotFound(f"contact {contact_id} does not exist")

    existing = session.scalars(
        select(CampaignContact).where(
            CampaignContact.campaign_id == campaign_id,
            CampaignContact.contact_id == contact_id,
        )
    ).first()
    if existing is not None:
        return existing, False

    decision = evaluate_suppression(session, email=contact.email, domain=contact.company_domain)
    state = ContactWorkflowState.SUPPRESSED if decision.blocked else ContactWorkflowState.IMPORTED

    membership = CampaignContact(
        campaign_id=campaign_id,
        contact_id=contact_id,
        source_batch_id=source_batch_id,
        state=state,
    )
    session.add(membership)
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="campaign_contact.membership_created",
        entity_type="campaign_contact",
        entity_id=str(membership.id),
        new_state=state.value,
        reason=(
            f"suppressed on join: {decision.blocked_reason}"
            if decision.blocked
            else "contact joined campaign"
        ),
        context={"campaign_id": str(campaign_id), "contact_id": str(contact_id)},
    )
    return membership, True


def record_outreach_event(
    session: Session,
    *,
    campaign_contact: CampaignContact,
    provider: str,
    external_event_id: str,
    event_type: str,
    occurred_at: datetime,
    is_outbound: bool = False,
    payload: dict[str, Any] | None = None,
    actor: str = "system",
) -> tuple[ExternalEvent, bool]:
    """Append one attributable outreach-history event; returns ``(event, created)``.

    This is a deliberately minimal seam, not a sending engine (CMP-003 does not
    build campaign execution): it only records that an event happened, against
    exactly one membership, for exactly one campaign. It never mutates or
    deletes an existing event, and never touches a different campaign's
    history — appending under campaign A's membership can never affect
    campaign B's rows (invariant 3).

    **Idempotent by provider event id** (invariant 9, invariant 4 for events):
    a retried or re-delivered call with the same ``(provider,
    external_event_id)`` returns the existing row (``created=False``) instead
    of raising or duplicating — enforced by
    ``uq_external_events_provider_event_id`` at the database, not only by the
    existence check here (see the concurrency tests).

    **``is_outbound=True`` is the suppression/eligibility gate** (invariant 7):
    when set, this refuses to record the event — raising
    :class:`OutreachError`, nothing is persisted — if the membership is
    already terminal (``SUPPRESSED``/``EXCLUDED``) or if the suppression ledger
    currently blocks the contact's email/domain, evaluated fresh via
    :func:`app.services.suppressions.evaluate_suppression` (the same
    authoritative primitive the verification path uses — never re-derived or
    weakened here). Non-outbound events (e.g. a bounce or unsubscribe arriving
    for a membership already SUPPRESSED) are always recorded — history must
    stay complete even after a contact becomes ineligible.
    """

    existing = session.scalars(
        select(ExternalEvent).where(
            ExternalEvent.provider == provider,
            ExternalEvent.external_event_id == external_event_id,
        )
    ).first()
    if existing is not None:
        return existing, False

    if is_outbound:
        if campaign_contact.state in _TERMINAL_STATES:
            raise OutreachError(
                "cannot record an outbound outreach event: this campaign "
                f"membership is {campaign_contact.state.value}"
            )
        contact = session.get(Contact, campaign_contact.contact_id)
        if contact is None:
            raise CampaignContactNotFound(f"contact {campaign_contact.contact_id} does not exist")
        decision = evaluate_suppression(session, email=contact.email, domain=contact.company_domain)
        if decision.blocked:
            raise OutreachError(
                "cannot record an outbound outreach event: identity is "
                f"suppressed ({decision.blocked_reason})"
            )

    event = ExternalEvent(
        provider=provider,
        external_event_id=external_event_id,
        event_type=event_type,
        received_at=occurred_at,
        payload=payload,
        contact_id=campaign_contact.contact_id,
        campaign_id=campaign_contact.campaign_id,
        campaign_contact_id=campaign_contact.id,
    )
    session.add(event)
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="outreach.event_recorded",
        entity_type="campaign_contact",
        entity_id=str(campaign_contact.id),
        reason=f"{provider} {event_type}",
        context={
            "external_event_id": external_event_id,
            "event_type": event_type,
            "is_outbound": is_outbound,
        },
    )
    return event, True


def campaign_contact_history(
    session: Session, campaign_contact_id: uuid.UUID
) -> list[ExternalEvent]:
    """All outreach-history events recorded against one membership, oldest first."""

    return list(
        session.scalars(
            select(ExternalEvent)
            .where(ExternalEvent.campaign_contact_id == campaign_contact_id)
            .order_by(ExternalEvent.received_at)
        ).all()
    )


def contact_campaign_history(
    session: Session, *, contact_id: uuid.UUID, campaign_id: uuid.UUID
) -> list[ExternalEvent]:
    """All outreach-history events for one ``(contact, campaign)`` pair, oldest first.

    Scoped by ``contact_id``/``campaign_id`` directly (not only
    ``campaign_contact_id``) so history stays queryable even in the rare case
    where the membership row itself no longer exists — e.g. after DAT-004's
    duplicate-contact merge coalesces a redundant membership
    (``app/services/identity.py::_apply_merge``, which re-parents these events
    onto the surviving membership before removing the redundant row).
    """

    return list(
        session.scalars(
            select(ExternalEvent)
            .where(
                ExternalEvent.contact_id == contact_id,
                ExternalEvent.campaign_id == campaign_id,
            )
            .order_by(ExternalEvent.received_at)
        ).all()
    )
