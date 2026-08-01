"""Typed, query-only report for one durable Company Agent execution.

Historical execution truth comes only from the selected job's persisted result or
error detail and from decision ids that result pinned. Current capture, Company and
Campaign state are separate projections and never repair missing history.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.contact import Contact
from app.models.enums import (
    AgentIdentifier,
    EnrichmentConfirmationStatus,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.pipeline import PipelineEvent
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.models.verification_job import AgentJob
from app.services.agent_studio.research_report import (
    _generation,
    _job_error,
    _mapping,
    _retryable_error,
    _safe_domain,
    _safe_text,
    _safe_url,
    _uuid,
)
from app.services.agents import jobs as agent_jobs
from app.services.captures import promotion as capture_promotion
from app.services.companies import conflicts as company_conflicts
from app.services.resolution import policy as resolution_policy
from app.services.resolution import store as resolution_store


class CompanyReportState(enum.StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class CompanyDomainOutcome(enum.StrEnum):
    CONFIRMED = "confirmed"
    PROVISIONAL = "provisional"
    UNRESOLVED = "unresolved"
    PROVIDER_ONLY = "provider_only"


@dataclass(frozen=True)
class CapturedEmployerReport:
    capture_id: uuid.UUID
    captured_at: datetime | None
    employer_name: str | None
    normalized_name: str | None
    linkedin_company_url: str | None
    linkedin_company_id: str | None
    location_hint: str | None


@dataclass(frozen=True)
class CompanyIdentityReport:
    match_key: str | None
    match_value: str | None
    candidate_company_ids: tuple[uuid.UUID, ...] | None
    selected_company_id: uuid.UUID | None
    company_action: str | None
    contact_link_action: str | None
    reason: str | None
    evidence_references: tuple[str, ...]
    idempotency_key: str


@dataclass(frozen=True)
class DomainCandidateReport:
    ordinal: int
    domain: str | None
    normalized_domain: str | None
    source_type: str | None
    source_reference: str | None
    provider_rank: int | None
    confidence: float | None
    status: str
    evidence: str | None


@dataclass(frozen=True)
class DomainDecisionReport:
    decision_id: uuid.UUID
    capture_id: uuid.UUID
    company_id: uuid.UUID | None
    scope: str
    outcome: CompanyDomainOutcome
    selected_domain: str | None
    decision_kind: str
    policy_version: str
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    decided_at: datetime
    observed_at: datetime | None
    is_current: bool
    superseded_at: datetime | None
    provider: str | None
    provider_call_made: bool
    candidates: tuple[DomainCandidateReport, ...]


@dataclass(frozen=True)
class ProviderResultReport:
    outcome: CompanyDomainOutcome
    provider: str | None
    lookup_status: str
    looked_up_at: datetime | None
    model_lookup_status: str
    model_domain: str | None
    model_source_url: str | None
    candidate_count: int
    confirmation_status: str
    candidates: tuple[DomainCandidateReport, ...]


@dataclass(frozen=True)
class CampaignPolicyReport:
    historical_allow_provisional: bool | None
    historical_settings_version: int | None
    historical_source: str | None
    current_allow_provisional: bool
    current_settings_version: int
    action: str | None
    research_allowed: bool | None
    research_reason: str | None
    later_stages_allowed: bool | None
    later_stages_reason: str | None


@dataclass(frozen=True)
class CompanyTruthReport:
    company_id: uuid.UUID | None
    company_name: str | None
    company_record_domain: str | None
    canonical_domain: str | None
    domain_outcome: CompanyDomainOutcome | None
    domain_source: str | None


@dataclass(frozen=True)
class RelatedCompanyGeneration:
    job_id: uuid.UUID
    status: str
    attempts: int
    max_attempts: int
    generation: int | None
    created_at: datetime
    selected: bool


@dataclass(frozen=True)
class CompanyJobEventReport:
    event_type: str
    from_status: str | None
    to_status: str | None
    reason_code: str | None
    reason_detail: str | None
    retryable: bool
    occurred_at: datetime


@dataclass(frozen=True)
class CompanyExecutionReport:
    report_state: CompanyReportState
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
    error_type: str | None
    error_detail: str | None
    retryable_error: bool | None
    campaign_id: uuid.UUID
    campaign_name: str
    campaign_contact_id: uuid.UUID
    contact_id: uuid.UUID
    contact_name: str
    customer_account: str | None
    preceding_identity_job_id: uuid.UUID | None
    captured_employer: CapturedEmployerReport | None
    identity: CompanyIdentityReport | None
    historical: CompanyTruthReport
    historical_conflict_kinds: tuple[str, ...]
    historical_domain_decision: DomainDecisionReport | None
    historical_capture_decision: DomainDecisionReport | None
    historical_company_aggregate_decision: DomainDecisionReport | None
    current_contact_company: CompanyTruthReport
    current_conflict_kinds: tuple[str, ...]
    current_capture_decision: DomainDecisionReport | None
    current_company_aggregate_decision: DomainDecisionReport | None
    current_provider_result: ProviderResultReport | None
    campaign_policy: CampaignPolicyReport
    downstream_research_job_id: uuid.UUID | None
    downstream_research_status: str | None
    related_generations: tuple[RelatedCompanyGeneration, ...]
    decision_history: tuple[DomainDecisionReport, ...]
    job_events: tuple[CompanyJobEventReport, ...]
    unavailable: tuple[str, ...]


def _contact_name(contact: Contact) -> str:
    name = " ".join(part for part in (contact.first_name, contact.last_name) if part)
    return name or contact.email or str(contact.id)


def _bool(mapping: Mapping[str, object], key: str) -> bool | None:
    value = mapping.get(key)
    return value if isinstance(value, bool) else None


def _int(mapping: Mapping[str, object], key: str) -> int | None:
    value = mapping.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(mapping: Mapping[str, object], key: str, *, limit: int = 500) -> str | None:
    value = mapping.get(key)
    return _safe_text(value if isinstance(value, str) else None, limit=limit)


def _outcome(value: object) -> CompanyDomainOutcome | None:
    if not isinstance(value, str):
        return None
    try:
        return CompanyDomainOutcome(value)
    except ValueError:
        return None


def _candidate_ids(value: object) -> tuple[uuid.UUID, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    return tuple(parsed for item in value if (parsed := _uuid(item)) is not None)


def _safe_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(
        safe for item in value if isinstance(item, str) and (safe := _safe_text(item, limit=200))
    )


class DurableCompanyReportReader:
    """Project persisted Company execution facts without issuing any writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def read_job(self, job_id: uuid.UUID) -> CompanyExecutionReport | None:
        with self._session.no_autoflush:
            return self._read_job(job_id)

    def _read_job(self, job_id: uuid.UUID) -> CompanyExecutionReport | None:
        job = self._session.get(AgentJob, job_id)
        if (
            job is None
            or job.agent_id is not AgentIdentifier.COMPANY
            or job.campaign_id is None
            or job.campaign_contact_id is None
            or job.contact_id is None
        ):
            return None
        membership = self._session.get(CampaignContact, job.campaign_contact_id)
        campaign = self._session.get(Campaign, job.campaign_id)
        contact = self._session.get(Contact, job.contact_id)
        if (
            membership is None
            or campaign is None
            or contact is None
            or membership.campaign_id != job.campaign_id
            or membership.contact_id != job.contact_id
        ):
            return None

        payload = self._execution_payload(job)
        historical_map = _mapping(payload.get("historical_company"))
        historical_company_id = (
            _uuid(historical_map.get("company_id"))
            or _uuid(_mapping(job.result).get("company_id"))
            or job.company_id
        )
        historical_record_domain = _safe_domain(
            historical_map.get("company_record_domain")
            or historical_map.get("domain")
            or _mapping(job.result).get("domain")
        )
        historical_domain = (
            _safe_domain(historical_map.get("canonical_domain"))
            if "canonical_domain" in historical_map
            else historical_record_domain
        )
        historical_state = _outcome(
            historical_map.get("domain_resolution_state")
            or _mapping(job.result).get("domain_resolution_state")
        )
        historical = CompanyTruthReport(
            company_id=historical_company_id,
            company_name=_str(historical_map, "name"),
            company_record_domain=historical_record_domain,
            canonical_domain=historical_domain,
            domain_outcome=historical_state,
            domain_source=_str(payload, "domain_resolution_source", limit=100),
        )

        capture = self._capture(job)
        captured = self._captured_employer(capture)
        enrichment = self._enrichment(capture)
        identity = self._identity(job, payload)
        historical_capture_decision = self._pinned_decision(
            job=job,
            payload=payload,
            key="capture_domain_resolution_id",
            scope="execution_capture",
            company_id=historical_company_id,
        )
        historical_aggregate_decision = self._pinned_decision(
            job=job,
            payload=payload,
            key="company_aggregate_domain_resolution_id",
            scope="execution_company_aggregate",
            company_id=historical_company_id,
        )
        historical_decision = historical_aggregate_decision or historical_capture_decision

        current_company = (
            self._session.get(Company, contact.company_id) if contact.company_id else None
        )
        current_aggregate_row = self._aggregate_decision(current_company)
        current_capture_row = (
            resolution_store.current_decision(self._session, capture.id) if capture else None
        )
        current_company_state = (
            resolution_store.company_state(self._session, current_company.id)
            if current_company
            else None
        )
        current_contact_truth = CompanyTruthReport(
            company_id=current_company.id if current_company else None,
            company_name=current_company.name if current_company else None,
            company_record_domain=(
                _safe_domain(current_company.domain) if current_company else None
            ),
            canonical_domain=(
                _safe_domain(current_aggregate_row.selected_domain)
                if current_aggregate_row
                else _safe_domain(current_company.domain)
                if current_company
                else None
            ),
            domain_outcome=_outcome(current_company_state.value) if current_company_state else None,
            domain_source=(
                "current_company_aggregate_decision"
                if current_company_state is not None
                else "no_automatic_decision"
                if current_company is not None
                else None
            ),
        )
        policy = self._campaign_policy(campaign, payload)
        research = self._research_handoff(job, membership)
        error_type, error_detail = _job_error(job)
        unavailable = self._unavailable(
            payload=payload,
            captured=captured,
            identity=identity,
            historical=historical,
            historical_decision=historical_decision,
            policy=policy,
            research=research,
        )
        state, reason = self._report_state(job, historical, identity, policy, unavailable)
        history = (
            tuple(
                self._decision_view(row, scope="capture_history", capture=capture)
                for row in resolution_store.decision_history(self._session, capture.id)
            )
            if capture
            else ()
        )
        return CompanyExecutionReport(
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
            error_type=error_type,
            error_detail=error_detail,
            retryable_error=_retryable_error(job),
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            campaign_contact_id=membership.id,
            contact_id=contact.id,
            contact_name=_contact_name(contact),
            customer_account=None,
            preceding_identity_job_id=self._identity_parent(job, membership),
            captured_employer=captured,
            identity=identity,
            historical=historical,
            historical_conflict_kinds=_safe_strings(payload.get("conflict_kinds")),
            historical_domain_decision=historical_decision,
            historical_capture_decision=historical_capture_decision,
            historical_company_aggregate_decision=historical_aggregate_decision,
            current_contact_company=current_contact_truth,
            current_conflict_kinds=(
                tuple(
                    conflict.kind.value
                    for conflict in company_conflicts.for_company(
                        self._session, company=current_company
                    )
                )
                if current_company
                else ()
            ),
            current_capture_decision=(
                self._decision_view(current_capture_row, scope="current_capture", capture=capture)
                if current_capture_row
                else None
            ),
            current_company_aggregate_decision=(
                self._decision_view(
                    current_aggregate_row, scope="current_company_aggregate", capture=None
                )
                if current_aggregate_row
                else None
            ),
            current_provider_result=self._provider_result(enrichment, current_capture_row),
            campaign_policy=policy,
            downstream_research_job_id=research.id if research else None,
            downstream_research_status=agent_jobs.public_status(research) if research else None,
            related_generations=self._related(job, membership),
            decision_history=history,
            job_events=self._events(job, membership),
            unavailable=unavailable,
        )

    @staticmethod
    def _execution_payload(job: AgentJob) -> Mapping[str, object]:
        result = _mapping(job.result)
        if result:
            return result
        return _mapping(_mapping(job.error).get("detail"))

    def _capture(self, job: AgentJob) -> LinkedInProfileSnapshot | None:
        if job.capture_id is None:
            return None
        capture = self._session.get(LinkedInProfileSnapshot, job.capture_id)
        if capture is None or capture.matched_contact_id not in {None, job.contact_id}:
            return None
        return capture

    @staticmethod
    def _captured_employer(
        capture: LinkedInProfileSnapshot | None,
    ) -> CapturedEmployerReport | None:
        if capture is None:
            return None
        hints = capture_promotion.company_hints(capture)
        return CapturedEmployerReport(
            capture_id=capture.id,
            captured_at=capture.captured_at,
            employer_name=_safe_text(hints.name, limit=512),
            normalized_name=_safe_text(hints.key, limit=512) if hints.key else None,
            linkedin_company_url=_safe_url(hints.linkedin_url),
            linkedin_company_id=_safe_text(hints.linkedin_id, limit=256),
            location_hint=_safe_text(hints.location, limit=512),
        )

    def _enrichment(
        self, capture: LinkedInProfileSnapshot | None
    ) -> SalesNavCompanyEnrichment | None:
        if capture is None:
            return None
        return self._session.scalars(
            select(SalesNavCompanyEnrichment).where(
                SalesNavCompanyEnrichment.capture_id == capture.id
            )
        ).one_or_none()

    @staticmethod
    def _identity(job: AgentJob, payload: Mapping[str, object]) -> CompanyIdentityReport | None:
        value = _mapping(payload.get("identity"))
        if not value:
            return None
        return CompanyIdentityReport(
            match_key=_str(value, "match_key", limit=100),
            match_value=_str(value, "match_value", limit=512),
            candidate_company_ids=_candidate_ids(value.get("candidate_company_ids")),
            selected_company_id=_uuid(value.get("selected_company_id")),
            company_action=_str(value, "company_action", limit=50),
            contact_link_action=_str(value, "contact_link_action", limit=50),
            reason=_str(value, "reason", limit=1_000),
            evidence_references=_safe_strings(value.get("evidence_references")),
            idempotency_key=_safe_text(job.idempotency_key, limit=400) or "[unavailable]",
        )

    def _pinned_decision(
        self,
        *,
        job: AgentJob,
        payload: Mapping[str, object],
        key: str,
        scope: str,
        company_id: uuid.UUID | None,
    ) -> DomainDecisionReport | None:
        decision_id = _uuid(payload.get(key))
        row = self._session.get(CompanyDomainResolution, decision_id) if decision_id else None
        if row is None:
            return None
        if key.startswith("capture") and row.capture_id != job.capture_id:
            return None
        if key.startswith("company") and row.resolved_company_id != company_id:
            return None
        capture = self._session.get(LinkedInProfileSnapshot, row.capture_id)
        return self._decision_view(row, scope=scope, capture=capture)

    def _aggregate_decision(self, company: Company | None) -> CompanyDomainResolution | None:
        if company is None:
            return None
        rows = resolution_store.current_decisions_for_company(self._session, company.id)
        return rows[0] if rows else None

    def _decision_view(
        self,
        row: CompanyDomainResolution,
        *,
        scope: str,
        capture: LinkedInProfileSnapshot | None,
    ) -> DomainDecisionReport:
        reason_codes = tuple(str(item) for item in row.reasons or [])
        reasons = tuple(
            _safe_text(resolution_policy.REASON_TEXT.get(code, code), limit=1_000) or code
            for code in reason_codes
        )
        warnings = tuple(
            _safe_text(resolution_policy.WARNING_TEXT.get(str(code), str(code)), limit=1_000)
            or str(code)
            for code in row.warnings or []
        )
        enrichment = (
            self._session.get(SalesNavCompanyEnrichment, row.enrichment_id)
            if row.enrichment_id
            else None
        )
        candidate_rows = tuple(item for item in row.candidates or () if isinstance(item, Mapping))
        eligible_count = sum(item.get("eligible") is True for item in candidate_rows)
        return DomainDecisionReport(
            decision_id=row.id,
            capture_id=row.capture_id,
            company_id=row.resolved_company_id,
            scope=scope,
            outcome=CompanyDomainOutcome(row.state.value),
            selected_domain=_safe_domain(row.selected_domain),
            decision_kind=row.decision_kind.value,
            policy_version=row.policy_version,
            reason_codes=reason_codes,
            reasons=reasons,
            warnings=warnings,
            decided_at=row.decided_at,
            observed_at=capture.captured_at if capture else None,
            is_current=row.is_current,
            superseded_at=row.superseded_at,
            provider=_safe_text(row.provider, limit=64),
            provider_call_made=row.provider_call_made,
            candidates=tuple(
                self._candidate_view(
                    item,
                    ordinal=index,
                    row=row,
                    model_source_url=(enrichment.model_source_url if enrichment else None),
                    conflicting=(row.state.value == "unresolved" and eligible_count > 1),
                )
                for index, item in enumerate(candidate_rows, start=1)
            ),
        )

    @staticmethod
    def _candidate_view(
        item: Mapping[str, object],
        *,
        ordinal: int,
        row: CompanyDomainResolution,
        model_source_url: str | None,
        conflicting: bool,
    ) -> DomainCandidateReport:
        domain = _safe_domain(item.get("domain"))
        rank = item.get("rank")
        eligible = item.get("eligible") is True
        selected = bool(domain and domain == _safe_domain(row.selected_domain))
        status = (
            "selected"
            if selected
            else "conflicting"
            if eligible and conflicting
            else "eligible"
            if eligible
            else "rejected"
        )
        evidence_parts = []
        for key in ("name", "alignment", "rejection_reason"):
            value = item.get(key)
            safe = _safe_text(value if isinstance(value, str) else None, limit=200)
            if safe:
                evidence_parts.append(f"{key}: {safe}")
        confidence = item.get("confidence")
        return DomainCandidateReport(
            ordinal=ordinal,
            domain=domain,
            normalized_domain=domain,
            source_type=_safe_text(row.provider, limit=64) or row.decision_kind.value,
            source_reference=(
                _safe_url(model_source_url)
                if selected and row.provider == resolution_policy.MODEL_PROVIDER
                else None
            ),
            provider_rank=rank if isinstance(rank, int) and not isinstance(rank, bool) else None,
            confidence=(
                float(confidence)
                if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                else None
            ),
            status=status,
            evidence="; ".join(evidence_parts) or None,
        )

    @staticmethod
    def _provider_result(
        enrichment: SalesNavCompanyEnrichment | None,
        current_decision: CompanyDomainResolution | None,
    ) -> ProviderResultReport | None:
        if enrichment is None:
            return None
        has_provider_evidence = bool(enrichment.candidates or enrichment.model_domain)
        if current_decision is not None or not has_provider_evidence:
            return None
        if enrichment.confirmation_status is not EnrichmentConfirmationStatus.UNCONFIRMED:
            return None
        candidates: list[DomainCandidateReport] = []
        for ordinal, item in enumerate(enrichment.candidates or (), start=1):
            if not isinstance(item, Mapping):
                continue
            domain = _safe_domain(item.get("domain"))
            rank = item.get("rank")
            confidence = item.get("confidence")
            name = item.get("name")
            candidates.append(
                DomainCandidateReport(
                    ordinal=ordinal,
                    domain=domain,
                    normalized_domain=domain,
                    source_type=_safe_text(enrichment.provider, limit=64),
                    source_reference=None,
                    provider_rank=(
                        rank if isinstance(rank, int) and not isinstance(rank, bool) else None
                    ),
                    confidence=(
                        float(confidence)
                        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                        else None
                    ),
                    status="provider_only",
                    evidence=_safe_text(name if isinstance(name, str) else None, limit=200),
                )
            )
        model_domain = _safe_domain(enrichment.model_domain)
        if model_domain:
            candidates.append(
                DomainCandidateReport(
                    ordinal=len(candidates) + 1,
                    domain=model_domain,
                    normalized_domain=model_domain,
                    source_type="model_fallback",
                    source_reference=_safe_url(enrichment.model_source_url),
                    provider_rank=None,
                    confidence=None,
                    status="provider_only",
                    evidence=_safe_text(enrichment.model_note, limit=500),
                )
            )
        return ProviderResultReport(
            outcome=CompanyDomainOutcome.PROVIDER_ONLY,
            provider=_safe_text(enrichment.provider, limit=64),
            lookup_status=enrichment.lookup_status.value,
            looked_up_at=enrichment.looked_up_at,
            model_lookup_status=enrichment.model_lookup_status.value,
            model_domain=model_domain,
            model_source_url=_safe_url(enrichment.model_source_url),
            candidate_count=len(enrichment.candidates or ()),
            confirmation_status=enrichment.confirmation_status.value,
            candidates=tuple(candidates),
        )

    @staticmethod
    def _campaign_policy(campaign: Campaign, payload: Mapping[str, object]) -> CampaignPolicyReport:
        historical = _mapping(payload.get("campaign_policy"))
        continuation = _mapping(payload.get("continuation"))
        return CampaignPolicyReport(
            historical_allow_provisional=_bool(historical, "allow_provisional_domains"),
            historical_settings_version=_int(historical, "campaign_settings_version"),
            historical_source=_str(historical, "source", limit=100),
            current_allow_provisional=campaign.allow_provisional_domains,
            current_settings_version=campaign.settings_version,
            action=_str(continuation, "action", limit=50),
            research_allowed=_bool(continuation, "research_allowed"),
            research_reason=_str(continuation, "research_reason", limit=1_000),
            later_stages_allowed=_bool(continuation, "later_stages_allowed"),
            later_stages_reason=_str(continuation, "later_stages_reason", limit=1_000),
        )

    def _identity_parent(self, job: AgentJob, membership: CampaignContact) -> uuid.UUID | None:
        parent = self._session.get(AgentJob, job.parent_job_id) if job.parent_job_id else None
        if (
            parent is not None
            and parent.agent_id is AgentIdentifier.IDENTITY
            and parent.campaign_contact_id == membership.id
            and parent.campaign_id == membership.campaign_id
            and parent.contact_id == membership.contact_id
        ):
            return parent.id
        return None

    def _research_handoff(self, job: AgentJob, membership: CampaignContact) -> AgentJob | None:
        return self._session.scalars(
            select(AgentJob)
            .where(
                AgentJob.parent_job_id == job.id,
                AgentJob.agent_id == AgentIdentifier.RESEARCH,
                AgentJob.campaign_contact_id == membership.id,
                AgentJob.campaign_id == membership.campaign_id,
                AgentJob.contact_id == membership.contact_id,
            )
            .order_by(AgentJob.created_at.asc(), AgentJob.id.asc())
        ).first()

    def _related(
        self, job: AgentJob, membership: CampaignContact
    ) -> tuple[RelatedCompanyGeneration, ...]:
        rows = self._session.scalars(
            select(AgentJob)
            .where(
                AgentJob.agent_id == AgentIdentifier.COMPANY,
                AgentJob.campaign_contact_id == membership.id,
                AgentJob.campaign_id == membership.campaign_id,
                AgentJob.contact_id == membership.contact_id,
            )
            .order_by(AgentJob.created_at.desc(), AgentJob.id.desc())
        ).all()
        return tuple(
            RelatedCompanyGeneration(
                job_id=row.id,
                status=agent_jobs.public_status(row),
                attempts=row.attempts,
                max_attempts=row.max_attempts,
                generation=_generation(row),
                created_at=row.created_at,
                selected=row.id == job.id,
            )
            for row in rows
        )

    def _events(
        self, job: AgentJob, membership: CampaignContact
    ) -> tuple[CompanyJobEventReport, ...]:
        rows = self._session.scalars(
            select(PipelineEvent)
            .where(
                PipelineEvent.job_id == job.id,
                PipelineEvent.campaign_contact_id == membership.id,
                PipelineEvent.agent_id == AgentIdentifier.COMPANY,
            )
            .order_by(PipelineEvent.occurred_at.asc(), PipelineEvent.id.asc())
        ).all()
        return tuple(
            CompanyJobEventReport(
                event_type=row.event_type.value,
                from_status=row.from_status.value if row.from_status else None,
                to_status=row.to_status.value if row.to_status else None,
                reason_code=_safe_text(row.reason_code, limit=96),
                reason_detail=_safe_text(row.reason_detail, limit=1_000),
                retryable=row.retryable,
                occurred_at=row.occurred_at,
            )
            for row in rows
        )

    @staticmethod
    def _unavailable(
        *,
        payload: Mapping[str, object],
        captured: CapturedEmployerReport | None,
        identity: CompanyIdentityReport | None,
        historical: CompanyTruthReport,
        historical_decision: DomainDecisionReport | None,
        policy: CampaignPolicyReport,
        research: AgentJob | None,
    ) -> tuple[str, ...]:
        missing = ["Authoritative customer/account ownership is not persisted in this context."]
        if captured is None:
            missing.append("Captured employer evidence is unavailable for this execution.")
        if identity is None:
            missing.append("The historical Company identity decision was not persisted.")
        elif identity.candidate_company_ids is None:
            missing.append("The historical Company candidate ledger was not persisted.")
        if historical.company_id is None:
            missing.append("No historical Company association was recorded by this execution.")
        if (
            historical.canonical_domain is None
            and historical.domain_outcome is not CompanyDomainOutcome.UNRESOLVED
        ):
            missing.append("The exact historical execution domain was not persisted.")
        resolution_source = payload.get("domain_resolution_source")
        if historical_decision is None and resolution_source != "no_automatic_decision":
            missing.append("No exact domain-decision row was pinned by this execution.")
        if policy.historical_allow_provisional is None:
            missing.append("The effective provisional-domain setting at execution is unavailable.")
        if policy.action is None:
            missing.append("The historical continuation or blocking action was not persisted.")
        if research is None:
            missing.append("No exact downstream Research handoff is durably linked.")
        if payload.get("schema_version") != "company-agent-report/1":
            missing.append("This execution predates the CMP-003 Company report contract.")
        missing.extend(
            (
                "Historical Company creation provenance is unavailable when not explicitly "
                "recorded.",
                "Unpersisted fuzzy or retrospective Company candidates are unavailable.",
            )
        )
        return tuple(missing)

    @staticmethod
    def _report_state(
        job: AgentJob,
        historical: CompanyTruthReport,
        identity: CompanyIdentityReport | None,
        policy: CampaignPolicyReport,
        unavailable: tuple[str, ...],
    ) -> tuple[CompanyReportState, str]:
        has_execution = bool(job.result or job.error)
        if not has_execution:
            return (
                CompanyReportState.UNAVAILABLE,
                "No durable execution outcome has been recorded for this Company job.",
            )
        complete = (
            identity is not None
            and historical.company_id is not None
            and (
                historical.canonical_domain is not None
                or historical.domain_outcome is CompanyDomainOutcome.UNRESOLVED
            )
            and policy.historical_allow_provisional is not None
            and policy.action is not None
            and not any("exact domain-decision" in item for item in unavailable)
        )
        if complete:
            return CompanyReportState.COMPLETE, "Exact CMP-003 execution lineage is available."
        return (
            CompanyReportState.PARTIAL,
            "Only part of this Company's historical execution lineage is durable.",
        )
