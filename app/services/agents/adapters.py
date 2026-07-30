"""Real Phase 2 adapters over existing domain components."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.draft import DraftVersion
from app.models.email_candidate import EmailCandidate
from app.models.enums import (
    AgentIdentifier,
    EmailPreciseStatus,
    IdentityLinkState,
    InsightKind,
    InsightState,
    LinkedInIdentifierKind,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.verification_job import AgentJob
from app.services import identity_links
from app.services.audit import record_audit_event
from app.services.companies import conflicts as company_conflicts
from app.services.companies import dossiers
from app.services.companies.dossiers import DossierError
from app.services.imports.normalization import is_valid_email, normalize_email
from app.services.insights import evidence as insights_evidence
from app.services.insights.evidence import InsightError
from app.services.resolution import store as resolution_store
from app.services.seller import context as seller_context
from app.services.seller.context import SellerContext
from app.services.suppressions import evaluate_suppression
from app.services.thinking import prompts
from app.services.thinking.claude_cli import ClaudeCliThinker
from app.services.thinking.contracts import Thinker, ThinkingError, ThinkingRequest
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


# --- Research, Insights and Personalization: the language-model Agents ---
#
# These three differ from Identity, Company, Email and Verification in one way
# that shapes every line below: their input is a judgement, not a lookup. The
# structure that follows exists to keep that judgement bounded.
#
# * The model is reached through an injected seam, so no test ever shells out.
# * Every answer is validated against the shape this code will store, and a
#   malformed answer is a failure rather than a partial write.
# * ``config["live"]`` must be exactly True, mirroring the Verification Agent.
#   There is deliberately no simulated mode: fabricated research would flow
#   downstream into a real email, and a fake insight is far more dangerous than
#   an absent one.
# * Anything the model could not establish is stored as an explicit unknown
#   rather than omitted, so a thin answer is visible instead of silently empty.


def _live_or_blocked(context: AgentExecutionContext, agent_label: str) -> None:
    """Refuse to run unless this Campaign explicitly enabled live execution."""

    if context.config.get("live") is not True:
        raise AgentBlocked(
            "thinking_live_disabled",
            f"Live execution is not enabled for the {agent_label} Agent on this Campaign. "
            'Set the Agent config to {"live": true} to allow it to run.',
        )


def _translate_thinking_error(error: ThinkingError) -> AgentExecutionError:
    """Map a model failure onto the worker's retry contract."""

    if error.retryable:
        return AgentRetryableError(error.code, error.message, detail=error.detail)
    return AgentTerminalError(error.code, error.message, detail=error.detail)


def _text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:limit] if cleaned else None


def _url(value: Any) -> str | None:
    """Accept only an absolute http(s) URL — the insight store requires one."""

    candidate = _text(value, limit=1024)
    if candidate is None:
        return None
    lowered = candidate.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return candidate
    return None


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return min(1.0, max(0.0, number))


def _seller_summary(seller: SellerContext) -> str:
    """Flatten trusted seller context into the prompt's first block."""

    lines: list[str] = []
    profile = seller.profile
    if profile is not None:
        lines.append(f"Company: {profile.name}")
        for label, value in (
            ("What we do", profile.short_description or profile.description),
            ("Positioning", profile.positioning),
            ("How we communicate", profile.communication_guidance),
        ):
            if value:
                lines.append(f"{label}: {value}")
        for label, values in (
            ("Industries served", profile.industries_served),
            ("Geographies served", profile.geographies_served),
            ("Capabilities", profile.capabilities),
            ("Differentiators", profile.differentiators),
        ):
            if values:
                lines.append(f"{label}: {', '.join(str(item) for item in values)}")
    for entry in seller.offerings:
        offering = entry.offering
        suffix = " (ARCHIVED — no longer offered)" if entry.is_archived else ""
        lines.append(f"\nOffering — {offering.name}{suffix}")
        if offering.short_description or offering.description:
            lines.append(f"  {offering.short_description or offering.description}")
        for proof in entry.proof_points:
            lines.append(f"  Proof point: {proof.statement}")
    return "\n".join(lines) if lines else "(no seller knowledge base has been entered yet)"


def _restricted_claims_block(seller: SellerContext) -> str:
    claims: list[str] = []
    for claim in seller.global_restricted_claims:
        claims.append(f"- {claim.title}: {claim.explanation}")
    for entry in seller.offerings:
        for claim in entry.restricted_claims:
            claims.append(f"- {claim.title}: {claim.explanation}")
    if not claims:
        return "- (none recorded — still do not invent numbers, customers or outcomes)"
    return "\n".join(dict.fromkeys(claims))


class ResearchAgentAdapter:
    """Research one company and store the answer as an immutable dossier version.

    The Agent produces no canonical Company fields as a side effect. It writes a
    submission (the raw answer, verbatim) and one interpretation of it, which is
    the storage contract ``app/services/companies/dossiers.py`` was built for and
    which APP-003 documented for exactly this producer.
    """

    agent_id = AgentIdentifier.RESEARCH

    def __init__(self, *, thinker_factory: Callable[[Settings], Thinker] | None = None) -> None:
        # The same constructor seam the Verification Agent uses for its provider.
        self._thinker_factory = thinker_factory or (
            lambda settings: ClaudeCliThinker(settings=settings)
        )

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        _live_or_blocked(context, "Research")
        contact = context.contact
        company = (
            context.session.get(Company, contact.company_id)
            if contact.company_id is not None
            else None
        )
        if company is None:
            raise AgentBlocked(
                "company_missing",
                "Research needs the permanent Company the Company Agent resolves.",
            )

        settings = get_settings()
        thinker = self._thinker_factory(settings)
        request = ThinkingRequest(
            prompt=prompts.research_prompt(
                company_name=company.name,
                domain=company.domain,
                industry=company.industry,
                country=company.country,
                company_size=company.company_size,
            ),
            purpose="company_research",
            timeout_seconds=float(context.config.get("timeout_seconds", 300.0)),
            # Research is the one stage that may look things up. Insights and
            # Personalization reason only over what this stage already stored.
            allowed_tools=tuple(context.config.get("allowed_tools", ("WebSearch",))),
        )
        try:
            answer = thinker.think(request)
        except ThinkingError as exc:
            raise _translate_thinking_error(exc) from exc

        payload = answer.payload
        sections = {name: payload[name] for name in dossiers.SECTION_COLUMNS if name in payload}
        if not sections:
            raise AgentTerminalError(
                "research_empty",
                "The research answer addressed none of the nine dossier sections.",
                detail={"returned_keys": sorted(payload)[:20]},
            )

        warnings: list[Any] = []
        unaddressed = [name for name in dossiers.SECTION_COLUMNS if name not in sections]
        if unaddressed:
            warnings.append({"unaddressed_sections": unaddressed})

        try:
            submission, submission_created = dossiers.submit(
                context.session,
                company=company,
                producer=answer.producer,
                payload=payload,
                producer_version=answer.producer_version,
                submitted_by=context.worker_id,
                request_context={
                    "agent_id": self.agent_id.value,
                    "job_id": str(context.job.id),
                    "campaign_id": str(context.campaign.id),
                    "purpose": request.purpose,
                    "duration_seconds": round(answer.duration_seconds, 2),
                },
            )
            version = dossiers.interpret(
                context.session,
                company=company,
                submission=submission,
                interpreter="research-agent",
                interpreter_version=answer.producer_version,
                sections=sections,
                warnings=warnings or None,
                created_by=context.worker_id,
                make_current=True,
            )
        except DossierError as exc:
            # The answer arrived but will not store. Terminal, not retryable:
            # asking again produces the same shape.
            raise AgentTerminalError(
                "research_unstorable",
                str(exc),
                detail={"sections": sorted(sections)},
            ) from exc

        overview = sections.get("overview")
        summary = None
        if isinstance(overview, dict):
            summary = _text(overview.get("summary"), limit=2000)
        output = {
            "company_id": str(company.id),
            "dossier_version": version.version_number,
            "submission_created": submission_created,
            "summary": summary,
            "sections_present": sorted(sections),
            "sections_unaddressed": unaddressed,
            "source_count": len(sections.get("sources") or []),
            "unknown_count": len(sections.get("unknowns") or []),
            "producer": answer.producer,
            "producer_version": answer.producer_version,
        }
        return AgentExecutionResult(
            outcome_committed=True,
            result={"domain_outcome": "company_researched", **output},
            output_reference=output,
        )


class InsightsAgentAdapter:
    """Turn the stored dossier into a few evidence-backed, seller-relevant claims.

    Every claim is written through ``insights.create_insight``, which refuses a
    supported claim that has no traceable source. A claim the model offered
    without a usable URL is therefore not stored as a weaker fact — it is
    dropped and counted, and the gaps the model named are stored as explicit
    unknowns. That asymmetry is the point: this stage may reduce what is
    asserted, never expand it.
    """

    agent_id = AgentIdentifier.INSIGHTS

    def __init__(self, *, thinker_factory: Callable[[Settings], Thinker] | None = None) -> None:
        self._thinker_factory = thinker_factory or (
            lambda settings: ClaudeCliThinker(settings=settings)
        )

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        _live_or_blocked(context, "Insights")
        contact = context.contact
        company = (
            context.session.get(Company, contact.company_id)
            if contact.company_id is not None
            else None
        )
        if company is None:
            raise AgentBlocked(
                "company_missing",
                "Insights needs the permanent Company the Company Agent resolves.",
            )
        current = dossiers.current_version(context.session, company_id=company.id)
        if current is None:
            raise AgentBlocked(
                "research_missing",
                "Insights needs a current company dossier. Run the Research Agent first, "
                "or skip Insights for this Contact.",
            )

        dossier = {
            name: getattr(current, name)
            for name in dossiers.SECTION_COLUMNS
            if getattr(current, name) is not None
        }
        seller = seller_context.assemble(context.session, campaign_id=context.campaign.id)

        settings = get_settings()
        thinker = self._thinker_factory(settings)
        request = ThinkingRequest(
            prompt=prompts.insights_prompt(
                seller_summary=_seller_summary(seller),
                company_name=company.name,
                dossier=dossier,
                contact_title=contact.title,
            ),
            purpose="company_insights",
            timeout_seconds=float(context.config.get("timeout_seconds", 240.0)),
            # No tools: this stage reasons over evidence already gathered. A new
            # lookup here would produce a claim whose source never entered the
            # dossier, and so could never be shown next to it.
            allowed_tools=(),
        )
        try:
            answer = thinker.think(request)
        except ThinkingError as exc:
            raise _translate_thinking_error(exc) from exc

        raw_claims = answer.payload.get("claims")
        raw_claims = raw_claims if isinstance(raw_claims, list) else []
        retrieved_at = datetime.now(UTC)
        stored: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        max_claims = int(context.config.get("max_claims", 5))

        for index, item in enumerate(raw_claims[:max_claims]):
            if not isinstance(item, dict):
                dropped.append({"index": index, "reason": "not_an_object"})
                continue
            claim = _text(item.get("claim"), limit=2000)
            source_url = _url(item.get("source_url"))
            evidence_summary = _text(item.get("evidence_summary"), limit=2000)
            if claim is None:
                dropped.append({"index": index, "reason": "empty_claim"})
                continue
            if source_url is None or evidence_summary is None:
                # Unsourced is not stored as a lesser fact. It is not stored.
                dropped.append({"index": index, "reason": "unsourced", "claim": claim[:200]})
                continue
            kind = (
                InsightKind.INTERPRETATION
                if str(item.get("kind", "")).strip().lower() == "interpretation"
                else InsightKind.FACT
            )
            try:
                insight = insights_evidence.create_insight(
                    context.session,
                    claim=claim,
                    kind=kind,
                    state=InsightState.SUPPORTED,
                    evidence=[
                        insights_evidence.EvidenceInput(
                            source_url=source_url,
                            retrieved_at=retrieved_at,
                            evidence_summary=evidence_summary,
                            confidence=_confidence(item.get("confidence")),
                            extraction_method=f"{answer.producer}/{answer.producer_version}",
                            source_record_type="company_dossier_version",
                            source_record_id=current.id,
                        )
                    ],
                    company_id=company.id,
                    # Stable per job and position, so a retried job re-uses the
                    # same records rather than duplicating them.
                    idempotency_key=f"insights-agent:{context.job.id}:{index}",
                    actor=context.worker_id,
                )
            except InsightError as exc:
                dropped.append({"index": index, "reason": "rejected", "detail": str(exc)[:200]})
                continue
            stored.append(
                {
                    "insight_id": str(insight.id),
                    "claim": claim,
                    "kind": kind.value,
                    "source_url": source_url,
                    "relevance": _text(item.get("relevance"), limit=600),
                }
            )

        unknowns = answer.payload.get("unknowns")
        unknown_texts = [
            text
            for text in (
                _text(entry, limit=1000)
                for entry in (unknowns if isinstance(unknowns, list) else [])
            )
            if text
        ][: int(context.config.get("max_unknowns", 5))]
        for index, unknown in enumerate(unknown_texts):
            try:
                insights_evidence.create_insight(
                    context.session,
                    claim=unknown,
                    kind=InsightKind.INTERPRETATION,
                    state=InsightState.UNKNOWN,
                    evidence=[],
                    company_id=company.id,
                    idempotency_key=f"insights-agent:{context.job.id}:unknown:{index}",
                    actor=context.worker_id,
                )
            except InsightError:
                # A gap that will not store is not worth failing the stage over.
                continue

        if not stored:
            raise AgentBlocked(
                "insufficient_evidence",
                "No claim in the answer carried a usable source, so nothing was stored. "
                "The Contact cannot be personalized from evidence that does not exist.",
                detail={"dropped": dropped[:10], "unknowns_recorded": len(unknown_texts)},
                # The unknown records above are real writes worth keeping.
                preserve_outcome=True,
            )

        output = {
            "company_id": str(company.id),
            "dossier_version": current.version_number,
            "insights_stored": len(stored),
            "claims_dropped": len(dropped),
            "unknowns_recorded": len(unknown_texts),
            "insights": stored,
            "dropped": dropped[:10],
            "producer": answer.producer,
        }
        return AgentExecutionResult(
            outcome_committed=True,
            result={"domain_outcome": "insights_recorded", **output},
            output_reference=output,
        )


class PersonalizationAgentAdapter:
    """Draft one email from stored evidence, and store it as an unapproved version.

    Three things this Agent deliberately does not do. It does not approve
    anything — a ``DraftVersion`` carries no authority and the separate
    ``DraftApproval`` remains a human act. It does not personalize from anything
    except insights that already passed the eligibility gate, so an unsourced
    sentence cannot reach an email through this path. And it re-checks the
    suppression ledger immediately before drafting, because an entry added while
    the job waited in the queue must still stop it: writing to someone is what
    suppression exists to prevent, and drafting is the first step of writing.
    """

    agent_id = AgentIdentifier.PERSONALIZATION

    def __init__(self, *, thinker_factory: Callable[[Settings], Thinker] | None = None) -> None:
        self._thinker_factory = thinker_factory or (
            lambda settings: ClaudeCliThinker(settings=settings)
        )

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        _live_or_blocked(context, "Personalization")
        session = context.session
        contact = context.contact

        suppression = evaluate_suppression(
            session, email=contact.email, domain=contact.company_domain
        )
        if suppression.blocked:
            raise AgentBlocked(
                "suppression",
                suppression.blocked_reason or "The suppression ledger blocks this Contact.",
            )

        company = session.get(Company, contact.company_id) if contact.company_id else None
        if company is None:
            raise AgentBlocked(
                "company_missing",
                "Drafting needs the permanent Company the Company Agent resolves.",
            )

        eligible = [
            insight
            for insight in insights_evidence.list_for_company(session, company_id=company.id)
            if insights_evidence.is_personalization_eligible(session, insight=insight)
        ]
        eligible.extend(
            insight
            for insight in insights_evidence.list_for_contact(session, contact_id=contact.id)
            if insights_evidence.is_personalization_eligible(session, insight=insight)
        )
        if not eligible:
            raise AgentBlocked(
                "no_eligible_evidence",
                "No insight has passed the personalization eligibility gate for this Contact, "
                "so there is nothing specific to write about.",
            )

        allowed_ids = {str(insight.id) for insight in eligible}
        evidence_block = "\n".join(
            f"[{insight.id}] ({insight.kind.value}) {insight.claim}" for insight in eligible
        )
        seller = seller_context.assemble(session, campaign_id=context.campaign.id)

        settings = get_settings()
        thinker = self._thinker_factory(settings)
        request = ThinkingRequest(
            prompt=prompts.personalization_prompt(
                seller_summary=_seller_summary(seller),
                restricted_claims=_restricted_claims_block(seller),
                evidence_block=evidence_block,
                first_name=contact.first_name,
                title=contact.title,
                company_name=company.name,
                max_words=int(context.config.get("max_words", 150)),
            ),
            purpose="email_personalization",
            timeout_seconds=float(context.config.get("timeout_seconds", 240.0)),
            allowed_tools=(),
        )
        try:
            answer = thinker.think(request)
        except ThinkingError as exc:
            raise _translate_thinking_error(exc) from exc

        subject = _text(answer.payload.get("subject"), limit=300)
        body = _text(answer.payload.get("body"), limit=20000)
        rationale = _text(answer.payload.get("rationale"), limit=2000)
        if subject is None or body is None:
            # The prompt explicitly permits this as an answer, and it is a
            # better one than a generic email would have been.
            raise AgentBlocked(
                "evidence_too_thin",
                rationale
                or "The evidence was too thin to write anything specific, so no draft was made.",
            )

        cited_raw = answer.payload.get("evidence_insight_ids")
        cited = [
            value
            for value in (cited_raw if isinstance(cited_raw, list) else [])
            if isinstance(value, str) and value in allowed_ids
        ]
        invented = [
            value
            for value in (cited_raw if isinstance(cited_raw, list) else [])
            if isinstance(value, str) and value not in allowed_ids
        ]
        if invented:
            # A citation to something that was never supplied means the draft is
            # not traceable to its evidence, which is the one property that makes
            # it reviewable at all.
            raise AgentTerminalError(
                "citation_not_supplied",
                "The draft cited evidence that was never supplied to it.",
                detail={"invented_ids": invented[:10]},
            )

        next_number = (
            session.scalar(
                select(func.coalesce(func.max(DraftVersion.version_number), 0)).where(
                    DraftVersion.contact_id == contact.id,
                    DraftVersion.campaign_id == context.campaign.id,
                )
            )
            or 0
        ) + 1
        draft = DraftVersion(
            contact_id=contact.id,
            campaign_id=context.campaign.id,
            version_number=next_number,
            subject=subject,
            body=body,
            rationale=rationale,
            created_by=f"{answer.producer}/{answer.producer_version}",
        )
        session.add(draft)
        session.flush()
        record_audit_event(
            session,
            actor=context.worker_id,
            action="draft.version_created",
            entity_type="draft_version",
            entity_id=str(draft.id),
            new_state=f"v{next_number}",
            reason="drafted by the Personalization Agent; not approved",
            context={
                "contact_id": str(contact.id),
                "campaign_id": str(context.campaign.id),
                "evidence_insight_ids": cited,
            },
        )

        output = {
            "draft_version_id": str(draft.id),
            "version_number": next_number,
            "subject": subject,
            "body": body,
            "rationale": rationale,
            "evidence_insight_ids": cited,
            "evidence_supplied": len(eligible),
            "approved": False,
            "producer": answer.producer,
        }
        return AgentExecutionResult(
            outcome_committed=True,
            result={"domain_outcome": "draft_created", **output},
            output_reference=output,
        )


DEFAULT_ADAPTERS: dict[AgentIdentifier, AgentAdapter] = {
    AgentIdentifier.IDENTITY: IdentityAgentAdapter(),
    AgentIdentifier.COMPANY: CompanyAgentAdapter(),
    AgentIdentifier.RESEARCH: ResearchAgentAdapter(),
    AgentIdentifier.EMAIL: EmailAgentAdapter(),
    AgentIdentifier.VERIFICATION: VerificationAgentAdapter(),
    AgentIdentifier.INSIGHTS: InsightsAgentAdapter(),
    AgentIdentifier.PERSONALIZATION: PersonalizationAgentAdapter(),
}
