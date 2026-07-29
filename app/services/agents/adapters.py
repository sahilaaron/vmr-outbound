"""Real Phase 2 adapters over existing domain components."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.enums import (
    AgentIdentifier,
    EmailPreciseStatus,
    IdentityLinkState,
    LinkedInIdentifierKind,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.verification_job import AgentJob
from app.services import identity_links
from app.services.audit import record_audit_event
from app.services.companies import conflicts as company_conflicts
from app.services.imports.normalization import is_valid_email, normalize_email
from app.services.resolution import store as resolution_store
from app.services.suppressions import evaluate_suppression
from app.services.verification import service as verification_service
from app.services.verification import status as verification_status
from app.services.verification.decisions import (
    ADDRESS_VERDICTS,
    UNSETTLED_EVIDENCE,
    DecisionOutcome,
    VerificationDecision,
    decide,
    refusal,
)
from app.services.verification.policy import get_policy
from app.services.verification.provider import VerificationProvider


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


class AgentWaiting(AgentExecutionError):
    """The job yielded until a separately queued dependency commits."""


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
    """Advance the policy-bounded Email state machine by one durable step."""

    agent_id = AgentIdentifier.EMAIL

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        # Kept local to avoid making the Email domain depend on Agent adapter
        # exception types. The state machine returns a semantic step; this
        # boundary translates it into the shared worker contract.
        from app.services.email.agent import (
            EmailExecutionStepKind,
            execute_step,
        )

        step = execute_step(
            context.session,
            job=context.job,
            contact=context.contact,
            membership=context.membership,
            actor=context.worker_id,
        )
        if step.kind is EmailExecutionStepKind.COMPLETE:
            return AgentExecutionResult(
                outcome_committed=True,
                result=step.result,
                output_reference=step.output_reference,
            )
        if step.kind is EmailExecutionStepKind.WAITING:
            raise AgentWaiting(
                step.reason_code or step.outcome.value,
                step.reason or "Waiting for the child Verification Agent.",
                detail=step.output_reference,
                preserve_outcome=True,
            )
        if step.kind is EmailExecutionStepKind.BLOCKED:
            raise AgentBlocked(
                step.reason_code or step.outcome.value,
                step.reason or "Email discovery is blocked.",
                detail=step.output_reference,
                preserve_outcome=True,
            )
        raise AgentTerminalError(
            step.reason_code or step.outcome.value,
            step.reason or "Email discovery ended without a usable address.",
            detail=step.output_reference,
            preserve_outcome=True,
        )


class ResearchAgentAdapter:
    """Gather sourced company facts through the enabled research workers.

    Thin on purpose: the state machine in ``app.services.research.agent``
    owns the decision, and the worker registry owns which sources run. The
    only logic here is the two framework-level gates -- the feature switch
    and the per-campaign ``live`` opt-in -- and translating the resulting
    step into the shared error vocabulary.
    """

    agent_id = AgentIdentifier.RESEARCH

    def __init__(self, *, workers_factory: Callable[..., Any] | None = None) -> None:
        # Injection seam for tests, mirroring VerificationAgentAdapter: the
        # suite must be able to run the real worker loop with a fake source.
        self._workers_factory = workers_factory

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        from app.services.research.agent import ResearchStepKind, execute_step
        from app.services.research.workers import WorkerNotRegistered, build_workers

        settings = get_settings()
        if not settings.features.company_research:
            raise AgentBlocked(
                "feature_disabled",
                "Company research is switched off for this deployment.",
            )
        # Nothing reaches another organisation's website until a campaign
        # explicitly opts in, exactly as verification refuses to spend before
        # an operator asks it to.
        if context.config.get("live") is not True:
            raise AgentBlocked(
                "research_not_live",
                "This campaign has not enabled live company research.",
            )

        factory = self._workers_factory or build_workers
        requested = context.config.get("workers")
        try:
            workers = factory(requested)
        except WorkerNotRegistered as exc:
            raise AgentTerminalError("worker_not_registered", str(exc)) from exc

        step = execute_step(
            context.session,
            job=context.job,
            contact=context.contact,
            workers=workers,
            options=context.config.get("worker_options") or {},
            actor=context.worker_id,
        )

        if step.kind is ResearchStepKind.COMPLETE:
            return AgentExecutionResult(
                outcome_committed=True,
                result=step.result,
                output_reference=step.output_reference,
            )
        if step.kind is ResearchStepKind.RETRY:
            raise AgentRetryableError(
                step.reason_code or "research_retry",
                step.reason or "Company research hit a transient fault.",
                detail=step.result,
                preserve_outcome=step.committed,
            )
        if step.kind is ResearchStepKind.BLOCKED:
            raise AgentBlocked(
                step.reason_code or "research_blocked",
                step.reason or "Company research cannot run for this contact.",
                detail=step.result,
                preserve_outcome=step.committed,
            )
        raise AgentTerminalError(
            step.reason_code or "research_failed",
            step.reason or "Company research produced no usable result.",
            detail=step.result,
            preserve_outcome=step.committed,
        )


class VerificationAgentAdapter:
    """Verify one exact address through the existing MillionVerifier service.

    The Agent boundary for MVP-01E (#225). It receives an already claimed and
    running Verification Agent Job from the common worker, validates the input,
    invokes the verification domain once, and returns a classified decision. It
    owns no queue transition, no retry schedule and no lifecycle of its own:
    every job status change is the Phase 2 worker's, translated from the error
    class this adapter raises.

    The rule the adapter exists to enforce is that a verdict is not an
    acceptance. ``verification_service`` will happily record durable evidence
    that a mailbox is invalid, catch-all, unknown, disposable or role-based —
    those are correct, valuable results — but none of them may complete the
    Verification stage and let a Campaign Contact advance toward outreach. Only
    fresh, live, valid, non-role evidence does. Everything else is recorded
    truthfully and stops the pipeline where it stands.
    """

    agent_id = AgentIdentifier.VERIFICATION

    def __init__(
        self,
        *,
        provider_factory: Callable[[Settings], VerificationProvider] | None = None,
    ) -> None:
        # A narrow, explicit seam so the automated suite can exercise the live
        # branch without a network call or a real credential. Production uses the
        # default, which is the same builder every other verification caller
        # uses; a test supplying its own provider still has to get past the live
        # authority and simulated-provider gates below.
        self._provider_factory = provider_factory or (
            lambda settings: verification_service.get_provider(settings, live=True)
        )

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        session = context.session
        requested_candidate_id = (context.job.input_reference or {}).get("candidate_id")
        selected: EmailCandidate | None
        if isinstance(requested_candidate_id, str):
            try:
                candidate_id = uuid.UUID(requested_candidate_id)
            except ValueError:
                candidate_id = None
            selected = session.get(EmailCandidate, candidate_id) if candidate_id else None
            if selected is not None and selected.contact_id != context.contact.id:
                selected = None
        else:
            # Backward-compatible standalone Verification stage: the permanent
            # selected candidate remains its input. Email child jobs carry an
            # immutable candidate_id and never need to mark generated text as
            # selected before evidence accepts it.
            selected = session.scalars(
                select(EmailCandidate).where(
                    EmailCandidate.contact_id == context.contact.id,
                    EmailCandidate.selected.is_(True),
                )
            ).one_or_none()
        if selected is None:
            rejected = refusal(
                "email_candidate_missing",
                "Verification needs one exact Email candidate from its requesting job.",
            )
            raise AgentBlocked(
                rejected.reason_code,
                rejected.reason,
                detail=self._refusal_output(rejected, context=context),
            )

        settings = get_settings()
        policy = get_policy(settings)
        email = normalize_email(selected.email)
        if not email or not is_valid_email(email):
            rejected = refusal(
                "verification_invalid_input",
                "The requested email candidate is not a well-formed address.",
            )
            raise AgentTerminalError(
                rejected.reason_code,
                rejected.reason,
                detail=self._refusal_output(
                    rejected,
                    context=context,
                    extra={"email_candidate_id": str(selected.id)},
                ),
            )
        if context.job.email is not None and normalize_email(context.job.email) != email:
            rejected = refusal(
                "verification_candidate_mismatch",
                "The Verification job address does not match its immutable Email candidate.",
            )
            raise AgentTerminalError(
                rejected.reason_code,
                rejected.reason,
                detail=self._refusal_output(
                    rejected,
                    context=context,
                    extra={"email_candidate_id": str(selected.id)},
                ),
            )

        # Suppression is authoritative and is re-checked immediately before the
        # provider is built, not merely when the job was queued: a ledger entry
        # added while this job sat in the queue must still stop it. Verifying a
        # suppressed identity is the first step of contacting it.
        suppression = evaluate_suppression(
            session, email=email, domain=context.contact.company_domain
        )
        if suppression.blocked:
            rejected = refusal(
                "suppression",
                suppression.blocked_reason or "The suppression ledger blocks this Contact.",
            )
            raise AgentBlocked(
                rejected.reason_code,
                rejected.reason,
                detail=self._refusal_output(rejected, context=context),
            )

        expected_policy = (context.job.input_reference or {}).get("policy_version")
        if expected_policy and expected_policy != policy.version:
            rejected = refusal(
                "verification_policy_mismatch",
                f"This job was queued for verification policy {expected_policy!r}, but "
                f"{policy.version!r} is active; results would not mean what was requested.",
            )
            raise AgentBlocked(
                rejected.reason_code,
                rejected.reason,
                detail=self._refusal_output(rejected, context=context),
            )

        if context.config.get("live") is not True:
            rejected = refusal(
                "verification_live_disabled",
                "Live MillionVerifier execution is not enabled for this Campaign. "
                "Simulator output cannot complete the Verification Agent.",
            )
            raise AgentBlocked(
                rejected.reason_code,
                rejected.reason,
                detail=self._refusal_output(rejected, context=context),
            )
        provider = self._provider_factory(settings)
        if provider.simulated:
            rejected = refusal(
                "verification_credentials_missing",
                "Live MillionVerifier credentials are not configured; simulator "
                "output cannot complete the Verification Agent.",
            )
            raise AgentBlocked(
                rejected.reason_code,
                rejected.reason,
                detail=self._refusal_output(rejected, context=context),
            )

        context.job.email = email
        context.job.policy_version = policy.version
        outcome = verification_service.verify_exact_address(
            session,
            context.job,
            provider=provider,
            settings=settings,
            policy=policy,
        )

        decision = self._decide(session, context=context, outcome=outcome)
        context.job.outcome_status = decision.status.value
        context.job.verification_id = outcome.evidence.id if outcome.evidence else None
        output = {
            "email": email,
            "decision": decision.decision.value,
            "reason_code": decision.reason_code,
            "reason": decision.reason,
            "precise_status": decision.status.value,
            "verification_result": outcome.result.value if outcome.result else None,
            "verification_id": str(outcome.evidence.id) if outcome.evidence else None,
            "reused_evidence": outcome.reused,
            "provider_called": outcome.provider_called,
            "provider": outcome.provider_label,
            "policy_version": outcome.policy_version,
        }

        if decision.decision is VerificationDecision.ACCEPT:
            return AgentExecutionResult(
                outcome_committed=True,
                result={"domain_outcome": "exact_email_verified", **output},
                output_reference=output,
            )

        # Everything below preserves the durable evidence this attempt produced.
        # The address was genuinely checked and paid for; discarding that because
        # the answer was unwelcome would make the next run buy it again.
        if decision.decision is VerificationDecision.RETRY_LATER:
            raise AgentRetryableError(
                decision.reason_code,
                decision.reason,
                detail=output,
                preserve_outcome=True,
            )
        if decision.decision is VerificationDecision.TRY_NEXT_CANDIDATE:
            raise AgentBlocked(
                decision.reason_code,
                decision.reason,
                detail=output,
                preserve_outcome=True,
            )
        if decision.decision is VerificationDecision.REFUSED:
            raise AgentBlocked(
                decision.reason_code,
                decision.reason,
                detail=output,
                preserve_outcome=True,
            )
        raise AgentTerminalError(
            decision.reason_code,
            decision.reason,
            detail=output,
            preserve_outcome=True,
        )

    @staticmethod
    def _refusal_output(
        outcome: DecisionOutcome,
        *,
        context: AgentExecutionContext,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        output: dict[str, Any] = {
            "decision": outcome.decision.value,
            "reason_code": outcome.reason_code,
            "reason": outcome.reason,
            "precise_status": outcome.status.value,
            "verification_id": None,
            "provider_called": False,
            "reused_evidence": False,
            "policy_version": (context.job.input_reference or {}).get("policy_version"),
        }
        output.update(extra or {})
        context.job.outcome_status = EmailPreciseStatus.UNVERIFIED.value
        return output

    @staticmethod
    def _decide(
        session: Session,
        *,
        context: AgentExecutionContext,
        outcome: verification_service.VerificationOutcome,
    ) -> DecisionOutcome:
        """Classify the outcome, re-asking the address read model about verdicts.

        A verdict recorded moments ago can still be untrustworthy: reused
        evidence may have aged past its freshness policy, and a second fresh
        result may contradict it. Rather than re-deriving those rules, the
        existing single read model for "what is true about this address now" is
        consulted and its answer wins when it says the evidence is unsettled.
        """

        status = outcome.precise
        if status in ADDRESS_VERDICTS:
            current = verification_status.derive_status_for_email(
                session, outcome.email, exclude_job_id=context.job.id
            ).precise
            if current in UNSETTLED_EVIDENCE:
                status = current

        job = context.job
        return decide(
            status,
            failure_class=outcome.failure_class,
            retry_available=job.attempts < job.max_attempts,
            simulated=outcome.simulated,
        )


DEFAULT_ADAPTERS: dict[AgentIdentifier, AgentAdapter] = {
    AgentIdentifier.IDENTITY: IdentityAgentAdapter(),
    AgentIdentifier.COMPANY: CompanyAgentAdapter(),
    AgentIdentifier.RESEARCH: ResearchAgentAdapter(),
    AgentIdentifier.EMAIL: EmailAgentAdapter(),
    AgentIdentifier.VERIFICATION: VerificationAgentAdapter(),
}
