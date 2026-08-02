"""Durable, read-only Company Research execution reports.

The report is a projection over the existing RES-001 system of record.  A job's
persisted result is the authoritative link to its submission and dossier.  The
submission request context is only a compatibility fallback: identical raw
payloads are deliberately deduplicated and a later run can therefore reuse a
submission whose original context names an earlier job.

Only bounded, typed fields cross this boundary.  Raw job input/result/error,
raw worker output, environment values, console logs and model reasoning never
do.  Missing observability is reported explicitly instead of reconstructed.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.company_dossier import CompanyDossierVersion, CompanyResearchSubmission
from app.models.contact import Contact
from app.models.enums import AgentIdentifier, AgentJobStatus, PipelineEventType
from app.models.insight import Insight, InsightEvidence
from app.models.pipeline import CampaignContactAgentState, PipelineEvent
from app.models.verification_job import AgentJob
from app.services.agents import jobs as agent_jobs
from app.services.imports.normalization import is_valid_hostname, normalize_domain
from app.services.resolution import store as resolution_store
from app.services.workbench_agents.sanitize import sanitize_mapping, sanitize_text


class ResearchReportState(enum.StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ResearchSourceRead:
    url: str
    title: str | None
    retrieved_at: str | None
    retrieval_method: str | None
    page_type: str | None = None
    research_worker: str | None = None


@dataclass(frozen=True)
class ResearchCollectionFailure:
    error: str
    url: str | None = None
    stage: str | None = None
    occurred_at: str | None = None
    research_worker: str | None = None


@dataclass(frozen=True)
class ResearchEvidenceView:
    evidence_id: uuid.UUID
    source_url: str
    source_title: str | None
    published_at: datetime | None
    retrieved_at: datetime | None
    confidence: float | None
    extraction_method: str | None
    freshness_at: datetime | None
    source_record_type: str | None
    source_record_id: uuid.UUID | None
    version: int


@dataclass(frozen=True)
class ResearchFactView:
    claim: str
    source_urls: tuple[str, ...]
    confidence: float | None
    insight_id: uuid.UUID | None = None
    kind: str | None = None
    state: str | None = None
    version: int | None = None
    evidence: tuple[ResearchEvidenceView, ...] = ()


@dataclass(frozen=True)
class ResearchRetryView:
    """One related Research job.

    Retries increment ``attempts`` on the same job.  Separate rows represent
    later queue generations, including operator re-runs; they are not called
    retry attempts in the UI.
    """

    job_id: uuid.UUID
    public_status: str
    attempts: int
    max_attempts: int
    error_type: str | None
    error_detail: str | None
    created_at: datetime
    selected: bool = False
    generation: int | None = None
    retryable_error: bool | None = None
    next_run_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class ResearchJobEventView:
    event_type: str
    occurred_at: datetime
    actor: str
    attempt: int | None
    from_status: str | None
    to_status: str | None
    reason_code: str | None
    reason_detail: str | None
    retryable: bool


@dataclass(frozen=True)
class ResearchSubmissionView:
    submission_id: uuid.UUID
    company_id: uuid.UUID
    producer: str
    producer_version: str | None
    submitted_at: datetime
    link_source: str


@dataclass(frozen=True)
class ResearchDossierView:
    dossier_id: uuid.UUID
    version_number: int
    is_current: bool
    status: str
    interpreter: str
    interpreter_version: str | None
    created_at: datetime
    sections_present: tuple[str, ...]


@dataclass(frozen=True)
class ResearchDomainResolutionView:
    scope: str
    capture_id: uuid.UUID | None
    state: str
    selected_domain: str | None
    decision_kind: str | None
    policy_version: str | None
    decided_at: datetime | None


@dataclass(frozen=True)
class ResearchReport:
    # Existing Agent Studio template contract.  New durable detail is appended
    # below so callers constructing the original frozen shape stay compatible.
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
    report_state: ResearchReportState = ResearchReportState.PARTIAL
    report_reason: str = "Only part of this execution is durably available."
    job_created_at: datetime | None = None
    job_updated_at: datetime | None = None
    next_run_at: datetime | None = None
    retryable_error: bool | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    lease_state: str | None = None
    execution_workers: tuple[str, ...] = ()
    pipeline_status: str | None = None
    pipeline_reason: str | None = None
    domain_resolution: ResearchDomainResolutionView | None = None
    submission: ResearchSubmissionView | None = None
    dossier: ResearchDossierView | None = None
    collection_failure_details: tuple[ResearchCollectionFailure, ...] | None = None
    warnings: tuple[str, ...] = ()
    job_events: tuple[ResearchJobEventView, ...] = ()
    selection_reason: str | None = None


@runtime_checkable
class ResearchReportReader(Protocol):
    def read(self, campaign_contact_id: uuid.UUID) -> ResearchReport | None: ...

    def read_job(self, agent_job_id: uuid.UUID) -> ResearchReport | None: ...


_WINDOWS_PATH = re.compile(r"(?i)(?<![a-z0-9+.-])(?:[a-z]:\\|[a-z]:/|\\\\[^\s\\]+\\)[^\s,;]+")
_UNIX_PATH = re.compile(
    r"(?<!https:)(?<!http:)(?:/root|/home|/Users|/workspace|/tmp|/var|/etc|/opt|/srv|"
    r"/mnt|/private|/app)/[^\s,;]+"
)
_ENV_SECRET = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIALS?|"
    r"AUTHORIZATION|DATABASE_URL|DSN))\s*=\s*[^\s,;]+"
)
_GENERATION = re.compile(r"(?:^|:)v(?P<generation>[1-9][0-9]*)(?:$|:)")
_DOSSIER_SECTION_FIELDS = (
    "overview",
    "products_services",
    "industries",
    "geography",
    "leadership",
    "activity_signals",
    "public_contacts",
    "sources",
    "unknowns",
)
_EXECUTION_EVENT_TYPES = frozenset({PipelineEventType.JOB_LEASED, PipelineEventType.JOB_STARTED})


def _safe_text(value: str | None, *, limit: int = 4_000) -> str | None:
    cleaned = sanitize_text(value, limit=limit)
    if cleaned is None:
        return None
    cleaned = _ENV_SECRET.sub(lambda match: f"{match.group(1)}=[redacted]", cleaned)
    return _UNIX_PATH.sub("[local path]", _WINDOWS_PATH.sub("[local path]", cleaned))


def _safe_string(value: object, *, limit: int = 4_000) -> str | None:
    """Sanitize scalar metadata without stringifying raw structures."""

    if isinstance(value, str):
        return _safe_text(value, limit=limit)
    if isinstance(value, (bool, int, float)):
        return _safe_text(str(value), limit=limit)
    return None


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        # User information, query strings and fragments can carry credentials.
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", "", ""))
    except (ValueError, UnicodeError):
        return None


def _safe_domain(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = normalize_domain(value)
    return normalized if normalized and is_valid_hostname(normalized) else None


def _contact_label(contact: Contact) -> str:
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part)
    return name or contact.email or str(contact.id)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _retryable_error(job: AgentJob) -> bool | None:
    raw = _mapping(job.error).get("retryable")
    return raw if isinstance(raw, bool) else None


def _job_error(job: AgentJob) -> tuple[str | None, str | None]:
    sanitized = sanitize_mapping(dict(job.error)) if job.error else None
    mapped = _mapping(sanitized)
    message = mapped.get("message")
    detail = job.last_error or (message if isinstance(message, str) else None)
    return _safe_text(job.error_class), _safe_text(detail)


def _generation(job: AgentJob) -> int | None:
    match = _GENERATION.search(job.idempotency_key)
    return int(match.group("generation")) if match else None


class DurableResearchReportReader:
    """Project the durable RES-001 records without issuing any writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def read(self, campaign_contact_id: uuid.UUID) -> ResearchReport | None:
        with self._session.no_autoflush:
            return self._read(campaign_contact_id)

    def _read(self, campaign_contact_id: uuid.UUID) -> ResearchReport | None:
        context = self._membership_context(campaign_contact_id)
        if context is None:
            return None
        membership, campaign, contact = context
        all_jobs = self._related_jobs(membership.id)
        jobs = tuple(item for item in all_jobs if self._job_belongs_to_membership(item, membership))
        # Never skip a malformed newest row and make an older execution look
        # current.  A cross-owner latest row makes the natural report
        # unavailable; the exact-job API also refuses that row.
        job = (
            all_jobs[0]
            if all_jobs and self._job_belongs_to_membership(all_jobs[0], membership)
            else None
        )
        return self._build(
            membership=membership,
            campaign=campaign,
            contact=contact,
            job=job,
            related_jobs=jobs,
            selection_reason="Latest persisted Research job for this Campaign Contact.",
            missing_job_reason=(
                "The newest persisted Research job has conflicting Campaign Contact "
                "ownership and was withheld."
                if all_jobs and job is None
                else None
            ),
        )

    def read_job(self, agent_job_id: uuid.UUID) -> ResearchReport | None:
        with self._session.no_autoflush:
            return self._read_job(agent_job_id)

    def _read_job(self, agent_job_id: uuid.UUID) -> ResearchReport | None:
        job = self._session.get(AgentJob, agent_job_id)
        if (
            job is None
            or job.agent_id is not AgentIdentifier.RESEARCH
            or job.campaign_contact_id is None
        ):
            return None
        context = self._membership_context(job.campaign_contact_id)
        if context is None:
            return None
        membership, campaign, contact = context
        # A malformed cross-owner job is not allowed to borrow another
        # Campaign Contact's identity just because its membership FK resolves.
        if not self._job_belongs_to_membership(job, membership):
            return None
        return self._build(
            membership=membership,
            campaign=campaign,
            contact=contact,
            job=job,
            related_jobs=tuple(
                item
                for item in self._related_jobs(membership.id)
                if self._job_belongs_to_membership(item, membership)
            ),
            selection_reason="Selected by persisted Research Agent Job identifier.",
        )

    def _membership_context(
        self, campaign_contact_id: uuid.UUID
    ) -> tuple[CampaignContact, Campaign, Contact] | None:
        membership = self._session.get(CampaignContact, campaign_contact_id)
        if membership is None:
            return None
        campaign = self._session.get(Campaign, membership.campaign_id)
        contact = self._session.get(Contact, membership.contact_id)
        if campaign is None or contact is None:
            return None
        return membership, campaign, contact

    def _related_jobs(self, campaign_contact_id: uuid.UUID) -> tuple[AgentJob, ...]:
        return tuple(
            self._session.scalars(
                select(AgentJob)
                .where(
                    AgentJob.campaign_contact_id == campaign_contact_id,
                    AgentJob.agent_id == AgentIdentifier.RESEARCH,
                )
                .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
            ).all()
        )

    @staticmethod
    def _job_belongs_to_membership(job: AgentJob, membership: CampaignContact) -> bool:
        return (
            (job.campaign_id is None or job.campaign_id == membership.campaign_id)
            and (job.contact_id is None or job.contact_id == membership.contact_id)
            and job.campaign_contact_id == membership.id
        )

    def _submission_for_job(
        self, job: AgentJob, result: Mapping[str, Any]
    ) -> tuple[CompanyResearchSubmission | None, str | None]:
        raw_submission_id = result.get("submission_id")
        if raw_submission_id is not None:
            submission_id = _uuid(raw_submission_id)
            if submission_id is None:
                return None, "invalid job result"
            return self._session.get(CompanyResearchSubmission, submission_id), "job result"
        expected = str(job.id)
        statement = select(CompanyResearchSubmission).where(
            CompanyResearchSubmission.request_context["agent_job_id"].as_string() == expected
        )
        expected_company_id = _uuid(result.get("company_id")) or job.company_id
        if expected_company_id is not None:
            statement = statement.where(CompanyResearchSubmission.company_id == expected_company_id)
        submission = self._session.scalars(
            statement.order_by(
                CompanyResearchSubmission.submitted_at.desc(),
                CompanyResearchSubmission.id.desc(),
            )
        ).first()
        return submission, "legacy request context" if submission is not None else None

    def _dossier_for_job(
        self,
        *,
        result: Mapping[str, Any],
        submission: CompanyResearchSubmission | None,
        company_id: uuid.UUID | None,
    ) -> CompanyDossierVersion | None:
        version_number = _integer(result.get("dossier_version"))
        if submission is None or company_id is None or version_number is None:
            return None
        return self._session.scalars(
            select(CompanyDossierVersion).where(
                CompanyDossierVersion.company_id == company_id,
                CompanyDossierVersion.submission_id == submission.id,
                CompanyDossierVersion.version_number == version_number,
            )
        ).first()

    def _domain_resolution(
        self,
        *,
        membership: CampaignContact,
        job: AgentJob | None,
        company_id: uuid.UUID | None,
    ) -> ResearchDomainResolutionView | None:
        capture_id = job.capture_id if job and job.capture_id else membership.source_capture_id
        if capture_id is not None:
            decision = resolution_store.current_decision(self._session, capture_id)
            if decision is not None:
                return self._domain_decision(
                    decision, scope="current decision for execution capture"
                )
        if company_id is None:
            return None
        decisions = resolution_store.current_decisions_for_company(self._session, company_id)
        if not decisions:
            return None
        return self._domain_decision(decisions[0], scope="current Company aggregate")

    @staticmethod
    def _domain_decision(
        decision: CompanyDomainResolution, *, scope: str
    ) -> ResearchDomainResolutionView:
        return ResearchDomainResolutionView(
            scope=scope,
            capture_id=decision.capture_id,
            state=decision.state.value,
            selected_domain=_safe_domain(decision.selected_domain),
            decision_kind=decision.decision_kind.value,
            policy_version=_safe_text(decision.policy_version, limit=128),
            decided_at=decision.decided_at,
        )

    def _events(self, job: AgentJob) -> tuple[tuple[ResearchJobEventView, ...], tuple[str, ...]]:
        rows = self._session.scalars(
            select(PipelineEvent)
            .where(PipelineEvent.job_id == job.id)
            .order_by(PipelineEvent.occurred_at.asc(), PipelineEvent.id.asc())
        ).all()
        events: list[ResearchJobEventView] = []
        workers: list[str] = []
        for row in rows:
            detail = _mapping(row.detail)
            attempt = _integer(detail.get("attempt"))
            actor = _safe_text(row.actor, limit=256) or "[unavailable]"
            worker = detail.get("worker_id")
            safe_worker = _safe_text(worker, limit=256) if isinstance(worker, str) else actor
            if row.event_type in _EXECUTION_EVENT_TYPES and safe_worker:
                workers.append(safe_worker)
            events.append(
                ResearchJobEventView(
                    event_type=row.event_type.value,
                    occurred_at=row.occurred_at,
                    actor=actor,
                    attempt=attempt,
                    from_status=row.from_status.value if row.from_status else None,
                    to_status=row.to_status.value if row.to_status else None,
                    reason_code=_safe_text(row.reason_code, limit=256),
                    reason_detail=_safe_text(row.reason_detail),
                    retryable=row.retryable,
                )
            )
        return tuple(events), tuple(dict.fromkeys(workers))

    def _collection(
        self, submission: CompanyResearchSubmission | None
    ) -> tuple[
        tuple[str, ...],
        tuple[str, ...] | None,
        tuple[ResearchSourceRead, ...] | None,
        tuple[str, ...] | None,
        tuple[ResearchCollectionFailure, ...] | None,
        tuple[str, ...],
        bool,
    ]:
        payload = _mapping(submission.payload) if submission is not None else {}
        raw_value = payload.get("workers")
        if not isinstance(raw_value, Sequence) or isinstance(raw_value, (str, bytes)):
            return (), None, None, None, None, (), False
        raw_workers = [item for item in raw_value if isinstance(item, Mapping)]
        if not raw_workers:
            return (), (), (), (), (), (), False

        worker_labels: list[str] = []
        attempted: list[str] = []
        reads: list[ResearchSourceRead] = []
        failure_messages: list[str] = []
        failure_details: list[ResearchCollectionFailure] = []
        security_warnings: list[str] = []
        for item in raw_workers:
            worker = _safe_string(item.get("worker"), limit=256) or "unknown"
            version = _safe_string(item.get("worker_version"), limit=128) or "unknown"
            label = f"{worker}/{version}"
            worker_labels.append(label)
            raw = _mapping(item.get("raw"))
            pages = raw.get("pages")
            if isinstance(pages, Sequence) and not isinstance(pages, (str, bytes)):
                for page in pages:
                    if not isinstance(page, Mapping):
                        continue
                    raw_url = page.get("url")
                    url = _safe_url(raw_url if isinstance(raw_url, str) else None)
                    if url is None:
                        security_warnings.append(
                            "A source URL was omitted because it was not a valid HTTP(S) URL."
                        )
                        continue
                    attempted.append(url)
                    reads.append(
                        ResearchSourceRead(
                            url=url,
                            title=_safe_string(page.get("title"), limit=1_000),
                            retrieved_at=_safe_string(page.get("retrieved_at"), limit=128),
                            retrieval_method=_safe_string(page.get("retrieval_method"), limit=128),
                            page_type=_safe_string(page.get("page_type"), limit=128),
                            research_worker=label,
                        )
                    )
            errors = raw.get("errors")
            if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)):
                for error in errors:
                    error_map = _mapping(error)
                    raw_url = error_map.get("url")
                    url = _safe_url(raw_url if isinstance(raw_url, str) else None)
                    if isinstance(raw_url, str) and url is None:
                        security_warnings.append(
                            "A failed source URL was omitted because it was not a valid "
                            "HTTP(S) URL."
                        )
                    if url is not None:
                        attempted.append(url)
                    raw_error = error_map.get("error")
                    if raw_error is None and not error_map:
                        raw_error = error
                    message = _safe_string(raw_error, limit=1_000) or (
                        "A structured collection failure was persisted; raw content is not exposed."
                    )
                    failure_messages.append(message)
                    failure_details.append(
                        ResearchCollectionFailure(
                            error=message,
                            url=url,
                            stage=_safe_string(error_map.get("stage"), limit=128),
                            occurred_at=_safe_string(error_map.get("at"), limit=128),
                            research_worker=label,
                        )
                    )
        reads.sort(key=lambda row: (row.url, row.research_worker or "", row.retrieved_at or ""))
        failure_details.sort(
            key=lambda row: (
                row.url or "",
                row.stage or "",
                row.occurred_at or "",
                row.error,
            )
        )
        return (
            tuple(dict.fromkeys(worker_labels)),
            tuple(sorted(set(attempted))),
            tuple(reads),
            tuple(message for message in failure_messages),
            tuple(failure_details),
            tuple(dict.fromkeys(security_warnings)),
            True,
        )

    def _facts(
        self, *, job: AgentJob, company_id: uuid.UUID | None
    ) -> tuple[tuple[ResearchFactView, ...], tuple[str, ...]]:
        if company_id is None:
            return (), ()
        insights = self._session.scalars(
            select(Insight)
            .where(
                Insight.company_id == company_id,
                Insight.idempotency_key.like(f"research:{job.id}:%"),
            )
            .order_by(Insight.created_at.asc(), Insight.id.asc())
        ).all()
        facts: list[ResearchFactView] = []
        warnings: list[str] = []
        for insight in insights:
            evidence_rows = self._session.scalars(
                select(InsightEvidence)
                .where(InsightEvidence.insight_id == insight.id)
                .order_by(
                    InsightEvidence.version.asc(),
                    InsightEvidence.source_url.asc(),
                    InsightEvidence.id.asc(),
                )
            ).all()
            evidence: list[ResearchEvidenceView] = []
            for item in evidence_rows:
                url = _safe_url(item.source_url)
                if url is None:
                    warnings.append(f"Evidence {item.id} has no displayable HTTP(S) source URL.")
                    continue
                evidence.append(
                    ResearchEvidenceView(
                        evidence_id=item.id,
                        source_url=url,
                        source_title=_safe_text(item.source_title, limit=1_000),
                        published_at=item.published_at,
                        retrieved_at=item.retrieved_at,
                        confidence=float(item.confidence) if item.confidence is not None else None,
                        extraction_method=_safe_text(item.extraction_method, limit=256),
                        freshness_at=item.freshness_at,
                        source_record_type=_safe_text(item.source_record_type, limit=128),
                        source_record_id=item.source_record_id,
                        version=item.version,
                    )
                )
            confidence = min(
                (row.confidence for row in evidence if row.confidence is not None),
                default=None,
            )
            facts.append(
                ResearchFactView(
                    claim=_safe_text(insight.claim) or "[unavailable]",
                    source_urls=tuple(row.source_url for row in evidence),
                    confidence=confidence,
                    insight_id=insight.id,
                    kind=insight.kind.value,
                    state=insight.state.value,
                    version=insight.version,
                    evidence=tuple(evidence),
                )
            )
        return tuple(facts), tuple(warnings)

    def _stage(
        self, *, membership_id: uuid.UUID, job: AgentJob
    ) -> CampaignContactAgentState | None:
        state = self._session.scalars(
            select(CampaignContactAgentState).where(
                CampaignContactAgentState.campaign_contact_id == membership_id,
                CampaignContactAgentState.agent_id == AgentIdentifier.RESEARCH,
            )
        ).first()
        return state if state is not None and state.latest_job_id == job.id else None

    @staticmethod
    def _related_job_views(
        jobs: tuple[AgentJob, ...], selected_job_id: uuid.UUID | None
    ) -> tuple[ResearchRetryView, ...]:
        views: list[ResearchRetryView] = []
        for item in jobs:
            error_type, error_detail = _job_error(item)
            views.append(
                ResearchRetryView(
                    job_id=item.id,
                    public_status=agent_jobs.public_status(item),
                    attempts=item.attempts,
                    max_attempts=item.max_attempts,
                    error_type=error_type,
                    error_detail=error_detail,
                    created_at=item.created_at,
                    selected=item.id == selected_job_id,
                    generation=_generation(item),
                    retryable_error=_retryable_error(item),
                    next_run_at=item.next_run_at,
                    started_at=item.started_at,
                    finished_at=item.finished_at,
                )
            )
        return tuple(views)

    def _build(
        self,
        *,
        membership: CampaignContact,
        campaign: Campaign,
        contact: Contact,
        job: AgentJob | None,
        related_jobs: tuple[AgentJob, ...],
        selection_reason: str,
        missing_job_reason: str | None = None,
    ) -> ResearchReport:
        if job is None:
            company = self._session.get(Company, contact.company_id) if contact.company_id else None
            resolution = self._domain_resolution(
                membership=membership, job=None, company_id=company.id if company else None
            )
            unavailable_without_job = (
                missing_job_reason
                or "No persisted Research Agent Job exists for this Campaign Contact.",
                "Worker identity, lease state and collection detail are unavailable "
                "because no job exists.",
                "Dropped or rejected evidence is not persisted as a structured Research ledger.",
            )
            return ResearchReport(
                campaign_contact_id=membership.id,
                campaign_id=campaign.id,
                campaign_name=_safe_text(campaign.name, limit=512) or str(campaign.id),
                contact_id=contact.id,
                contact_label=_safe_text(_contact_label(contact), limit=512) or str(contact.id),
                company_id=company.id if company else None,
                company_name=_safe_text(company.name, limit=512) if company else None,
                domain=_safe_domain(company.domain if company else contact.company_domain),
                domain_state=resolution.state if resolution else None,
                job_id=None,
                job_status=None,
                worker_identity=(),
                attempts=None,
                max_attempts=None,
                started_at=None,
                finished_at=None,
                duration_seconds=None,
                urls_attempted=None,
                successful_reads=None,
                collection_failures=None,
                submission_id=None,
                dossier_version=None,
                sourced_facts=None,
                rejected_evidence=None,
                retry_history=(),
                final_outcome=None,
                error_type=None,
                error_detail=None,
                unavailable=unavailable_without_job,
                report_state=ResearchReportState.UNAVAILABLE,
                report_reason=(
                    "No safe Research execution can be associated with this Campaign Contact."
                    if missing_job_reason
                    else "No Research execution has been durably recorded."
                ),
                domain_resolution=resolution,
                selection_reason=selection_reason,
            )

        result = _mapping(job.result)
        unavailable: list[str] = []
        warnings: list[str] = []
        submission, link_source = self._submission_for_job(job, result)

        result_company_id = _uuid(result.get("company_id"))
        company_id = result_company_id or job.company_id
        if company_id is None and submission is not None:
            company_id = submission.company_id
        if company_id is None:
            company_id = contact.company_id

        authoritative_ids = {
            value
            for value in (
                result_company_id,
                job.company_id,
                submission.company_id if submission else None,
            )
            if value is not None
        }
        if len(authoritative_ids) > 1:
            unavailable.append(
                "The job, result and submission disagree on Company ownership; "
                "linked research artifacts were withheld."
            )
            submission = None
            company_id = result_company_id or job.company_id
        company = self._session.get(Company, company_id) if company_id else None
        if company_id is not None and company is None:
            unavailable.append("The Company recorded for this execution no longer exists.")
        if contact.company_id and company_id and contact.company_id != company_id:
            warnings.append(
                "The Contact's current Company differs from the Company recorded by "
                "this historical execution."
            )

        dossier = self._dossier_for_job(result=result, submission=submission, company_id=company_id)
        if submission is None:
            unavailable.append("No raw Research submission is linked to the selected job.")
        if dossier is None:
            unavailable.append(
                "No exact dossier version is linked to the selected job's committed result."
            )

        (
            research_workers,
            attempted,
            reads,
            failure_messages,
            failure_details,
            collection_warnings,
            worker_payload_available,
        ) = self._collection(submission)
        warnings.extend(collection_warnings)
        if not worker_payload_available:
            unavailable.append("Worker-level collection detail was not persisted for this job.")

        facts, fact_warnings = self._facts(job=job, company_id=company_id)
        warnings.extend(fact_warnings)
        events, execution_workers = self._events(job)
        if not execution_workers:
            unavailable.append(
                "Execution worker identity is unavailable because no matching "
                "lease/start event was persisted."
            )
        unavailable.extend(
            (
                "A complete discovered/attempted URL ledger is not persisted; only "
                "successful reads and structured collection failures are shown.",
                "Historical lease-expiry transitions are not persisted as a dedicated "
                "Research attempt ledger; current lease fields and append-only job "
                "events are shown.",
                "The job stores one started/finished pair, so duration is the latest "
                "persisted attempt interval rather than a complete attempt timeline.",
                "Dropped or rejected evidence is not persisted as a structured "
                "Research ledger; dossier warnings are shown separately.",
            )
        )

        dossier_warnings = (
            tuple(
                _safe_string(item, limit=1_000)
                or "A structured dossier warning was persisted; raw content is not exposed."
                for item in (dossier.warnings or [])
            )
            if dossier
            else ()
        )
        warnings.extend(dossier_warnings)
        unavailable = list(dict.fromkeys(unavailable))

        duration = None
        if job.started_at and job.finished_at:
            duration = max(0.0, (job.finished_at - job.started_at).total_seconds())
        resolution = self._domain_resolution(membership=membership, job=job, company_id=company_id)
        execution_domain = _safe_domain(result.get("domain"))
        if (
            execution_domain
            and resolution
            and resolution.selected_domain
            and execution_domain != resolution.selected_domain
        ):
            warnings.append(
                "The current domain decision differs from the domain recorded by "
                "this historical execution."
            )
        warnings = list(dict.fromkeys(warnings))
        domain = (
            execution_domain
            or (resolution.selected_domain if resolution else None)
            or _safe_domain(company.domain if company else contact.company_domain)
        )
        error_type, error_detail = _job_error(job)
        final_raw = result.get("domain_outcome")
        final_outcome = _safe_text(final_raw, limit=1_000) if isinstance(final_raw, str) else None
        stage = self._stage(membership_id=membership.id, job=job)
        pipeline_reason = None
        if stage is not None:
            pipeline_reason = _safe_text(stage.reason_detail) or _safe_text(
                stage.reason_code, limit=256
            )

        complete = (
            job.status is AgentJobStatus.SUCCEEDED
            and submission is not None
            and dossier is not None
            and worker_payload_available
        )
        report_state = ResearchReportState.COMPLETE if complete else ResearchReportState.PARTIAL
        report_reason = (
            "The selected job and its committed Research artifacts are durably available."
            if complete
            else "The selected job exists, but one or more execution artifacts are "
            "absent or the job is not complete."
        )
        if job.lease_owner:
            lease_state = "active lease persisted"
        elif job.status in {
            AgentJobStatus.SUCCEEDED,
            AgentJobStatus.FAILED,
            AgentJobStatus.CANCELLED,
        }:
            lease_state = "released at terminal status"
        else:
            lease_state = "no active lease persisted"

        submission_view = (
            ResearchSubmissionView(
                submission_id=submission.id,
                company_id=submission.company_id,
                producer=_safe_text(submission.producer, limit=256) or "[unavailable]",
                producer_version=_safe_text(submission.producer_version, limit=128),
                submitted_at=submission.submitted_at,
                link_source=link_source or "unavailable",
            )
            if submission is not None
            else None
        )
        sections_present = tuple(
            field
            for field in _DOSSIER_SECTION_FIELDS
            if dossier and getattr(dossier, field) is not None
        )
        dossier_view = (
            ResearchDossierView(
                dossier_id=dossier.id,
                version_number=dossier.version_number,
                is_current=dossier.is_current,
                status="current" if dossier.is_current else "superseded",
                interpreter=_safe_text(dossier.interpreter, limit=256) or "[unavailable]",
                interpreter_version=_safe_text(dossier.interpreter_version, limit=128),
                created_at=dossier.created_at,
                sections_present=sections_present,
            )
            if dossier is not None
            else None
        )

        return ResearchReport(
            campaign_contact_id=membership.id,
            campaign_id=campaign.id,
            campaign_name=_safe_text(campaign.name, limit=512) or str(campaign.id),
            contact_id=contact.id,
            contact_label=_safe_text(_contact_label(contact), limit=512) or str(contact.id),
            company_id=company_id,
            company_name=_safe_text(company.name, limit=512) if company else None,
            domain=domain,
            domain_state=resolution.state if resolution else None,
            job_id=job.id,
            job_status=agent_jobs.public_status(job),
            worker_identity=research_workers,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            started_at=job.started_at,
            finished_at=job.finished_at,
            duration_seconds=duration,
            urls_attempted=attempted,
            successful_reads=reads,
            collection_failures=failure_messages,
            submission_id=submission.id if submission else None,
            dossier_version=dossier.version_number if dossier else None,
            sourced_facts=facts,
            rejected_evidence=None,
            retry_history=self._related_job_views(related_jobs, job.id),
            final_outcome=final_outcome,
            error_type=error_type,
            error_detail=error_detail,
            unavailable=tuple(unavailable),
            report_state=report_state,
            report_reason=report_reason,
            job_created_at=job.created_at,
            job_updated_at=job.updated_at,
            next_run_at=job.next_run_at,
            retryable_error=_retryable_error(job),
            lease_owner=_safe_text(job.lease_owner, limit=256),
            lease_expires_at=job.lease_expires_at,
            lease_state=lease_state,
            execution_workers=execution_workers,
            pipeline_status=stage.status.value if stage else None,
            pipeline_reason=pipeline_reason,
            domain_resolution=resolution,
            submission=submission_view,
            dossier=dossier_view,
            collection_failure_details=failure_details,
            warnings=tuple(warnings),
            job_events=events,
            selection_reason=selection_reason,
        )
