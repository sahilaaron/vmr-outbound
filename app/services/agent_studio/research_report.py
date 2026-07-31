"""Stable read model boundary for the read-only Research Agent report.

The current reader projects only facts already persisted by RES-001.  The
independent GLM branch can replace this reader behind the protocol or populate
the same dataclasses with richer attempt-level detail.  Missing persistence is
represented as ``None`` plus an explicit unavailable label; console logs are
never treated as durable observability.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.company_dossier import CompanyDossierVersion, CompanyResearchSubmission
from app.models.contact import Contact
from app.models.enums import AgentIdentifier
from app.models.insight import Insight, InsightEvidence
from app.models.verification_job import AgentJob
from app.services.agents import jobs as agent_jobs
from app.services.resolution import store as resolution_store
from app.services.workbench_agents.sanitize import sanitize_mapping, sanitize_text


@dataclass(frozen=True)
class ResearchSourceRead:
    url: str
    title: str | None
    retrieved_at: str | None
    retrieval_method: str | None


@dataclass(frozen=True)
class ResearchFactView:
    claim: str
    source_urls: tuple[str, ...]
    confidence: float | None


@dataclass(frozen=True)
class ResearchRetryView:
    job_id: uuid.UUID
    public_status: str
    attempts: int
    max_attempts: int
    error_type: str | None
    error_detail: str | None
    created_at: datetime


@dataclass(frozen=True)
class ResearchReport:
    campaign_contact_id: uuid.UUID
    campaign_id: uuid.UUID
    campaign_name: str
    contact_id: uuid.UUID
    contact_label: str
    company_id: uuid.UUID | None
    company_name: str | None
    domain: str | None
    domain_state: str | None
    job_id: uuid.UUID | None
    job_status: str | None
    worker_identity: tuple[str, ...]
    attempts: int | None
    max_attempts: int | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None
    urls_attempted: tuple[str, ...] | None
    successful_reads: tuple[ResearchSourceRead, ...] | None
    collection_failures: tuple[str, ...] | None
    submission_id: uuid.UUID | None
    dossier_version: int | None
    sourced_facts: tuple[ResearchFactView, ...] | None
    rejected_evidence: tuple[str, ...] | None
    retry_history: tuple[ResearchRetryView, ...]
    final_outcome: str | None
    error_type: str | None
    error_detail: str | None
    unavailable: tuple[str, ...]


@runtime_checkable
class ResearchReportReader(Protocol):
    def read(self, campaign_contact_id: uuid.UUID) -> ResearchReport | None: ...


_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:\\|[a-z]:/)[^\s,;]+")
_UNIX_PATH = re.compile(r"(?<!https:)(?<!http:)(?:/root|/home|/workspace|/tmp)/[^\s,;]+")


def _safe_text(value: str | None) -> str | None:
    cleaned = sanitize_text(value, limit=4_000)
    if cleaned is None:
        return None
    return _UNIX_PATH.sub("[local path]", _WINDOWS_PATH.sub("[local path]", cleaned))


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        # User information, query strings and fragments can carry credentials.
        return urlunsplit((parsed.scheme, host, parsed.path or "/", "", ""))
    except (ValueError, UnicodeError):
        return None


def _contact_label(contact: Contact) -> str:
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part)
    return name or contact.email or str(contact.id)


class PersistedResearchReportReader:
    """Read the current RES-001 tables without inventing missing detail."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _submission(
        self, company_id: uuid.UUID, job_id: uuid.UUID | None
    ) -> CompanyResearchSubmission | None:
        submissions = self._session.scalars(
            select(CompanyResearchSubmission)
            .where(CompanyResearchSubmission.company_id == company_id)
            .order_by(CompanyResearchSubmission.submitted_at.desc())
        ).all()
        if job_id is None:
            return submissions[0] if submissions else None
        expected = str(job_id)
        return next(
            (
                item
                for item in submissions
                if isinstance(item.request_context, dict)
                and item.request_context.get("agent_job_id") == expected
            ),
            None,
        )

    def read(self, campaign_contact_id: uuid.UUID) -> ResearchReport | None:
        membership = self._session.get(CampaignContact, campaign_contact_id)
        if membership is None:
            return None
        campaign = self._session.get(Campaign, membership.campaign_id)
        contact = self._session.get(Contact, membership.contact_id)
        if campaign is None or contact is None:
            return None
        company = self._session.get(Company, contact.company_id) if contact.company_id else None
        jobs = tuple(
            self._session.scalars(
                select(AgentJob)
                .where(
                    AgentJob.campaign_contact_id == membership.id,
                    AgentJob.agent_id == AgentIdentifier.RESEARCH,
                )
                .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
            ).all()
        )
        job = jobs[0] if jobs else None
        submission = self._submission(company.id, job.id if job else None) if company else None
        dossier = None
        if submission is not None:
            dossier = self._session.scalars(
                select(CompanyDossierVersion)
                .where(CompanyDossierVersion.submission_id == submission.id)
                .order_by(CompanyDossierVersion.version_number.desc())
            ).first()

        raw_workers: list[dict[str, Any]] = []
        if submission and isinstance(submission.payload, dict):
            value = submission.payload.get("workers")
            raw_workers = (
                [item for item in value if isinstance(item, dict)]
                if isinstance(value, list)
                else []
            )
        reads: list[ResearchSourceRead] = []
        attempted: list[str] = []
        failures: list[str] = []
        workers: list[str] = []
        for worker in raw_workers:
            worker_name = _safe_text(str(worker.get("worker") or "unknown")) or "unknown"
            worker_version = _safe_text(str(worker.get("worker_version") or "unknown")) or "unknown"
            workers.append(f"{worker_name}/{worker_version}")
            raw = worker.get("raw") if isinstance(worker.get("raw"), dict) else {}
            pages = raw.get("pages") if isinstance(raw, dict) else None
            for page in pages if isinstance(pages, list) else []:
                if not isinstance(page, dict) or not isinstance(page.get("url"), str):
                    continue
                url = _safe_url(page["url"])
                if url is None:
                    failures.append("A non-HTTP or credential-bearing source URL was omitted.")
                    continue
                attempted.append(url)
                reads.append(
                    ResearchSourceRead(
                        url=url,
                        title=_safe_text(str(page.get("title"))) if page.get("title") else None,
                        retrieved_at=(
                            str(page.get("retrieved_at")) if page.get("retrieved_at") else None
                        ),
                        retrieval_method=(
                            str(page.get("retrieval_method"))
                            if page.get("retrieval_method")
                            else None
                        ),
                    )
                )
            errors = raw.get("errors") if isinstance(raw, dict) else None
            for error in errors if isinstance(errors, list) else []:
                failures.append(_safe_text(str(error)) or "Unknown collection failure")

        facts: list[ResearchFactView] = []
        if job is not None:
            insights = self._session.scalars(
                select(Insight).where(Insight.idempotency_key.like(f"research:{job.id}:%"))
            ).all()
            for insight in insights:
                evidence = self._session.scalars(
                    select(InsightEvidence).where(InsightEvidence.insight_id == insight.id)
                ).all()
                confidence = min(
                    (float(item.confidence) for item in evidence if item.confidence is not None),
                    default=None,
                )
                facts.append(
                    ResearchFactView(
                        claim=_safe_text(insight.claim) or "",
                        source_urls=tuple(
                            safe
                            for item in evidence
                            if (safe := _safe_url(item.source_url)) is not None
                        ),
                        confidence=confidence,
                    )
                )

        rejected: list[str] = []
        if dossier and dossier.warnings:
            rejected.extend(_safe_text(str(item)) or "Unknown warning" for item in dossier.warnings)
        error_mapping = sanitize_mapping(dict(job.error)) if job and job.error else None
        error_detail = _safe_text(
            job.last_error
            if job and job.last_error
            else str(error_mapping.get("message"))
            if error_mapping and error_mapping.get("message")
            else None
        )
        retry_history = tuple(
            ResearchRetryView(
                job_id=item.id,
                public_status=agent_jobs.public_status(item),
                attempts=item.attempts,
                max_attempts=item.max_attempts,
                error_type=_safe_text(item.error_class),
                error_detail=_safe_text(item.last_error),
                created_at=item.created_at,
            )
            for item in jobs
        )
        unavailable: list[str] = []
        if job is None:
            unavailable.append("No persisted Research Agent Job exists for this Campaign Contact.")
        if submission is None:
            unavailable.append("No raw Research submission is linked to the selected run.")
        if dossier is None:
            unavailable.append("No dossier version is linked to the selected run.")
        if not raw_workers:
            unavailable.append("Worker-level collection detail was not persisted for this run.")
        unavailable.append(
            "Attempt-by-attempt lease transitions are not persisted as a Research report ledger."
        )
        duration = None
        if job and job.started_at and job.finished_at:
            duration = max(0.0, (job.finished_at - job.started_at).total_seconds())
        domain_state = (
            resolution_store.company_state(self._session, company.id) if company else None
        )
        result = dict(job.result or {}) if job else {}
        final = (
            str(result.get("domain_outcome"))
            if result.get("domain_outcome") is not None
            else agent_jobs.public_status(job)
            if job
            else None
        )
        return ResearchReport(
            campaign_contact_id=membership.id,
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            contact_id=contact.id,
            contact_label=_contact_label(contact),
            company_id=company.id if company else None,
            company_name=company.name if company else None,
            domain=company.domain if company else contact.company_domain,
            domain_state=domain_state.value if domain_state else None,
            job_id=job.id if job else None,
            job_status=agent_jobs.public_status(job) if job else None,
            worker_identity=tuple(workers),
            attempts=job.attempts if job else None,
            max_attempts=job.max_attempts if job else None,
            started_at=job.started_at if job else None,
            finished_at=job.finished_at if job else None,
            duration_seconds=duration,
            urls_attempted=tuple(dict.fromkeys(attempted)) if raw_workers else None,
            successful_reads=tuple(reads) if raw_workers else None,
            collection_failures=tuple(failures) if raw_workers else None,
            submission_id=submission.id if submission else None,
            dossier_version=dossier.version_number if dossier else None,
            sourced_facts=tuple(facts) if job else None,
            rejected_evidence=tuple(rejected) if dossier else None,
            retry_history=retry_history,
            final_outcome=final,
            error_type=_safe_text(job.error_class) if job else None,
            error_detail=error_detail,
            unavailable=tuple(unavailable),
        )
