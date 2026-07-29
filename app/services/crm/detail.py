"""Read models for the contact and pending-capture detail pages.

The operator opening one of these pages is trying to answer six questions: who
is this person, where did the data come from, what is known, what is uncertain,
what has changed, and what still has to happen. Every section below exists to
answer one of them from real records.

Two rules shape the whole module.

**Nothing is invented.** Research and qualification have no engine yet
(APP-004 and APP-006), so those sections carry their truthful "not requested" /
"not assessed" state and an explanation, rather than an empty panel that reads
like a bug or a plausible-looking placeholder that reads like data.

**Absence is information.** A pending capture has no company domain, no address
and no contact row — that is precisely why it is pending, so it is stated rather
than blanked. A contact with no capture came from a spreadsheet, which is worth
knowing when judging how current its fields are.

Neither builder takes a campaign. Campaign membership appears on a contact as
*history*, and a contact with none is a normal contact, not a broken one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.contact_capture import ContactCaptureNote, ContactCaptureSubmission, ContactLabel
from app.models.contact_field_value import ContactFieldValue
from app.models.enums import LinkedInSnapshotOutcome
from app.models.linkedin_profile import (
    LinkedInProfileExperienceObservation,
    LinkedInProfileSnapshot,
)
from app.models.qa_evaluation import ContactQAEvaluation

# Aliased: a bare `annotations` import would shadow `from __future__ import
# annotations` above, and the module would silently resolve to the __future__
# feature flag instead.
from app.services.crm import annotations as crm_annotations
from app.services.crm.records import current_experience
from app.services.crm.states import (
    WorkflowStates,
    latest_capture_for_contact,
    states_for_capture,
    states_for_contact,
)
from app.services.resolution import service as resolution_service

# Shown wherever a downstream stage has not run. Phrased as a statement about
# the system, not about the person: "nothing has been requested" is a fact,
# "no research found" would be a claim we cannot support.
RESEARCH_NOT_BUILT = (
    "No research has been requested for this person. Company and contact research "
    "arrives in a later stage; nothing has run, so there is nothing to show."
)
QUALIFICATION_NOT_BUILT = (
    "This person has not been assessed. Qualification is a later stage; no judgement "
    "has been recorded, and capture alone never implies a fit."
)


@dataclass(frozen=True)
class EmploymentObservation:
    """One observed role, with the caveats that make it readable.

    ``dates_reliable`` matters more than it looks: LinkedIn timelines are
    frequently ambiguous, and presenting an unreliable date as fact is how a
    "current" role silently becomes wrong.
    """

    company_name: str | None
    job_title: str | None
    is_current: bool | None
    timeline_text: str | None
    duration_text: str | None
    dates_reliable: bool
    role_location: str | None
    employment_type: str | None
    observed_at: datetime | None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CaptureEvidence:
    """One immutable capture, summarised for inspection.

    Deliberately omits the raw payload. The operator needs to know a capture
    happened, from where, under which parser, and what it produced — not to read
    a verbatim page scrape in a list view. The dedicated snapshot page already
    exists for that.
    """

    capture_id: uuid.UUID
    source: str
    source_url: str | None
    captured_at: datetime | None
    ingested_at: datetime
    schema_version: str
    adapter_version: str | None
    extraction_status: str
    outcome: LinkedInSnapshotOutcome
    warnings: list[str]
    missing_sections: list[str]
    matched_contact_id: uuid.UUID | None
    submission_id: uuid.UUID | None

    @property
    def detail_url(self) -> str:
        return f"/profiles/{self.capture_id}"


@dataclass(frozen=True)
class FieldProvenance:
    """Which observation currently backs one canonical field.

    This is the DAT-005 ledger read back: the winning value, where it came from,
    when it was observed, and why the policy chose it.
    """

    field_name: str
    value: str | None
    source_name: str | None
    observed_at: datetime | None
    confidence: float | None
    policy_version: str
    decision_reason: str | None
    is_manual_override: bool


@dataclass(frozen=True)
class CompanyLink:
    """What is known about this person's employer, and how firmly.

    ``resolution_note`` explains an unresolved or ambiguous link in the
    operator's terms, because "no company" and "a company we cannot yet name"
    are different situations that must not look identical.
    """

    company: Company | None
    captured_name: str | None
    domain: str | None
    linkedin_company_url: str | None
    linkedin_company_id: str | None
    is_resolved: bool
    resolution_note: str
    # How that domain was decided, when automatic resolution decided it
    # (DAT-017A). None means no decision record exists for this company, which
    # is the ordinary case for an imported or operator-supplied domain and is
    # NOT the same as an uncertain one.
    domain_resolution: resolution_service.DecisionView | None = None


@dataclass
class ContactDetailView:
    """Everything the contact detail page renders."""

    contact: Contact
    states: WorkflowStates
    labels: list[ContactLabel] = field(default_factory=list)
    notes: list[ContactCaptureNote] = field(default_factory=list)
    employment: list[EmploymentObservation] = field(default_factory=list)
    company: CompanyLink | None = None
    captures: list[CaptureEvidence] = field(default_factory=list)
    field_provenance: list[FieldProvenance] = field(default_factory=list)
    qa_evaluations: list[ContactQAEvaluation] = field(default_factory=list)
    memberships: list[tuple[CampaignContact, Campaign | None]] = field(default_factory=list)
    research_note: str = RESEARCH_NOT_BUILT
    qualification_note: str = QUALIFICATION_NOT_BUILT

    @property
    def full_name(self) -> str:
        return (
            " ".join(part for part in (self.contact.first_name, self.contact.last_name) if part)
            or "(name not captured)"
        )

    @property
    def has_captures(self) -> bool:
        return bool(self.captures)


@dataclass
class CaptureDetailView:
    """Everything the pending-capture detail page renders.

    A capture is not a contact, and the page must not pretend otherwise: there
    is no email section and no campaign history, because neither exists for
    someone who has not been promoted yet.
    """

    capture: LinkedInProfileSnapshot
    states: WorkflowStates
    labels: list[ContactLabel] = field(default_factory=list)
    notes: list[ContactCaptureNote] = field(default_factory=list)
    employment: list[EmploymentObservation] = field(default_factory=list)
    company: CompanyLink | None = None
    evidence: CaptureEvidence | None = None
    submission: ContactCaptureSubmission | None = None
    candidate_contacts: list[Contact] = field(default_factory=list)
    research_note: str = RESEARCH_NOT_BUILT
    qualification_note: str = QUALIFICATION_NOT_BUILT

    @property
    def full_name(self) -> str:
        fields: dict[str, Any] = self.capture.profile_fields or {}
        return str(fields.get("full_name") or "(name not captured)")

    @property
    def is_ambiguous(self) -> bool:
        return self.capture.outcome == LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW


def _employment_from(
    observations: list[LinkedInProfileExperienceObservation],
) -> list[EmploymentObservation]:
    """Project experience rows for display, newest-listed first.

    History is never flattened into a single current role: every observation the
    capture carried is preserved and shown, because a role that disappeared
    between captures is itself a signal.
    """

    return [
        EmploymentObservation(
            company_name=obs.company_name,
            job_title=obs.job_title,
            is_current=obs.is_current,
            timeline_text=obs.timeline_text,
            duration_text=obs.duration_text,
            dates_reliable=bool(obs.dates_reliable),
            role_location=obs.role_location,
            employment_type=obs.employment_type,
            observed_at=obs.observed_at,
            warnings=[str(w) for w in (obs.warnings or [])],
        )
        for obs in sorted(observations, key=lambda o: o.position_index)
    ]


def _evidence_from(snapshot: LinkedInProfileSnapshot) -> CaptureEvidence:
    return CaptureEvidence(
        capture_id=snapshot.id,
        source=snapshot.source,
        source_url=snapshot.source_url,
        captured_at=snapshot.captured_at,
        ingested_at=snapshot.ingested_at,
        schema_version=snapshot.schema_version,
        adapter_version=snapshot.adapter_version,
        extraction_status=snapshot.extraction_status,
        outcome=snapshot.outcome,
        warnings=[str(w) for w in (snapshot.page_warnings or [])],
        missing_sections=[str(s) for s in (snapshot.missing_sections or [])],
        matched_contact_id=snapshot.matched_contact_id,
        submission_id=snapshot.submission_id,
    )


def get_contact_detail(session: Session, contact_id: uuid.UUID) -> ContactDetailView | None:
    """Assemble the contact detail page, or ``None`` when no such contact."""

    contact = session.get(Contact, contact_id)
    if contact is None:
        return None

    subject = crm_annotations.Subject(contact=contact)
    view = ContactDetailView(contact=contact, states=states_for_contact(session, contact))
    view.labels = crm_annotations.labels_for(session, subject)
    view.notes = crm_annotations.notes_for(session, subject)

    captures = list(
        session.scalars(
            select(LinkedInProfileSnapshot)
            .where(LinkedInProfileSnapshot.matched_contact_id == contact_id)
            .order_by(LinkedInProfileSnapshot.ingested_at.desc())
        ).all()
    )
    view.captures = [_evidence_from(snapshot) for snapshot in captures]

    # Employment comes from the most recent capture that carried any. A contact
    # imported from a spreadsheet has none, and shows an empty section rather
    # than a fabricated single role built from its own columns.
    latest = latest_capture_for_contact(session, contact_id)
    if latest is not None:
        view.employment = _employment_from(list(latest.experiences))

    view.company = _company_link_for_contact(session, contact, latest)

    view.field_provenance = [
        FieldProvenance(
            field_name=row.field_name,
            value=row.value,
            source_name=row.source_name,
            observed_at=row.observed_at,
            confidence=row.confidence,
            policy_version=row.policy_version,
            decision_reason=row.decision_reason,
            is_manual_override=row.is_manual_override,
        )
        for row in session.scalars(
            select(ContactFieldValue)
            .where(
                ContactFieldValue.contact_id == contact_id,
                ContactFieldValue.is_current_winner.is_(True),
            )
            .order_by(ContactFieldValue.field_name.asc())
        ).all()
    ]

    view.qa_evaluations = list(
        session.scalars(
            select(ContactQAEvaluation)
            .where(ContactQAEvaluation.contact_id == contact_id)
            .order_by(ContactQAEvaluation.evaluated_at.desc())
            .limit(10)
        ).all()
    )

    # Outer join as defence in depth. campaign_id is NOT NULL with CASCADE, so
    # an orphaned membership cannot arise today; the outer join means this page
    # would degrade to "campaign missing" rather than silently omit history if
    # that ever changed. A contact with no membership at all is the normal,
    # expected case and is not an error.
    view.memberships = [
        (membership, campaign)
        for membership, campaign in session.execute(
            select(CampaignContact, Campaign)
            .outerjoin(Campaign, Campaign.id == CampaignContact.campaign_id)
            .where(CampaignContact.contact_id == contact_id)
            .order_by(CampaignContact.created_at.desc())
        ).all()
    ]
    return view


def _company_link_for_contact(
    session: Session,
    contact: Contact,
    latest_capture: LinkedInProfileSnapshot | None,
) -> CompanyLink:
    """Resolve a contact's employer to a canonical company where possible.

    Prefer the permanent company edge, then an exact domain. An unresolved
    Contact truthfully has neither and remains valid but downstream-blocked.
    """

    company = session.get(Company, contact.company_id) if contact.company_id is not None else None
    if company is None and contact.company_domain:
        company = session.scalars(
            select(Company).where(Company.domain == contact.company_domain).limit(1)
        ).first()

    current = current_experience(latest_capture) if latest_capture is not None else None
    if company is not None:
        note = "Linked to a canonical company by exact domain match."
    elif contact.company_domain:
        note = (
            f"No company record exists for {contact.company_domain!r} yet. "
            "The contact is unaffected — the company workspace is a later stage."
        )
    else:
        note = (
            "No company domain has been observed or approved yet. The permanent "
            "Contact is preserved while Company resolution waits."
        )

    return CompanyLink(
        company=company,
        captured_name=current.company_name if current is not None else contact.company_name,
        domain=contact.company_domain,
        linkedin_company_url=current.company_linkedin_url if current is not None else None,
        linkedin_company_id=current.company_linkedin_id if current is not None else None,
        is_resolved=company is not None,
        resolution_note=note,
        # Read through the permanent edge, not through the domain string above:
        # a decision belongs to the company a contact is actually linked to.
        domain_resolution=resolution_service.contact_view(session, contact),
    )


def get_capture_detail(session: Session, capture_id: uuid.UUID) -> CaptureDetailView | None:
    """Assemble the pending-capture detail page, or ``None`` when no such capture."""

    capture = session.get(LinkedInProfileSnapshot, capture_id)
    if capture is None:
        return None

    subject = crm_annotations.Subject(capture=capture)
    view = CaptureDetailView(capture=capture, states=states_for_capture(session, capture))
    view.labels = crm_annotations.labels_for(session, subject)
    view.notes = crm_annotations.notes_for(session, subject)
    view.employment = _employment_from(list(capture.experiences))
    view.evidence = _evidence_from(capture)
    view.company = _company_link_for_capture(capture)

    if capture.submission_id is not None:
        view.submission = session.get(ContactCaptureSubmission, capture.submission_id)

    # For an ambiguous capture, show the contacts it could be. The backend never
    # picks between them on a name or a company — only an exact normalized
    # profile URL may match automatically — so this list is for the operator to
    # decide from, not a ranking to accept.
    if capture.outcome == LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW:
        candidate_ids = [
            uuid.UUID(str(entry.get("contact_id")))
            for entry in (capture.review_candidates or [])
            if isinstance(entry, dict) and entry.get("contact_id")
        ]
        if candidate_ids:
            view.candidate_contacts = list(
                session.scalars(select(Contact).where(Contact.id.in_(candidate_ids))).all()
            )
    return view


def _company_link_for_capture(capture: LinkedInProfileSnapshot) -> CompanyLink:
    """What is known about a pending capture's employer.

    By definition there is no resolved domain — that is the reason this record
    is still a capture. The note names the actual next step rather than leaving
    a blank, so the operator knows the queue is waiting on domain resolution and
    not on them.
    """

    current = current_experience(capture)
    captured_name = current.company_name if current is not None else None

    if captured_name:
        note = (
            f"Captured as {captured_name!r}. No company domain has been resolved yet, "
            "so this person is not a canonical contact. Domain resolution runs through "
            "the logo.dev candidate flow with an operator confirmation."
        )
    else:
        note = (
            "No current employer was captured for this person, so there is nothing "
            "to resolve a company domain from yet."
        )

    return CompanyLink(
        company=None,
        captured_name=captured_name,
        domain=None,
        linkedin_company_url=current.company_linkedin_url if current is not None else None,
        linkedin_company_id=current.company_linkedin_id if current is not None else None,
        is_resolved=False,
        resolution_note=note,
    )
