"""Read models backing the operator verification surfaces (EML-006 / VER-006 / UI).

Every number here is queried from the local database — no simulated or placeholder
values. Business rules stay in the service layer; the templates only render.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import VerificationJobStatus
from app.models.verification_job import VerificationJob
from app.models.verification_usage import VerificationUsageEvent
from app.services.verification import usage as usage_service
from app.services.verification.status import StatusView, derive_status_for_contact


@dataclass
class ContactEmailIntel:
    """Everything the contact page shows about email intelligence + verification."""

    status: StatusView
    candidates: list[EmailCandidate] = field(default_factory=list)
    selected: EmailCandidate | None = None
    evidence: list[ExactEmailVerification] = field(default_factory=list)
    jobs: list[VerificationJob] = field(default_factory=list)


def contact_email_intel(session: Session, contact: Contact) -> ContactEmailIntel:
    candidates = list(
        session.scalars(
            select(EmailCandidate)
            .where(EmailCandidate.contact_id == contact.id)
            .order_by(EmailCandidate.rank)
        ).all()
    )
    selected = next((c for c in candidates if c.selected), None)
    status = derive_status_for_contact(session, contact)

    evidence: list[ExactEmailVerification] = []
    jobs: list[VerificationJob] = []
    if status.email:
        evidence = list(
            session.scalars(
                select(ExactEmailVerification)
                .where(ExactEmailVerification.email == status.email)
                .order_by(ExactEmailVerification.checked_at.desc())
            ).all()
        )
        jobs = list(
            session.scalars(
                select(VerificationJob)
                .where(VerificationJob.email == status.email)
                .order_by(VerificationJob.created_at.desc())
            ).all()
        )
    return ContactEmailIntel(
        status=status, candidates=candidates, selected=selected, evidence=evidence, jobs=jobs
    )


def statuses_for_contacts(session: Session, contacts: list[Contact]) -> dict[uuid.UUID, StatusView]:
    """Status view per contact for a list page (pilot-scale N queries)."""

    return {c.id: derive_status_for_contact(session, c) for c in contacts}


@dataclass
class VerificationConsole:
    """Aggregates for the verification operator page."""

    usage: usage_service.UsageSummary
    job_counts: dict[str, int] = field(default_factory=dict)
    recent_jobs: list[VerificationJob] = field(default_factory=list)
    recent_events: list[VerificationUsageEvent] = field(default_factory=list)
    total_jobs: int = 0
    runnable_jobs: int = 0
    reclaimable_jobs: int = 0


def load_console(session: Session) -> VerificationConsole:
    counts_rows = session.execute(
        select(VerificationJob.status, func.count()).group_by(VerificationJob.status)
    ).all()
    job_counts = {status.value: int(count) for status, count in counts_rows}
    total_jobs = sum(job_counts.values())
    runnable = job_counts.get(VerificationJobStatus.PENDING.value, 0) + job_counts.get(
        VerificationJobStatus.RETRY_SCHEDULED.value, 0
    )
    recent_jobs = list(
        session.scalars(
            select(VerificationJob).order_by(VerificationJob.updated_at.desc()).limit(25)
        ).all()
    )
    return VerificationConsole(
        usage=usage_service.usage_summary(session),
        job_counts=job_counts,
        recent_jobs=recent_jobs,
        recent_events=usage_service.recent_events(session, limit=25),
        total_jobs=total_jobs,
        runnable_jobs=runnable,
        reclaimable_jobs=job_counts.get(VerificationJobStatus.IN_PROGRESS.value, 0),
    )
