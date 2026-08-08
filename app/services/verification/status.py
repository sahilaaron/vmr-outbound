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

import dataclasses
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
from app.services.verification.provider import SIMULATOR_PROVIDER_LABEL

# How each evidence provenance is described to the operator, so a simulated
# outcome is never presented as an external MillionVerifier verification and a
# stored live result is shown as cached evidence rather than a fresh live call
# (VER-007).
_SOURCE_SIMULATED = "simulated"
_SOURCE_LIVE = "live"
_PROVENANCE_SUFFIX = {
    _SOURCE_SIMULATED: " Simulated result — no external verification performed.",
    _SOURCE_LIVE: " Live MillionVerifier evidence — produced by an external provider request.",
}

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


#: Marks an address a Campaign was handed in a contact file (IMP-001) rather
#: than one this system built and checked.
SOURCE_IMPORTED = "imported"

#: What an imported address's status says on every surface that renders one.
#:
#: The ordinary UNVERIFIED wording is "Not yet verified", which promises a check
#: that is still to come. For an imported address that promise is false: the
#: Email stage completed through the imported path, no candidate was generated,
#: no provider was called, and none ever will be for this address in the
#: Campaign that imported it. "Pending" was the visual label, and it said the
#: same thing more briefly.
IMPORTED_LABEL = "supplied by import"
IMPORTED_EXPLANATION = (
    "Supplied by a contact file import. No verification provider was called for "
    "this address, so this is a vendor-supplied claim rather than a "
    "provider-verified mailbox."
)


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
    # Provenance of the evidence being shown, when any: "simulated" or "live".
    # None for in-flight/operational/unverified states with no evidence to show.
    evidence_source: str | None = None
    #: Where the ADDRESS came from, as distinct from where its evidence came
    #: from. ``SOURCE_IMPORTED`` when a contact file supplied it and the import
    #: recorded the verification bypass; None otherwise.
    address_origin: str | None = None

    @property
    def is_imported(self) -> bool:
        return self.address_origin == SOURCE_IMPORTED

    @property
    def label(self) -> str:
        return IMPORTED_LABEL if self.is_imported else self.visual.value

    @property
    def is_simulated(self) -> bool:
        return self.evidence_source == _SOURCE_SIMULATED


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


def derive_status_for_email(
    session: Session, email: str | None, *, exclude_job_id: uuid.UUID | None = None
) -> StatusView:
    """Derive the status for one exact address (or the empty/no-address state).

    ``exclude_job_id`` omits one job from the in-flight check. The Verification
    Agent needs it: while its adapter runs, its own job is IN_PROGRESS, so an
    unfiltered read would answer "checking" and mask the very staleness or
    conflict the Agent is asking about. Excluding only the caller's own job keeps
    every other in-flight guarantee intact.
    """

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
    active_stmt = select(VerificationJob).where(
        VerificationJob.email == email,
        VerificationJob.status.in_(ACTIVE_JOB_STATUSES),
    )
    if exclude_job_id is not None:
        active_stmt = active_stmt.where(VerificationJob.id != exclude_job_id)
    active_job = session.scalars(
        active_stmt.order_by(VerificationJob.created_at.desc()).limit(1)
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
        return _view(email, precise, checked_at, is_stale=False, source=_source_of(latest))

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
        return _view(
            email,
            EmailPreciseStatus.STALE_EVIDENCE,
            checked_at,
            is_stale=True,
            source=_source_of(latest),
        )

    return _view(email, EmailPreciseStatus.UNVERIFIED, None, is_stale=False)


def _source_of(row: ExactEmailVerification | None) -> str | None:
    """Provenance of a stored evidence row: 'simulated', 'live', or None."""

    if row is None:
        return None
    return _SOURCE_SIMULATED if row.provider == SIMULATOR_PROVIDER_LABEL else _SOURCE_LIVE


def _view(
    email: str,
    precise: EmailPreciseStatus,
    checked_at: datetime | None,
    *,
    is_stale: bool,
    source: str | None = None,
) -> StatusView:
    explanation = _EXPLANATIONS[precise]
    if source is not None:
        explanation += _PROVENANCE_SUFFIX[source]
    return StatusView(
        email=email,
        precise=precise,
        visual=PRECISE_TO_VISUAL[precise],
        explanation=explanation,
        checked_at=checked_at,
        is_stale=is_stale,
        has_address=True,
        evidence_source=source,
    )


def _imported_origin(session: Session, contact: Contact, email: str | None) -> bool:
    """Whether *email* is an address a contact file supplied for this person.

    Asks the import's own evidence rather than inferring anything from the
    absence of a verification row: "no evidence yet" and "no provider will ever
    be asked" are different states and a surface that conflates them tells the
    operator to wait for something that is not coming.

    Contact-level and therefore campaign-agnostic, which is what the caller
    needs — this feeds the Contact page, which is not scoped to a Campaign. The
    campaign-scoped question is answered by
    :func:`app.services.imports.campaign_import.accepted_primary_email`, and the
    campaign-scoped surfaces use that instead.
    """

    if not email:
        return False
    # Imported locally: the verification domain must not take a module-level
    # dependency on the import subsystem.
    from app.models.enums import ImportedEmailSlot, ImportedEmailStageOutcome
    from app.models.imported_email import ImportedContactEmail

    return (
        session.scalars(
            select(ImportedContactEmail.id)
            .where(
                ImportedContactEmail.contact_id == contact.id,
                ImportedContactEmail.normalized_email == email,
                ImportedContactEmail.slot == ImportedEmailSlot.PRIMARY,
                ImportedContactEmail.email_stage_outcome
                == ImportedEmailStageOutcome.IMPORTED_EMAIL_ACCEPTED,
            )
            .limit(1)
        ).first()
        is not None
    )


def derive_status_for_contact(session: Session, contact: Contact) -> StatusView:
    email = address_for_contact(session, contact)
    view = derive_status_for_email(session, email)
    if not view.has_address or not _imported_origin(session, contact, view.email):
        return view
    # Provider evidence, where it exists, outranks the import: a mailbox somebody
    # actually checked is a stronger fact than a cell in a spreadsheet, and the
    # import label would hide it.
    if view.evidence_source is not None:
        return view
    return dataclasses.replace(
        view,
        explanation=IMPORTED_EXPLANATION,
        address_origin=SOURCE_IMPORTED,
    )


def status_for_contact_id(session: Session, contact_id: uuid.UUID) -> StatusView | None:
    contact = session.get(Contact, contact_id)
    if contact is None:
        return None
    return derive_status_for_contact(session, contact)
