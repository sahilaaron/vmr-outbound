"""Exact, read-only Research lineage for an Insights execution.

The Company's currently selected dossier is deliberately never a fallback.  A
job either names (or has an ancestor that names) the Research execution whose
committed submission and dossier it consumed, or that historical fact is
unavailable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company_dossier import CompanyDossierVersion, CompanyResearchSubmission
from app.models.enums import AgentIdentifier, AgentJobStatus
from app.models.verification_job import AgentJob

MAX_ANCESTORS = 16


@dataclass(frozen=True)
class ResearchLineage:
    research_job: AgentJob
    submission: CompanyResearchSubmission
    dossier: CompanyDossierVersion


def _uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _research_ancestor(session: Session, start: AgentJob | None) -> AgentJob | None:
    current = start
    seen: set[uuid.UUID] = set()
    for _ in range(MAX_ANCESTORS):
        if current is None or current.id in seen:
            return None
        seen.add(current.id)
        if current.agent_id is AgentIdentifier.RESEARCH:
            return current
        current = session.get(AgentJob, current.parent_job_id) if current.parent_job_id else None
    return None


def _from_research_job(
    session: Session,
    *,
    research_job: AgentJob,
    company_id: uuid.UUID,
    campaign_id: uuid.UUID | None,
    campaign_contact_id: uuid.UUID | None,
    contact_id: uuid.UUID | None,
) -> ResearchLineage | None:
    if (
        research_job.agent_id is not AgentIdentifier.RESEARCH
        or research_job.status is not AgentJobStatus.SUCCEEDED
        or research_job.campaign_id != campaign_id
        or research_job.campaign_contact_id != campaign_contact_id
        or research_job.contact_id != contact_id
    ):
        return None
    result = research_job.result or {}
    if _uuid(result.get("company_id")) != company_id:
        return None
    submission_id = _uuid(result.get("submission_id"))
    version_number = result.get("dossier_version")
    if submission_id is None or not isinstance(version_number, int):
        return None
    submission = session.get(CompanyResearchSubmission, submission_id)
    if submission is None or submission.company_id != company_id:
        return None
    dossier = session.scalars(
        select(CompanyDossierVersion).where(
            CompanyDossierVersion.company_id == company_id,
            CompanyDossierVersion.submission_id == submission.id,
            CompanyDossierVersion.version_number == version_number,
        )
    ).one_or_none()
    if dossier is None:
        return None
    return ResearchLineage(research_job=research_job, submission=submission, dossier=dossier)


def resolve(
    session: Session,
    *,
    insights_job: AgentJob,
    company_id: uuid.UUID,
) -> ResearchLineage | None:
    """Resolve only the exact Research artifacts used by one Insights job."""

    if insights_job.agent_id is not AgentIdentifier.INSIGHTS:
        return None
    reference = insights_job.input_reference or {}
    if "research_job_id" in reference:
        pinned_id = _uuid(reference.get("research_job_id"))
        research_job = session.get(AgentJob, pinned_id) if pinned_id else None
    elif isinstance(insights_job.result, dict) and "research_job_id" in insights_job.result:
        pinned_id = _uuid(insights_job.result.get("research_job_id"))
        research_job = session.get(AgentJob, pinned_id) if pinned_id else None
    else:
        parent = (
            session.get(AgentJob, insights_job.parent_job_id)
            if insights_job.parent_job_id
            else None
        )
        research_job = _research_ancestor(session, parent)
    if research_job is None:
        return None
    lineage = _from_research_job(
        session,
        research_job=research_job,
        company_id=company_id,
        campaign_id=insights_job.campaign_id,
        campaign_contact_id=insights_job.campaign_contact_id,
        contact_id=insights_job.contact_id,
    )
    if lineage is None:
        return None

    # New jobs carry redundant immutable pins.  If any is present it must agree;
    # a malformed or cross-linked pin is an unavailable lineage, never a cue to
    # attach the Company's latest dossier.
    expected: dict[str, uuid.UUID] = {
        "research_submission_id": lineage.submission.id,
        "research_dossier_version_id": lineage.dossier.id,
    }
    for source in (reference, insights_job.result or {}):
        for key, authoritative in expected.items():
            if key in source and _uuid(source.get(key)) != authoritative:
                return None
        if (
            "research_dossier_version" in source
            and source.get("research_dossier_version") != lineage.dossier.version_number
        ):
            return None
        if (
            "dossier_version" in source
            and source.get("dossier_version") != lineage.dossier.version_number
        ):
            return None
    return lineage


def pins_from_ancestor(
    session: Session,
    *,
    parent_job: AgentJob | None,
    company_id: uuid.UUID,
) -> dict[str, object]:
    """Build immutable lineage pins while a new Insights job is queued."""

    research_job = _research_ancestor(session, parent_job)
    if research_job is None:
        return {}
    lineage = _from_research_job(
        session,
        research_job=research_job,
        company_id=company_id,
        campaign_id=research_job.campaign_id,
        campaign_contact_id=research_job.campaign_contact_id,
        contact_id=research_job.contact_id,
    )
    if lineage is None:
        return {}
    return {
        "research_job_id": str(lineage.research_job.id),
        "research_submission_id": str(lineage.submission.id),
        "research_dossier_version_id": str(lineage.dossier.id),
        "research_dossier_version": lineage.dossier.version_number,
    }
