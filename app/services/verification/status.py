"""Derive the current verification status beside a prospect's email (VER-004 / UI).

This is the single read-model that every surface uses to render the four-state
icon truthfully. It combines, for one exact address:

* the latest address evidence and whether it is still fresh under the policy;
* whether two fresh pieces of evidence disagree (a conflict);
* any in-flight job (queued / checking / retry / stale-recheck);
* the terminal operational outcome of the most recent finished job (insufficient
  credits, provider error) when there is no fresh evidence to show instead.

It returns a precise status, the mapped visible state, and a plain explanation.
Catch-all, unknown, disposable, role-based, provider errors, insufficient credits,
stale evidence, and conflicts all resolve to WARNING — never to SUCCESSFUL or a
FAILURE that would read as a definitively bad mailbox.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    PRECISE_TO_VISUAL,
    EmailPreciseStatus,
    EmailVisualStatus,
    VerificationJobStatus,
)
from app.models.verification_job import ACTIVE_JOB_STATUSES, VerificationJob
from app.services.verification.policy import get_policy

# Precise statuses that a finished job may leave as an operational condition to
# surface when no fresh address evidence exists.
_OPERATIONAL = {
    EmailPreciseStatus.INSUFFICIENT_CREDITS,
    EmailPreciseStatus.PROVIDER_ERROR,
}

_EXPLANATIONS = {
    EmailPreciseStatus.UNVERIFIED: "Not yet verified.",
    EmailPreciseStatus.QUEUED: "Queued for verification.",
    EmailPreciseStatus.CHECKING: "Verification in progress.",
    EmailPreciseStatus.RETRY_SCHEDULED: "A transient failure occurred; a retry is scheduled.",
    EmailPreciseStatus.STALE_RECHECK_SCHEDULED: "Evidence is stale; a recheck is scheduled.",
    EmailPreciseStatus.VALID: "Fresh result: the mailbox exists.",
    EmailPreciseStatus.INVALID: "Fresh result: the mailbox does not exist.",
    EmailPreciseStatus.CATCH_ALL: "Catch-all domain: existence unproven; not scheduling-ready.",
    EmailPreciseStatus.UNKNOWN: "Provider could not determine the mailbox.",
    EmailPreciseStatus.DISPOSABLE: "Disposable/temporary mailbox.",
    EmailPreciseStatus.ROLE_BASED: "Valid but role-based address; not an individual target.",
    EmailPreciseStatus.PROVIDER_ERROR: "Provider error; no verdict yet.",
    EmailPreciseStatus.INSUFFICIENT_CREDITS: "Insufficient provider credits; top up and re-run.",
    EmailPreciseStatus.STALE_EVIDENCE: "Evidence is stale and no recheck is scheduled.",
    EmailPreciseStatus.CONFLICTING_EVIDENCE: "Fresh evidence for this address disagrees.",
}


@dataclass(frozen=True)
class StatusView:
    """The rendered verification status for one address."""

    email: str | None
    precise: EmailPreciseStatus
    visual: EmailVisualStatus
    explanation: str
    checked_at: datetime | None = None
    is_stale: bool = False
    has_address: bool = True

    @property
    def label(self) -> str:
        return self.visual.value


def _now() -> datetime:
    return datetime.now(UTC)


def address_for_contact(session: Session, contact: Contact) -> str | None:
    """The address a contact's status is about: imported email, else selected candidate."""

    if contact.email:
        return contact.email.lower()
    selected = session.scalars(
        select(EmailCandidate).where(
            EmailCandidate.contact_id == contact.id, EmailCandidate.selected.is_(True)
        )
    ).first()
    return selected.email if selected else None


def derive_status_for_email(session: Session, email: str | None) -> StatusView:
    """Derive the status for one exact address (or the empty/no-address state)."""

    if not email:
        return StatusView(
            email=None,
            precise=EmailPreciseStatus.UNVERIFIED,
            visual=EmailVisualStatus.PENDING,
            explanation="No address to verify yet.",
            has_address=False,
        )

    email = email.lower()
    policy = get_policy(get_settings())
    now = _now()

    # Latest two evidence rows (for conflict detection), newest first.
    evidence = list(
        session.scalars(
            select(ExactEmailVerification)
            .where(ExactEmailVerification.email == email)
            .order_by(ExactEmailVerification.checked_at.desc())
            .limit(5)
        ).all()
    )
    latest = evidence[0] if evidence else None
    fresh = bool(latest and policy.is_fresh(latest.result, latest.checked_at, now))

    # Active (in-flight) job for this address, if any.
    active_job = session.scalars(
        select(VerificationJob)
        .where(
            VerificationJob.email == email,
            VerificationJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(VerificationJob.created_at.desc())
        .limit(1)
    ).first()

    # Conflict: the two most recent *fresh* results disagree.
    conflict = False
    fresh_rows = [e for e in evidence if policy.is_fresh(e.result, e.checked_at, now)]
    if len(fresh_rows) >= 2 and fresh_rows[0].result != fresh_rows[1].result:
        conflict = True

    checked_at = latest.checked_at if latest else None

    if active_job is not None:
        if latest is not None and not fresh:
            precise = EmailPreciseStatus.STALE_RECHECK_SCHEDULED
        elif active_job.status == VerificationJobStatus.IN_PROGRESS:
            precise = EmailPreciseStatus.CHECKING
        elif active_job.status == VerificationJobStatus.RETRY_SCHEDULED:
            precise = EmailPreciseStatus.RETRY_SCHEDULED
        else:
            precise = EmailPreciseStatus.QUEUED
        return _view(email, precise, checked_at, is_stale=bool(latest and not fresh))

    if latest is not None and fresh:
        if conflict:
            precise = EmailPreciseStatus.CONFLICTING_EVIDENCE
        else:
            precise = policy.precise_for_result(latest.result, is_role=bool(latest.is_role))
        return _view(email, precise, checked_at, is_stale=False)

    # No fresh evidence and no active job: surface a terminal operational outcome
    # from the most recent finished job, else stale evidence, else unverified.
    last_job = session.scalars(
        select(VerificationJob)
        .where(VerificationJob.email == email)
        .order_by(VerificationJob.updated_at.desc())
        .limit(1)
    ).first()
    if last_job is not None and last_job.outcome_status:
        try:
            precise_op = EmailPreciseStatus(last_job.outcome_status)
        except ValueError:
            precise_op = None
        if precise_op in _OPERATIONAL:
            return _view(email, precise_op, checked_at, is_stale=bool(latest and not fresh))

    if latest is not None:
        return _view(email, EmailPreciseStatus.STALE_EVIDENCE, checked_at, is_stale=True)

    return _view(email, EmailPreciseStatus.UNVERIFIED, None, is_stale=False)


def _view(
    email: str, precise: EmailPreciseStatus, checked_at: datetime | None, *, is_stale: bool
) -> StatusView:
    return StatusView(
        email=email,
        precise=precise,
        visual=PRECISE_TO_VISUAL[precise],
        explanation=_EXPLANATIONS[precise],
        checked_at=checked_at,
        is_stale=is_stale,
        has_address=True,
    )


def derive_status_for_contact(session: Session, contact: Contact) -> StatusView:
    return derive_status_for_email(session, address_for_contact(session, contact))


def status_for_contact_id(session: Session, contact_id: uuid.UUID) -> StatusView | None:
    contact = session.get(Contact, contact_id)
    if contact is None:
        return None
    return derive_status_for_contact(session, contact)
