"""Typed read-only execution reports for Email and Verification Agents."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_discovery import EmailCandidateAttempt
from app.models.email_evidence import MailDomainObservation
from app.models.email_verification_studio import VerificationProviderAttempt
from app.models.enums import AgentIdentifier
from app.models.usage_ledger import UsageLedgerEntry
from app.models.verification_job import AgentJob


def _sanitize_text(value: str | None) -> str | None:
    from app.services.workbench_agents.sanitize import sanitize_text

    return sanitize_text(value)


def _sanitize_mapping(value: dict[str, object]) -> dict[str, object] | None:
    from app.services.workbench_agents.sanitize import sanitize_mapping

    return sanitize_mapping(value)


@dataclass(frozen=True)
class ProviderStepReport:
    order: int
    provider_id: str
    adapter_version: str
    simulated: bool
    provider_called: bool
    precise_status: str | None
    result: str | None
    retryable: bool
    conflict: bool
    error_summary: str | None
    verification_id: uuid.UUID | None
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True)
class CandidateStepReport:
    index: int
    email: str
    pattern: str
    source: str
    status: str
    verification_job_id: uuid.UUID | None
    decision: str | None
    reason: str | None


@dataclass(frozen=True)
class UsageReportItem:
    provider: str
    origin: str
    result: str | None
    charge_status: str
    units: int
    account_reference: str | None
    attempted_at: datetime


@dataclass(frozen=True)
class AgentExecutionReport:
    availability: str
    completeness: str
    agent_id: str
    job_id: uuid.UUID
    parent_job_id: uuid.UUID | None
    public_status: str
    attempts: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    campaign_id: uuid.UUID | None
    campaign_name: str | None
    campaign_contact_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    contact_name: str | None
    company_id: uuid.UUID | None
    company_name: str | None
    domain: str | None
    catch_all: bool | None
    selected_email: str | None
    policy_version: str | None
    error_summary: str | None
    candidates: tuple[CandidateStepReport, ...]
    provider_steps: tuple[ProviderStepReport, ...]
    usage: tuple[UsageReportItem, ...]
    warnings: tuple[str, ...]
    unavailable_reasons: tuple[str, ...]


class EmailVerificationReportReader:
    """Project durable rows into one stable dataclass graph without writes."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def read(self, job_id: uuid.UUID, expected: AgentIdentifier) -> AgentExecutionReport | None:
        with self.session.no_autoflush:
            job = self.session.get(AgentJob, job_id)
            if job is None or job.agent_id is not expected:
                return None
            campaign = self.session.get(Campaign, job.campaign_id) if job.campaign_id else None
            membership = (
                self.session.get(CampaignContact, job.campaign_contact_id)
                if job.campaign_contact_id
                else None
            )
            contact = self.session.get(Contact, job.contact_id) if job.contact_id else None
            company = self.session.get(Company, job.company_id) if job.company_id else None
            if membership is not None and (
                membership.contact_id != job.contact_id or membership.campaign_id != job.campaign_id
            ):
                return None
            state = (job.result or {}).get("email_discovery")
            state_map = state if isinstance(state, dict) else {}
            candidate_rows = self.session.scalars(
                select(EmailCandidateAttempt)
                .where(EmailCandidateAttempt.email_job_id == job.id)
                .order_by(EmailCandidateAttempt.candidate_index, EmailCandidateAttempt.id)
            ).all()
            candidates = tuple(
                CandidateStepReport(
                    index=row.candidate_index,
                    email=row.normalized_email,
                    pattern=row.candidate_format,
                    source=(
                        str(
                            state_map.get("candidates", [])[row.candidate_index].get(
                                "source", "configured"
                            )
                        )
                        if isinstance(state_map.get("candidates"), list)
                        and row.candidate_index < len(state_map["candidates"])
                        and isinstance(state_map["candidates"][row.candidate_index], dict)
                        else "configured"
                    ),
                    status=row.status,
                    verification_job_id=row.verification_job_id,
                    decision=row.verification_decision,
                    reason=_sanitize_text(row.refusal_reason),
                )
                for row in candidate_rows
            )
            provider_rows = self.session.scalars(
                select(VerificationProviderAttempt)
                .where(VerificationProviderAttempt.job_id == job.id)
                .order_by(
                    VerificationProviderAttempt.provider_order, VerificationProviderAttempt.id
                )
            ).all()
            provider_steps = tuple(
                ProviderStepReport(
                    order=row.provider_order,
                    provider_id=row.provider_id,
                    adapter_version=row.adapter_version,
                    simulated=row.simulated,
                    provider_called=row.provider_called,
                    precise_status=row.precise_status,
                    result=row.result,
                    retryable=row.retryable,
                    conflict=row.conflict,
                    error_summary=_sanitize_text(row.error_summary),
                    verification_id=row.verification_id,
                    started_at=row.started_at,
                    finished_at=row.finished_at,
                )
                for row in provider_rows
            )
            usage_rows = self.session.scalars(
                select(UsageLedgerEntry)
                .where(UsageLedgerEntry.job_id == job.id)
                .order_by(UsageLedgerEntry.attempted_at, UsageLedgerEntry.id)
            ).all()
            usage = tuple(
                UsageReportItem(
                    provider=row.provider,
                    origin=row.origin,
                    result=row.result,
                    charge_status=row.charge_status.value,
                    units=row.units,
                    account_reference=row.account_reference,
                    attempted_at=row.attempted_at,
                )
                for row in usage_rows
            )
            domain = company.domain if company else (contact.company_domain if contact else None)
            domain_observation = (
                self.session.scalars(
                    select(MailDomainObservation)
                    .where(MailDomainObservation.domain == domain)
                    .order_by(
                        MailDomainObservation.observed_at.desc(), MailDomainObservation.id.desc()
                    )
                    .limit(1)
                ).first()
                if domain
                else None
            )
            unavailable = [
                "Per-provider response bodies are intentionally not exposed.",
                "Provider invoice reconciliation is not durably persisted.",
            ]
            if not provider_steps and expected is AgentIdentifier.VERIFICATION:
                unavailable.append("Provider-step history predates EV-001 or was not persisted.")
            warnings: list[str] = []
            if any(step.conflict for step in provider_steps):
                warnings.append("Providers returned conflicting address evidence.")
            sanitized_error = _sanitize_text(job.last_error)
            sanitized_payload = _sanitize_mapping(dict(job.error)) if job.error else None
            if sanitized_error is None and sanitized_payload:
                sanitized_error = _sanitize_text(str(sanitized_payload))
            if expected is AgentIdentifier.EMAIL:
                number = state_map.get("pattern_policy_version_number")
                identifier = state_map.get("pattern_policy_version_id")
                policy_version = (
                    f"Email pattern v{number} ({identifier})"
                    if number is not None and identifier is not None
                    else str(
                        state_map.get("policy_version") or job.policy_version or "legacy default"
                    )
                )
            else:
                waterfall_id = (job.input_reference or {}).get("waterfall_policy_version_id")
                policy_version = (
                    f"Verification {job.policy_version or 'policy not persisted'}; "
                    f"waterfall {waterfall_id or 'legacy active/default'}"
                )
            completeness = "complete"
            if expected is AgentIdentifier.EMAIL and not candidates:
                completeness = "partial"
            if expected is AgentIdentifier.VERIFICATION and not provider_steps:
                completeness = "partial"
            return AgentExecutionReport(
                availability="available",
                completeness=completeness,
                agent_id=job.agent_id.value,
                job_id=job.id,
                parent_job_id=job.parent_job_id,
                public_status=job.status.value,
                attempts=job.attempts,
                max_attempts=job.max_attempts,
                created_at=job.created_at,
                updated_at=job.updated_at,
                campaign_id=job.campaign_id,
                campaign_name=campaign.name if campaign else None,
                campaign_contact_id=job.campaign_contact_id,
                contact_id=job.contact_id,
                contact_name=(
                    " ".join(item for item in (contact.first_name, contact.last_name) if item)
                    if contact
                    else None
                ),
                company_id=job.company_id,
                company_name=company.name
                if company
                else (contact.company_name if contact else None),
                domain=domain,
                catch_all=domain_observation.is_catch_all if domain_observation else None,
                selected_email=(contact.email if contact else None) or job.email,
                policy_version=policy_version,
                error_summary=sanitized_error,
                candidates=candidates,
                provider_steps=provider_steps,
                usage=usage,
                warnings=tuple(warnings),
                unavailable_reasons=tuple(unavailable),
            )
