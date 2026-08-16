"""Typed, read-only execution report for one durable Insights Agent job."""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import AgentIdentifier, InsightState
from app.models.insight import Insight, InsightEvidence
from app.models.score import ScoreEvidence
from app.models.verification_job import AgentJob
from app.services.agent_studio.research_report import _safe_text, _safe_url
from app.services.agents import jobs as agent_jobs
from app.services.insights import employee_size
from app.services.insights import evidence as insight_evidence
from app.services.insights.lineage import ResearchLineage, recorded
from app.services.personalization import policy as personalization_policy


class InsightsReportState(enum.StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class InsightsEvidenceReport:
    evidence_id: uuid.UUID
    source_url: str | None
    source_title: str | None
    published_at: datetime | None
    retrieved_at: datetime | None
    freshness_at: datetime | None
    confidence: float | None
    extraction_method: str | None
    source_record_type: str | None
    source_record_id: uuid.UUID | None
    summary: str | None
    valid: bool


@dataclass(frozen=True)
class EmployeeSizeReport:
    status: str
    exact_count: int | None
    approximate_count: int | None
    lower_bound: int | None
    upper_bound: int | None
    normalized_band: str
    source_wording: str | None
    observation_date: str | None
    derived_at: str | None
    derivation_version: str | None
    confidence: float | None
    temporal_status: str
    rationale: str | None
    conflict_count: int


@dataclass(frozen=True)
class InsightClaimReport:
    insight_id: uuid.UUID
    claim_type: str
    claim: str
    kind: str
    status: str
    confidence: float | None
    created_at: datetime
    freshness: str
    current_derivation: bool | None
    downstream_eligible: bool
    ineligible_reason: str | None
    conflict: bool
    evidence: tuple[InsightsEvidenceReport, ...]
    employee_size: EmployeeSizeReport | None


@dataclass(frozen=True)
class InsightsGenerationReport:
    job_id: uuid.UUID
    status: str
    attempts: int
    max_attempts: int
    created_at: datetime
    selected: bool


@dataclass(frozen=True)
class InsightsDownstreamReference:
    reference_type: str
    reference_id: uuid.UUID
    insight_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True)
class InsightsExecutionReport:
    report_state: InsightsReportState
    report_reason: str
    job_id: uuid.UUID
    job_status: str
    attempts: int
    max_attempts: int
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    next_run_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    parent_job_id: uuid.UUID | None
    error_type: str | None
    error_detail: str | None
    retryable_error: bool | None
    campaign_id: uuid.UUID
    campaign_name: str
    campaign_contact_id: uuid.UUID
    contact_id: uuid.UUID
    contact_name: str
    company_id: uuid.UUID | None
    company_name: str | None
    customer_account: str | None
    research_job_id: uuid.UUID | None
    research_submission_id: uuid.UUID | None
    research_dossier_id: uuid.UUID | None
    research_dossier_version: int | None
    claims: tuple[InsightClaimReport, ...]
    related_generations: tuple[InsightsGenerationReport, ...]
    downstream_references: tuple[InsightsDownstreamReference, ...]
    dropped_claims: tuple[str, ...] | None
    unavailable: tuple[str, ...]


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _contact_name(contact: Contact) -> str:
    return " ".join(part for part in (contact.first_name, contact.last_name) if part) or str(
        contact.id
    )


def _evidence_valid(row: InsightEvidence) -> bool:
    return bool(
        _safe_url(row.source_url)
        and row.retrieved_at is not None
        and row.evidence_summary
        and row.evidence_summary.strip()
        and row.confidence is not None
        and row.extraction_method
        and row.extraction_method.strip()
    )


def _payload_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _payload_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _payload_float(payload: Mapping[str, object], key: str) -> float | None:
    value = payload.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _dropped_summary(value: object) -> str:
    if not isinstance(value, Mapping):
        return _safe_text(value if isinstance(value, str) else None, limit=400) or "[unavailable]"
    parts: list[str] = []
    index = value.get("index")
    if isinstance(index, int) and not isinstance(index, bool):
        parts.append(f"index {index}")
    for key in ("reason", "claim", "detail"):
        raw = value.get(key)
        safe = _safe_text(raw if isinstance(raw, str) else None, limit=200)
        if safe:
            parts.append(f"{key}: {safe}")
    return "; ".join(parts) or "[unavailable]"


def _employee_view(insight: Insight) -> EmployeeSizeReport | None:
    if insight.insight_type != employee_size.EMPLOYEE_SIZE_TYPE:
        return None
    payload = insight.structured_payload or {}
    conflicts = payload.get("conflicts")
    return EmployeeSizeReport(
        status=str(payload.get("status") or "unavailable"),
        exact_count=_payload_int(payload, "exact_count"),
        approximate_count=_payload_int(payload, "approximate_count"),
        lower_bound=_payload_int(payload, "lower_bound"),
        upper_bound=_payload_int(payload, "upper_bound"),
        normalized_band=str(payload.get("normalized_band") or "unknown"),
        source_wording=_safe_text(_payload_str(payload, "source_wording")),
        observation_date=_payload_str(payload, "observation_date"),
        derived_at=_payload_str(payload, "derived_at"),
        derivation_version=insight.derivation_version,
        confidence=_payload_float(payload, "confidence"),
        temporal_status=str(payload.get("temporal_status") or "unknown"),
        rationale=_safe_text(_payload_str(payload, "rationale")),
        conflict_count=len(conflicts) if isinstance(conflicts, list) else 0,
    )


class DurableInsightsReportReader:
    """Project only persisted rows and never flush, enqueue, retry or mutate."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def read_job(self, job_id: uuid.UUID) -> InsightsExecutionReport | None:
        with self.session.no_autoflush:
            return self._read_job(job_id)

    def _read_job(self, job_id: uuid.UUID) -> InsightsExecutionReport | None:
        job = self.session.get(AgentJob, job_id)
        if (
            job is None
            or job.agent_id is not AgentIdentifier.INSIGHTS
            or job.campaign_id is None
            or job.campaign_contact_id is None
            or job.contact_id is None
        ):
            return None
        membership = self.session.get(CampaignContact, job.campaign_contact_id)
        campaign = self.session.get(Campaign, job.campaign_id)
        contact = self.session.get(Contact, job.contact_id)
        if (
            membership is None
            or campaign is None
            or contact is None
            or membership.campaign_id != job.campaign_id
            or membership.contact_id != job.contact_id
        ):
            return None
        company = self.session.get(Company, contact.company_id) if contact.company_id else None
        if job.company_id not in {None, contact.company_id}:
            return None
        result_company = (job.result or {}).get("company_id")
        if result_company is not None and str(contact.company_id) != result_company:
            return None

        # What this execution recorded having used, never the Company's present
        # state: a later Research run must not silently re-attribute an older
        # Insights result to evidence it never saw.
        lineage = (
            recorded(self.session, insights_job=job, company_id=company.id) if company else None
        )
        insights = self._job_insights(job, company)
        current_employee = (
            employee_size.current_derivation(self.session, company_id=company.id)
            if company
            else None
        )
        active_policy = personalization_policy.active_policy(self.session)
        policy_config = (
            personalization_policy.PolicyConfig.from_dict(dict(active_policy.configuration))
            if active_policy
            else None
        )
        minimum_confidence = (
            personalization_policy.minimum_confidence(policy_config) if policy_config else None
        )
        maximum_age_days = policy_config.evidence.maximum_age_days if policy_config else 365
        claims = tuple(
            self._claim(
                item,
                current_employee=current_employee,
                minimum_confidence=minimum_confidence,
                maximum_age_days=maximum_age_days,
            )
            for item in insights
        )
        unavailable = [
            "Customer/account ownership is not represented by current persistence.",
            "An attempt-by-attempt retry ledger and historical lease transitions are not "
            "persisted.",
            "Dropped proposals exist only in this job's bounded result, not a global claim ledger.",
        ]
        if lineage is None:
            unavailable.append(
                "This execution recorded no Research submission or dossier provenance."
            )
        if not insights:
            unavailable.append("No Insight can be attributed to this exact historical execution.")

        state, reason = self._state(job, lineage=lineage, claims=claims)
        result = _mapping(job.result)
        dropped_raw = result.get("dropped")
        dropped = (
            tuple(_dropped_summary(item) for item in dropped_raw[:10])
            if isinstance(dropped_raw, list)
            else None
        )
        error = _mapping(job.error)
        error_message = error.get("message")
        safe_error_message = error_message if isinstance(error_message, str) else None
        retryable = error.get("retryable")
        return InsightsExecutionReport(
            report_state=state,
            report_reason=reason,
            job_id=job.id,
            job_status=agent_jobs.public_status(job),
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            queued_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            next_run_at=job.next_run_at,
            lease_owner=_safe_text(job.lease_owner, limit=100),
            lease_expires_at=job.lease_expires_at,
            parent_job_id=job.parent_job_id,
            error_type=_safe_text(job.error_class, limit=96),
            error_detail=_safe_text(job.last_error or safe_error_message),
            retryable_error=retryable if isinstance(retryable, bool) else None,
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            campaign_contact_id=membership.id,
            contact_id=contact.id,
            contact_name=_contact_name(contact),
            company_id=company.id if company else None,
            company_name=company.name if company else None,
            customer_account=None,
            research_job_id=lineage.research_job.id if lineage else None,
            research_submission_id=lineage.submission.id if lineage else None,
            research_dossier_id=lineage.dossier.id if lineage else None,
            research_dossier_version=lineage.dossier.version_number if lineage else None,
            claims=claims,
            related_generations=self._related(job, membership),
            downstream_references=self._downstream(job, insights),
            dropped_claims=dropped,
            unavailable=tuple(unavailable),
        )

    def _job_insights(self, job: AgentJob, company: Company | None) -> tuple[Insight, ...]:
        if company is None:
            return ()
        return tuple(
            self.session.scalars(
                select(Insight)
                .where(
                    Insight.company_id == company.id,
                    or_(
                        Insight.producer_job_id == job.id,
                        Insight.idempotency_key.like(f"insights-agent:{job.id}:%"),
                    ),
                )
                .order_by(Insight.created_at, Insight.id)
            ).all()
        )

    def _claim(
        self,
        insight: Insight,
        *,
        current_employee: Insight | None,
        minimum_confidence: float | None,
        maximum_age_days: int,
    ) -> InsightClaimReport:
        rows = tuple(
            self.session.scalars(
                select(InsightEvidence)
                .where(InsightEvidence.insight_id == insight.id)
                .order_by(InsightEvidence.created_at, InsightEvidence.id)
            ).all()
        )
        evidence = tuple(
            InsightsEvidenceReport(
                evidence_id=row.id,
                source_url=_safe_url(row.source_url),
                source_title=_safe_text(row.source_title, limit=1_024),
                published_at=row.published_at,
                retrieved_at=row.retrieved_at,
                freshness_at=row.freshness_at,
                confidence=row.confidence,
                extraction_method=_safe_text(row.extraction_method, limit=255),
                source_record_type=_safe_text(row.source_record_type, limit=100),
                source_record_id=row.source_record_id,
                summary=_safe_text(row.evidence_summary, limit=2_000),
                valid=_evidence_valid(row),
            )
            for row in rows
        )
        employee = _employee_view(insight)
        eligibility_reason: str | None
        freshness: str
        current: bool | None
        confidence_values = [row.confidence for row in rows if row.confidence is not None]
        confidence = (
            personalization_policy.supporting_confidence(confidence_values)
            if confidence_values
            else None
        )
        dated: list[datetime] = []
        for row in rows:
            value = row.freshness_at or row.published_at or row.retrieved_at
            if value is not None:
                dated.append(value)
        newest = max(dated) if dated else None
        stale_by_policy = newest is not None and newest < datetime.now(UTC) - timedelta(
            days=maximum_age_days
        )
        if employee is not None:
            eligible, eligibility_reason = employee_size.downstream_eligible(insight)
            freshness = employee.temporal_status
            current = current_employee is not None and current_employee.id == insight.id
            if eligible and not current:
                eligible = False
                eligibility_reason = "A later Employee Size derivation is the current projection."
            if eligible and stale_by_policy:
                eligible = False
                freshness = "stale"
                eligibility_reason = (
                    f"Evidence is older than the active {maximum_age_days}-day policy limit."
                )
        else:
            eligible = insight_evidence.is_personalization_eligible(self.session, insight=insight)
            freshness = "stale" if stale_by_policy else ("current" if newest else "unavailable")
            if stale_by_policy:
                eligible = False
                eligibility_reason = "All available evidence is older than the reporting policy."
            elif not rows:
                eligibility_reason = "No durable evidence is linked."
            elif any(not item.valid for item in evidence):
                eligibility_reason = "At least one linked evidence record is invalid."
                eligible = False
            elif insight.state is not InsightState.SUPPORTED:
                eligibility_reason = f"Claim state is {insight.state.value}."
            else:
                eligibility_reason = (
                    None if eligible else "The evidence eligibility gate refused it."
                )
            current = None
        if (
            eligible
            and minimum_confidence is not None
            and confidence is not None
            and confidence < minimum_confidence
        ):
            eligible = False
            eligibility_reason = (
                f"Strongest supporting confidence {confidence:.2f} is below the active "
                f"Personalization threshold {minimum_confidence:.2f}."
            )
        return InsightClaimReport(
            insight_id=insight.id,
            claim_type=insight.insight_type or "unstructured",
            claim=_safe_text(insight.claim, limit=4_000) or "[unavailable]",
            kind=insight.kind.value,
            status=employee.status if employee else insight.state.value,
            confidence=confidence,
            created_at=insight.created_at,
            freshness=freshness,
            current_derivation=current,
            downstream_eligible=eligible,
            ineligible_reason=None if eligible else eligibility_reason,
            conflict=(
                insight.state is InsightState.CONFLICTING
                or (employee is not None and employee.status == "conflicted")
            ),
            evidence=evidence,
            employee_size=employee,
        )

    def _related(
        self, selected: AgentJob, membership: CampaignContact
    ) -> tuple[InsightsGenerationReport, ...]:
        rows = self.session.scalars(
            select(AgentJob)
            .where(
                AgentJob.agent_id == AgentIdentifier.INSIGHTS,
                AgentJob.campaign_contact_id == membership.id,
                AgentJob.campaign_id == membership.campaign_id,
                AgentJob.contact_id == membership.contact_id,
            )
            .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
        ).all()
        return tuple(
            InsightsGenerationReport(
                job_id=row.id,
                status=agent_jobs.public_status(row),
                attempts=row.attempts,
                max_attempts=row.max_attempts,
                created_at=row.created_at,
                selected=row.id == selected.id,
            )
            for row in rows
        )

    def _downstream(
        self, selected: AgentJob, insights: tuple[Insight, ...]
    ) -> tuple[InsightsDownstreamReference, ...]:
        ids = {item.id for item in insights}
        if not ids:
            return ()
        output: list[InsightsDownstreamReference] = []
        for row in self.session.scalars(
            select(ScoreEvidence).where(ScoreEvidence.insight_id.in_(ids))
        ).all():
            output.append(
                InsightsDownstreamReference(
                    reference_type="score",
                    reference_id=row.score_id,
                    insight_id=row.insight_id,
                    created_at=row.created_at,
                )
            )
        jobs = self.session.scalars(
            select(AgentJob).where(
                AgentJob.agent_id == AgentIdentifier.PERSONALIZATION,
                AgentJob.campaign_contact_id == selected.campaign_contact_id,
            )
        ).all()
        for job in jobs:
            raw_ids = (job.result or {}).get("evidence_insight_ids")
            for raw in raw_ids if isinstance(raw_ids, list) else []:
                try:
                    insight_id = uuid.UUID(raw) if isinstance(raw, str) else None
                except ValueError:
                    insight_id = None
                if insight_id in ids:
                    output.append(
                        InsightsDownstreamReference(
                            reference_type="personalization_job",
                            reference_id=job.id,
                            insight_id=insight_id,
                            created_at=job.created_at,
                        )
                    )
        return tuple(sorted(output, key=lambda item: (item.created_at, item.reference_id)))

    @staticmethod
    def _state(
        job: AgentJob,
        *,
        lineage: ResearchLineage | None,
        claims: tuple[InsightClaimReport, ...],
    ) -> tuple[InsightsReportState, str]:
        if lineage is None and not claims:
            return (
                InsightsReportState.UNAVAILABLE,
                "Neither recorded Research provenance nor historical job output is durably "
                "available.",
            )
        complete_claims = bool(claims) and all(
            claim.evidence or claim.status in {"unavailable", "unresolved", "stale"}
            for claim in claims
        )
        if job.finished_at is not None and lineage is not None and complete_claims:
            return (
                InsightsReportState.COMPLETE,
                "The execution, its recorded Research provenance, claims and evidence are "
                "durably available.",
            )
        return (
            InsightsReportState.PARTIAL,
            "Only part of this Insights execution is durably available.",
        )
