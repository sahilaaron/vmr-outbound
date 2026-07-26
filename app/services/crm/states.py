"""The four workflow dimensions, kept separate and derived, never stored.

A single overloaded status field cannot describe a person honestly. Someone can
be captured but not researched, researched but not qualified, qualified but
without a usable address, and emailable but suppressed — all at once. So the CRM
reports four independent dimensions plus the suppression authority, and computes
each one from whatever record actually owns that truth:

===================  ==================================================
Dimension            Authority
===================  ==================================================
capture / identity   ``linkedin_profile_snapshots.outcome`` (DAT-013)
research             not built yet — APP-004
qualification        not built yet — APP-006
email                ``verification/status.py`` (VER-002/003)
suppression          the suppression ledger (DAT-006)
===================  ==================================================

Nothing here writes. Deriving rather than storing means no backfill, no second
source of truth to drift, and no risk of a stale status outliving the evidence
that produced it.

Outreach readiness is deliberately absent. It is a *computed decision* over all
of these plus policy, and the policy belongs to APP-007. Adding a readiness
column here would recreate the overloaded status field this module exists to
avoid.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.models.enums import (
    CaptureIdentityState,
    EmailPreciseStatus,
    EmailVisualStatus,
    LinkedInSnapshotOutcome,
    QualificationState,
    ResearchState,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.services import suppressions as suppression_service
from app.services.verification import status as verification_status

# Capture outcomes that mean "saved, but not yet a canonical contact". These are
# the rows the CRM must keep visible: the operator already decided this person
# matters, and the system simply has not finished resolving them.
PENDING_OUTCOMES: tuple[LinkedInSnapshotOutcome, ...] = (
    LinkedInSnapshotOutcome.UNMATCHED_STAGED,
    LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW,
)

_OUTCOME_TO_IDENTITY: dict[LinkedInSnapshotOutcome, CaptureIdentityState] = {
    LinkedInSnapshotOutcome.STORED: CaptureIdentityState.CANONICAL,
    LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED: CaptureIdentityState.CANONICAL,
    LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED: CaptureIdentityState.CANONICAL,
    LinkedInSnapshotOutcome.UNMATCHED_STAGED: CaptureIdentityState.AWAITING_COMPANY,
    LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW: CaptureIdentityState.AMBIGUOUS_IDENTITY,
    LinkedInSnapshotOutcome.SUPPRESSED: CaptureIdentityState.REJECTED,
    LinkedInSnapshotOutcome.DUPLICATE_IN_SUBMISSION: CaptureIdentityState.REJECTED,
}


@dataclass(frozen=True)
class WorkflowStates:
    """The four dimensions for one person, plus the suppression verdict.

    ``research`` and ``qualification`` are constants today. That is the truthful
    answer — no engine has run — and it is reported rather than hidden so the
    operator is never shown a status the system cannot support.
    """

    identity: CaptureIdentityState
    research: ResearchState
    qualification: QualificationState
    email_precise: EmailPreciseStatus
    email_visual: EmailVisualStatus
    email_explanation: str
    suppressed: bool
    suppression_reason: str | None = None

    @property
    def is_pending_capture(self) -> bool:
        return self.identity in (
            CaptureIdentityState.AWAITING_COMPANY,
            CaptureIdentityState.AMBIGUOUS_IDENTITY,
        )


def identity_state_for_outcome(outcome: LinkedInSnapshotOutcome) -> CaptureIdentityState:
    """Project a capture outcome onto the identity dimension."""

    return _OUTCOME_TO_IDENTITY.get(outcome, CaptureIdentityState.CANONICAL)


def states_for_contact(session: Session, contact: Contact) -> WorkflowStates:
    """Derive every dimension for a canonical contact."""

    email_view = verification_status.derive_status_for_contact(session, contact)
    decision = suppression_service.evaluate_suppression(
        session,
        email=contact.email,
        domain=contact.company_domain,
    )
    return WorkflowStates(
        identity=CaptureIdentityState.CANONICAL,
        research=ResearchState.NOT_REQUESTED,
        qualification=QualificationState.NOT_ASSESSED,
        email_precise=email_view.precise,
        email_visual=email_view.visual,
        email_explanation=email_view.explanation,
        suppressed=decision.blocked,
        suppression_reason=decision.blocked_reason,
    )


def states_for_capture(session: Session, snapshot: LinkedInProfileSnapshot) -> WorkflowStates:
    """Derive every dimension for a capture that has no contact row yet.

    A pending capture has no company domain and no address, so the email
    dimension is honestly ``UNVERIFIED`` rather than borrowed from somewhere
    else. Its suppression verdict comes from the capture outcome, which the
    intake path already evaluated against the ledger.
    """

    identity = identity_state_for_outcome(snapshot.outcome)
    suppressed = snapshot.outcome == LinkedInSnapshotOutcome.SUPPRESSED
    return WorkflowStates(
        identity=identity,
        research=ResearchState.NOT_REQUESTED,
        qualification=QualificationState.NOT_ASSESSED,
        email_precise=EmailPreciseStatus.UNVERIFIED,
        email_visual=EmailVisualStatus.PENDING,
        email_explanation="No address yet — this person is not a canonical contact.",
        suppressed=suppressed,
        suppression_reason="suppressed at capture" if suppressed else None,
    )


def latest_capture_for_contact(
    session: Session, contact_id: uuid.UUID
) -> LinkedInProfileSnapshot | None:
    """The most recent capture that resolved to this contact, if any.

    A contact created by a spreadsheet import has none, and that is a normal
    state rather than an error.
    """

    return session.scalars(
        select(LinkedInProfileSnapshot)
        .where(LinkedInProfileSnapshot.matched_contact_id == contact_id)
        .order_by(LinkedInProfileSnapshot.ingested_at.desc())
        .limit(1)
    ).first()
