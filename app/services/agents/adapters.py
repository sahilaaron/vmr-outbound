"""Real Phase 2 adapters over existing domain components."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    IdentityLinkState,
    LinkedInIdentifierKind,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.verification_job import AgentJob
from app.services import identity_links
from app.services.audit import record_audit_event
from app.services.companies import conflicts as company_conflicts
from app.services.email.candidates import generate_candidates
from app.services.resolution import store as resolution_store
from app.services.resolution.gates import DownstreamStage, authorize_contact
from app.services.suppressions import evaluate_suppression
from app.services.verification import service as verification_service
from app.services.verification.policy import get_policy


class AgentExecutionError(Exception):
    """Base classified Agent execution failure."""

    retryable = False
    preserve_outcome = False

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        preserve_outcome: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.preserve_outcome = preserve_outcome


class AgentBlocked(AgentExecutionError):
    """Non-terminal domain condition requiring evidence or operator action."""


class AgentRetryableError(AgentExecutionError):
    retryable = True


class AgentTerminalError(AgentExecutionError):
    pass


@dataclass
class AgentExecutionContext:
    session: Session
    job: AgentJob
    campaign: Campaign
    membership: CampaignContact
    contact: Contact
    config: dict[str, Any]
    worker_id: str


@dataclass(frozen=True)
class AgentExecutionResult:
    outcome_committed: bool
    result: dict[str, Any]
    output_reference: dict[str, Any]
    # Verification's established service owns its exact-address job transition.
    queue_status_handled: bool = False


class AgentAdapter(Protocol):
    agent_id: AgentIdentifier

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult: ...


class IdentityAgentAdapter:
    """Converge directly observed LinkedIn identifiers on the permanent Contact."""

    agent_id = AgentIdentifier.IDENTITY

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        contact = context.contact
        if contact.merged_into_id is not None:
            raise AgentTerminalError(
                "contact_merged",
                "The enrolled Contact was merged into a survivor.",
                detail={"survivor_contact_id": str(contact.merged_into_id)},
            )

        capture: LinkedInProfileSnapshot | None = None
        linked_ids: list[str] = []
        if context.job.capture_id is not None:
            capture = context.session.get(LinkedInProfileSnapshot, context.job.capture_id)
            if capture is None:
                raise AgentTerminalError(
                    "capture_missing",
                    "The source capture no longer exists.",
                )
            if capture.matched_contact_id not in {None, contact.id}:
                raise AgentBlocked(
                    "identity_conflict",
                    "The source capture resolves to a different permanent Contact.",
                    detail={"matched_contact_id": str(capture.matched_contact_id)},
                )

            observed_vanity = (
                capture.normalized_profile_url if capture.profile_url_source == "observed" else None
            )
            if capture.salesnav_member_id and observed_vanity:
                bridge_outcome = identity_links.bridge_observed_pair(
                    context.session,
                    contact=contact,
                    member_id=capture.salesnav_member_id,
                    vanity_url=observed_vanity,
                    decided_by="identity-agent",
                    capture_id=capture.id,
                    source_surface=capture.source_surface,
                )
                if not bridge_outcome.bridged:
                    raise AgentBlocked(
                        "identity_conflict",
                        bridge_outcome.reason
                        or "Observed LinkedIn identifiers need operator review.",
                    )
                linked_ids.extend(
                    [
                        LinkedInIdentifierKind.SALESNAV_MEMBER_ID.value,
                        LinkedInIdentifierKind.PUBLIC_VANITY_URL.value,
                    ]
                )
            else:
                for kind, value in (
                    (
                        LinkedInIdentifierKind.SALESNAV_MEMBER_ID,
                        capture.salesnav_member_id,
                    ),
                    (LinkedInIdentifierKind.PUBLIC_VANITY_URL, observed_vanity),
                ):
                    if not value:
                        continue
                    link_outcome = identity_links.record_observed(
                        context.session,
                        contact=contact,
                        kind=kind,
                        value=value,
                        decided_by="identity-agent",
                        capture_id=capture.id,
                        source_surface=capture.source_surface,
                    )
                    if link_outcome.state is IdentityLinkState.NEEDS_REVIEW:
                        raise AgentBlocked(
                            "identity_conflict",
                            "A directly observed LinkedIn identifier belongs to another Contact.",
                            detail={
                                "conflicting_contact_id": (
                                    str(link_outcome.conflicting_contact_id)
                                    if link_outcome.conflicting_contact_id
                                    else None
                                )
                            },
                        )
                    linked_ids.append(kind.value)

        context.session.flush()
        output = {
            "contact_id": str(contact.id),
            "capture_id": str(capture.id) if capture else None,
            "identity_basis": (
                "directly_observed_linkedin_identifiers"
                if linked_ids
                else "existing_permanent_contact"
            ),
            "identifier_kinds": linked_ids,
        }
        return AgentExecutionResult(
            outcome_committed=True,
            result={"domain_outcome": "identity_converged", **output},
            output_reference=output,
        )


class CompanyAgentAdapter:
    """Resolve only an existing permanent Company or one exact unique domain."""

    agent_id = AgentIdentifier.COMPANY

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        contact = context.contact
        company = (
            context.session.get(Company, contact.company_id)
            if contact.company_id is not None
            else None
        )
        linked_by_agent = False
        if company is None:
            if not contact.company_domain:
                raise AgentBlocked(
                    "company_domain_missing",
                    "Company resolution needs an observed or approved domain.",
                )
            candidates = list(
                context.session.scalars(
                    select(Company).where(Company.domain == contact.company_domain)
                ).all()
            )
            if not candidates:
                raise AgentBlocked(
                    "company_missing",
                    "No permanent Company matches the Contact's exact normalized domain.",
                    detail={"domain": contact.company_domain},
                )
            if len(candidates) > 1:
                raise AgentBlocked(
                    "company_ambiguous",
                    "Several permanent Companies share the Contact's domain.",
                    detail={
                        "domain": contact.company_domain,
                        "candidate_ids": [str(candidate.id) for candidate in candidates],
                    },
                )
            company = candidates[0]
            contact.company_id = company.id
            linked_by_agent = True
            record_audit_event(
                context.session,
                actor="company-agent",
                action="company_agent.contact_linked",
                entity_type="contact",
                entity_id=str(contact.id),
                new_state=str(company.id),
                reason="linked by exact unique normalized company domain",
                context={"domain": contact.company_domain},
            )

        conflicts = company_conflicts.for_company(context.session, company=company)
        resolution_state = resolution_store.company_state(context.session, company.id)
        context.session.flush()
        output = {
            "company_id": str(company.id),
            "domain": company.domain,
            "domain_resolution_state": (
                resolution_state.value if resolution_state is not None else None
            ),
            "research_state": company.research_state.value,
            "linked_by_agent": linked_by_agent,
            "conflict_kinds": [conflict.kind.value for conflict in conflicts],
        }
        return AgentExecutionResult(
            outcome_committed=True,
            result={"domain_outcome": "company_resolved", **output},
            output_reference=output,
        )


class EmailAgentAdapter:
    """Invoke the existing deterministic candidate generator behind safety gates."""

    agent_id = AgentIdentifier.EMAIL

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        suppression = evaluate_suppression(
            context.session,
            email=context.contact.email,
            domain=context.contact.company_domain,
        )
        if suppression.blocked:
            raise AgentBlocked(
                "suppression",
                suppression.blocked_reason or "The suppression ledger blocks this Contact.",
            )
        company_gate = authorize_contact(
            context.session,
            contact=context.contact,
            stage=DownstreamStage.EMAIL_DISCOVERY,
        )
        if company_gate.blocked:
            raise AgentBlocked(
                "company_identity",
                company_gate.reason or "Company identity is not confirmed.",
            )
        if not context.contact.first_name or not context.contact.last_name:
            raise AgentBlocked(
                "person_name_missing",
                "Email generation needs an observed first and last name.",
            )
        generated = generate_candidates(context.session, context.contact)
        if generated.needs_review or generated.selected is None:
            raise AgentBlocked(
                "email_review_required",
                generated.review_reason or "No exact email candidate can be selected safely.",
                preserve_outcome=True,
            )
        output = {
            "email_candidate_id": str(generated.selected.id),
            "email": generated.selected.email,
            "source": generated.selected.source.value,
            "engine_version": generated.selected.engine_version,
        }
        return AgentExecutionResult(
            outcome_committed=True,
            result={"domain_outcome": "email_candidate_selected", **output},
            output_reference=output,
        )


class VerificationAgentAdapter:
    """Invoke the existing MillionVerifier service only in explicitly live mode."""

    agent_id = AgentIdentifier.VERIFICATION

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        selected = context.session.scalars(
            select(EmailCandidate).where(
                EmailCandidate.contact_id == context.contact.id,
                EmailCandidate.selected.is_(True),
            )
        ).one_or_none()
        if selected is None:
            raise AgentBlocked(
                "email_candidate_missing",
                "Verification needs one selected exact email candidate.",
            )
        if context.config.get("live") is not True:
            raise AgentBlocked(
                "verification_live_disabled",
                "Live MillionVerifier execution is not enabled for this Campaign. "
                "Simulator output cannot complete the Verification Agent.",
            )
        settings = get_settings()
        policy = get_policy(settings)
        context.job.email = selected.email
        context.job.policy_version = policy.version
        provider = verification_service.get_provider(
            settings,
            live=True,
        )
        if provider.simulated:
            raise AgentBlocked(
                "verification_credentials_missing",
                "Live MillionVerifier credentials are not configured; simulator "
                "output cannot complete the Verification Agent.",
            )
        verification_service.process_job(
            context.session,
            context.job,
            provider=provider,
            policy=policy,
        )
        if context.job.status is AgentJobStatus.RETRY_SCHEDULED:
            return AgentExecutionResult(
                outcome_committed=False,
                result={
                    "domain_outcome": "verification_retry_scheduled",
                    "error": context.job.last_error,
                },
                output_reference={"email": selected.email},
                queue_status_handled=True,
            )
        if context.job.status is AgentJobStatus.FAILED:
            return AgentExecutionResult(
                outcome_committed=False,
                result={
                    "domain_outcome": "verification_failed",
                    "error": context.job.last_error,
                },
                output_reference={"email": selected.email},
                queue_status_handled=True,
            )
        if context.job.status is not AgentJobStatus.SUCCEEDED:
            raise AgentRetryableError(
                "verification_incomplete",
                "Verification did not reach a durable outcome.",
            )
        output = {
            "email": selected.email,
            "verification_id": (
                str(context.job.verification_id) if context.job.verification_id else None
            ),
            "precise_status": context.job.outcome_status,
        }
        context.job.result = {"domain_outcome": "exact_email_verified", **output}
        return AgentExecutionResult(
            outcome_committed=True,
            result=context.job.result,
            output_reference=output,
            queue_status_handled=True,
        )


DEFAULT_ADAPTERS: dict[AgentIdentifier, AgentAdapter] = {
    AgentIdentifier.IDENTITY: IdentityAgentAdapter(),
    AgentIdentifier.COMPANY: CompanyAgentAdapter(),
    AgentIdentifier.EMAIL: EmailAgentAdapter(),
    AgentIdentifier.VERIFICATION: VerificationAgentAdapter(),
}
