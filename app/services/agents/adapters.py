"""Real Phase 2 adapters over existing domain components."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.contact import Contact
from app.models.draft import DraftVersion
from app.models.email_candidate import EmailCandidate
from app.models.email_sequence import EmailSequence
from app.models.enums import (
    AgentIdentifier,
    EmailPreciseStatus,
    IdentityLinkState,
    InsightKind,
    InsightState,
    LinkedInIdentifierKind,
    SequenceReviewState,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.personalization_policy import PersonalizationPolicyVersion
from app.models.verification_job import AgentJob
from app.services import identity_links
from app.services.audit import record_audit_event
from app.services.companies import conflicts as company_conflicts
from app.services.companies import dossiers
from app.services.imports.normalization import is_valid_email, normalize_email
from app.services.insights import employee_size
from app.services.insights import evidence as insights_evidence
from app.services.insights import lineage as insights_lineage
from app.services.insights.evidence import InsightError
from app.services.operations import settings as operational
from app.services.personalization import generation as personalization_generation
from app.services.personalization import policy as personalization_policy
from app.services.personalization import sequence as sequence_generation
from app.services.personalization.cadence import CadenceError, campaign_opted_in
from app.services.personalization.sequence_validation import SequenceValidationError
from app.services.provenance import supplied_inputs
from app.services.resolution import gates as resolution_gates
from app.services.resolution import store as resolution_store
from app.services.seller import effective as effective_offering
from app.services.seller.context import SellerContext
from app.services.sequences import persistence as sequence_persistence
from app.services.sequences import read as sequence_read
from app.services.suppressions import evaluate_suppression
from app.services.thinking import prompts
from app.services.thinking.claude_cli import ClaudeCliThinker
from app.services.thinking.contracts import Thinker, ThinkingError, ThinkingRequest
from app.services.verification import service as verification_service
from app.services.verification import status as verification_status
from app.services.verification import waterfall as verification_waterfall
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
from app.services.verification.waterfall import WaterfallUnavailable


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
    """Establish the Contact's permanent Company, by evidence or by resolution.

    Three ways in, tried in that order, and the order is the safety property:

    1. the Contact already carries the permanent ``company_id`` edge;
    2. it carries a domain, and exactly one permanent Company has that exact
       normalized domain;
    3. it carries only the company *name*, and the shared company-domain
       resolution process is asked to establish one.

    Step 3 is what makes this Agent the single place a company is established,
    whichever surface the Contact arrived from. A Chrome capture reaches its
    company before the Contact exists — resolution runs against the capture at
    intake and in the backfill pass — so by the time this Agent sees it, step 1
    or 2 answers. Google Sheets produces the Contact directly and has no capture
    to resolve against, so its unseen companies arrive here with a name and
    nothing else, and this is where they enter the *same* process: same evidence,
    same provider ladder, same policy, same decision ledger, same gates.

    The alternative — a name-to-domain lookup inside the Sheets intake request —
    is what this repair exists to remove. Intake stays free and spends nothing;
    the Agent that owns the company stage owns the cost of establishing one, in
    the durable worker where a slow provider or model call has no request to
    overrun.
    """

    agent_id = AgentIdentifier.COMPANY

    def __init__(
        self,
        *,
        access_factory: Callable[[Session, Settings], Any] | None = None,
        model_factory: Callable[[Session, Settings], Any] | None = None,
    ) -> None:
        # Seams, in the shape ResearchAgentAdapter uses: a test drives a stubbed
        # provider transport without an API key, and production takes the shared
        # builders so this Agent cannot develop its own reading of whether the
        # provider may be called.
        self._access_factory = access_factory
        self._model_factory = model_factory

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        contact = context.contact
        company = (
            context.session.get(Company, contact.company_id)
            if contact.company_id is not None
            else None
        )
        linked_by_agent = False
        candidate_ids: list[str] = []
        identity_match_key = "contact.company_id"
        resolution_summary: dict[str, Any] | None = None
        if company is None and not contact.company_domain:
            resolution_summary = self._resolve_company_domain(context)
            company = (
                context.session.get(Company, contact.company_id)
                if contact.company_id is not None
                else None
            )
            if company is not None:
                linked_by_agent = True
                identity_match_key = "company.resolved_domain"
        else:
            resolution_summary = self._not_attempted(context)
        if company is None:
            if not contact.company_domain:
                raise AgentBlocked(
                    "company_domain_missing",
                    "Company resolution needs an observed or approved domain.",
                    detail=self._blocked_detail(
                        context,
                        match_key="contact.company_domain",
                        candidate_ids=(),
                        reason=self._unresolved_reason(resolution_summary),
                        resolution=resolution_summary,
                    ),
                )
            identity_match_key = "company.domain"
            candidates = list(
                context.session.scalars(
                    select(Company)
                    .where(Company.domain == contact.company_domain)
                    .order_by(Company.id.asc())
                ).all()
            )
            candidate_ids = [str(candidate.id) for candidate in candidates]
            if not candidates:
                raise AgentBlocked(
                    "company_missing",
                    "No permanent Company matches the Contact's exact normalized domain.",
                    detail=self._blocked_detail(
                        context,
                        match_key=identity_match_key,
                        candidate_ids=(),
                        reason="No exact permanent Company domain matched.",
                    ),
                )
            if len(candidates) > 1:
                raise AgentBlocked(
                    "company_ambiguous",
                    "Several permanent Companies share the Contact's domain.",
                    detail=self._blocked_detail(
                        context,
                        match_key=identity_match_key,
                        candidate_ids=tuple(candidate_ids),
                        reason="Several permanent Companies shared the exact domain.",
                    ),
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
        else:
            candidate_ids = [str(company.id)]

        conflicts = company_conflicts.for_company(context.session, company=company)
        resolution_state = resolution_store.company_state(context.session, company.id)
        capture_decision = (
            resolution_store.current_decision(context.session, context.job.capture_id)
            if context.job.capture_id is not None
            else None
        )
        aggregate_decisions = resolution_store.current_decisions_for_company(
            context.session, company.id
        )
        aggregate_decision = aggregate_decisions[0] if aggregate_decisions else None
        research_gate = resolution_gates.research_readiness(
            context.session, company_id=company.id, domain=company.domain
        )
        later_gate = resolution_gates.authorize_company(
            context.session,
            company_id=company.id,
            stage=resolution_gates.DownstreamStage.EMAIL_DISCOVERY,
            campaign=context.campaign,
        )
        policy = {
            "allow_provisional_domains": context.campaign.allow_provisional_domains,
            "campaign_settings_version": context.campaign.settings_version,
            "source": "execution_snapshot",
        }
        continuation_action = (
            "block"
            if not research_gate.ready
            else "review_required"
            if not later_gate.allowed
            else "continue"
        )
        resolved_here = identity_match_key == "company.resolved_domain"
        lineage = {
            "schema_version": "company-agent-report/1",
            # Present only when this execution asked the shared resolution process
            # to establish the company, so a reviewer can tell a Company that was
            # looked up here from one that was already on the Contact.
            "domain_resolution_attempt": resolution_summary,
            "identity": {
                "match_key": identity_match_key,
                "match_value": (
                    str(company.id)
                    if identity_match_key == "contact.company_id"
                    else contact.company_domain
                ),
                "candidate_company_ids": candidate_ids,
                "selected_company_id": str(company.id),
                "company_action": "resolved" if resolved_here else "reused",
                "contact_link_action": "linked" if linked_by_agent else "already_linked",
                "reason": (
                    "Established the company domain through automatic company-domain "
                    "resolution and linked the resulting permanent Company."
                    if resolved_here
                    else "Selected the one permanent Company with the exact normalized domain."
                    if linked_by_agent
                    else "Reused the Contact's existing permanent Company association."
                ),
                "evidence_references": [
                    f"capture:{context.job.capture_id}"
                    if context.job.capture_id is not None
                    else "contact:permanent_record"
                ],
            },
            "historical_company": {
                "company_id": str(company.id),
                "name": company.name,
                "company_record_domain": company.domain,
                "canonical_domain": (
                    aggregate_decision.selected_domain
                    if aggregate_decision is not None
                    else company.domain
                ),
                "domain_resolution_state": (
                    resolution_state.value if resolution_state is not None else None
                ),
            },
            "capture_domain_resolution_id": (
                str(capture_decision.id) if capture_decision is not None else None
            ),
            "company_aggregate_domain_resolution_id": (
                str(aggregate_decision.id) if aggregate_decision is not None else None
            ),
            "domain_resolution_source": (
                "company_aggregate_decision"
                if aggregate_decision is not None
                else "no_automatic_decision"
            ),
            "conflict_kinds": [conflict.kind.value for conflict in conflicts],
            "campaign_policy": policy,
            "continuation": {
                "action": continuation_action,
                "research_allowed": research_gate.ready,
                "research_reason": research_gate.reason,
                "later_stages_allowed": later_gate.allowed,
                "later_stages_reason": later_gate.reason,
            },
        }
        context.job.company_id = company.id
        if not research_gate.ready:
            raise AgentBlocked(
                "company_domain_unresolved",
                research_gate.reason,
                detail=lineage,
                preserve_outcome=True,
            )
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
            **lineage,
        }
        return AgentExecutionResult(
            outcome_committed=True,
            result={"domain_outcome": "company_resolved", **output},
            output_reference=output,
        )

    def _not_attempted(self, context: AgentExecutionContext) -> dict[str, Any]:
        """Why automatic company-domain resolution had nothing to do here.

        This branch is not new behaviour and deliberately does not become any: a
        Contact that already carries a domain, or already carries the permanent
        ``company_id`` edge, has never had resolution run against it, because
        resolution's only question — *which company is this?* — is already
        answered. What was missing was the record saying so.

        Two facts an operator reads differently used to look identical in the
        lineage, both as a bare ``null``: "resolution was not attempted because
        the operator named the website" and "resolution was not attempted because
        there was nothing to resolve from". Naming them is the whole of this
        method.

        Note what is *not* skipped, on any of these paths. A supplied website
        answers which company this is; it says nothing whatever about what
        matters about that company. Company Intelligence, Research and Insights
        run for these Contacts exactly as they do for every other.
        """

        contact = context.contact
        supplied = supplied_inputs.supplied_domain(
            context.session, membership=context.membership, contact=contact
        )
        if supplied is not None:
            return {
                "attempted": False,
                "reason_code": (
                    supplied_inputs.DOMAIN_DERIVED_REASON
                    if supplied.derived
                    else supplied_inputs.DOMAIN_REASON
                ),
                "reason": (
                    (
                        "The operator supplied a corporate address at intake and this "
                        "domain is its employer half, so there was no domain for "
                        "automatic resolution to establish."
                        if supplied.derived
                        else "The operator supplied this company website at intake, so "
                        "there was no domain for automatic resolution to establish."
                    )
                    + " Company research is unaffected and still runs."
                ),
                "supplied_domain": supplied.normalized,
                "supplied_raw_value": supplied.raw,
                "supplied_source_type": supplied.source_type,
                "supplied_source_id": str(supplied.source_id),
                "domain_source": supplied.origin,
                "derived_from_email": supplied.derived_from_email,
            }
        if contact.company_id is not None:
            return {
                "attempted": False,
                "reason_code": "existing_company_link",
                "reason": (
                    "The Contact already carried the permanent Company edge, so there "
                    "was nothing for automatic resolution to establish."
                ),
            }
        return {
            "attempted": False,
            "reason_code": "domain_already_known",
            "reason": (
                "The Contact already carried a company domain, so there was nothing for "
                "automatic resolution to establish."
            ),
        }

    def _resolve_company_domain(self, context: AgentExecutionContext) -> dict[str, Any] | None:
        """Establish this Contact's company through the shared resolution process.

        Returns what the decision said, or a note saying why resolution was not
        attempted, or ``None`` when there was nothing to attempt. "Not attempted"
        and "attempted and found nothing" are different facts about a Contact and
        an operator acts on them differently, so they are never reported as one.

        **A missing provider means no attempt, not a recorded failure.** This is
        the same rule intake and the backfill pass apply, for the same reason: with
        no usable provider the policy could only conclude "the lookup was not run",
        which is the absence of a decision rather than a decision — and because a
        recorded decision is not recalculated without an explicit force, writing
        that non-decision would stop this Contact ever resolving automatically
        again. So nothing is written and the job blocks exactly as it did before,
        which is where it would have been anyway.

        Note which switch is *not* consulted: capture promotion. That control
        governs turning captures into Contacts, and requiring it here would tie a
        spreadsheet-acquired Contact back to the browser extension — the precise
        coupling this repair removes.
        """

        contact = context.contact
        if not contact.company_name:
            return None

        settings = get_settings()
        if not operational.enabled(
            context.session, "automatic_company_domain_resolution", settings
        ):
            return {
                "attempted": False,
                "skipped_because": "automatic_company_domain_resolution is switched off",
            }

        # Imported here rather than at module scope: the resolution package pulls
        # in the provider and model seams, and the common case for this Agent is a
        # Contact whose company is already established.
        from app.services.resolution import service as resolution_service

        access_factory = self._access_factory or resolution_service.provider_access_for
        access = access_factory(context.session, settings)
        if not access.available:
            return {
                "attempted": False,
                "skipped_because": (
                    "no company-domain provider is available, so nothing was decided rather "
                    "than a non-decision being recorded"
                ),
            }
        model_factory = self._model_factory or resolution_service.model_access_for
        model = model_factory(context.session, settings)

        try:
            outcome = resolution_service.resolve_contact(
                context.session,
                contact=contact,
                access=access,
                model=model,
                actor="company-agent",
                # Never force: a decision already recorded for this Contact —
                # including an operator's correction — is a decision, and
                # recalculating over it would discard it.
                force=False,
            )
        except resolution_service.ResolutionError as exc:
            return {"attempted": True, "error": str(exc)}

        return {
            "attempted": True,
            "subject": "contact",
            "decision_id": str(outcome.decision.id),
            **outcome.summary(),
        }

    @staticmethod
    def _unresolved_reason(resolution: dict[str, Any] | None) -> str:
        """Why this Contact still has no company, in the operator's terms."""

        if resolution is None:
            return "No observed or approved Contact company domain was available."
        if not resolution.get("attempted"):
            return (
                "No Contact company domain was available and automatic resolution did not "
                f"run: {resolution.get('skipped_because')}."
            )
        if resolution.get("error"):
            return f"Automatic company-domain resolution could not run: {resolution['error']}"
        reasons = ", ".join(str(reason) for reason in resolution.get("reasons") or ())
        return (
            "Automatic company-domain resolution ran and could not establish a domain"
            f"{f' ({reasons})' if reasons else ''}."
        )

    @staticmethod
    def _blocked_detail(
        context: AgentExecutionContext,
        *,
        match_key: str,
        candidate_ids: tuple[str, ...],
        reason: str,
        resolution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "company-agent-report/1",
            "domain_resolution_attempt": resolution,
            "identity": {
                "match_key": match_key,
                "match_value": context.contact.company_domain,
                "candidate_company_ids": list(candidate_ids),
                "selected_company_id": None,
                "company_action": "unresolved",
                "contact_link_action": "unchanged",
                "reason": reason,
                "evidence_references": [
                    f"capture:{context.job.capture_id}"
                    if context.job.capture_id is not None
                    else "contact:permanent_record"
                ],
            },
            "campaign_policy": {
                "allow_provisional_domains": context.campaign.allow_provisional_domains,
                "campaign_settings_version": context.campaign.settings_version,
                "source": "execution_snapshot",
            },
            "continuation": {
                "action": "block",
                "research_allowed": False,
                "research_reason": reason,
                "later_stages_allowed": False,
                "later_stages_reason": reason,
            },
        }


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
    """Gather sourced company facts through bounded Claude CLI web research.

    Thin on purpose: the state machine in ``app.services.research.agent``
    owns the decision. The only logic here is the framework-level gates -- the
    feature switches and the per-campaign ``live`` opt-in -- constructing the
    required Claude source, and translating the resulting step into the shared
    error vocabulary.

    **This is the only production Research implementation, and Claude web
    research is its required primary source.** A wholly model-based adapter used
    to live further down this file and shadowed the evidence-preserving path.
    Removing that implementation remains the right decision:

    * Research's job is to *read pages and record what they said*, with a URL and a
      retrieval time on every fact. Every claim it stores has to be checkable
      against the page it came from. A model asked to research a company returns
      prose that is plausible whether or not it read anything, and the difference
      is invisible downstream — which is precisely the failure the whole evidence
      chain exists to prevent.
    * The worker path writes all three artefacts the pipeline depends on: the raw
      worker payload verbatim, one versioned dossier interpreting it, and one
      INS-001 insight per sourced fact. The old model path wrote a submission and a
      dossier but no insights, so the Insights Agent downstream had nothing sourced
      to gate on.

    The existing RES-002 boundary is safe to use as the primary source because it
    returns ``SourcedFact`` values through strict validation, so a claim
    without an openable source URL and the supporting text from that page is
    discarded rather than stored. And it writes all three artefacts, under its own
    worker name, so nothing downstream has to guess which source a fact came from.

    ``workers_factory`` remains only as a compatibility injection seam for tests
    and diagnostics; production execution never calls it. ``fallback_factory``
    remains as a temporary alias for ``research_factory`` so existing test and
    deployment wiring does not need an unrelated flag-day rename. Insights and
    Personalization keep ``allowed_tools=()``; this is the one Research-side call
    that may reach the web, and it may reach nothing else.
    """

    agent_id = AgentIdentifier.RESEARCH

    def __init__(
        self,
        *,
        workers_factory: Callable[..., Any] | None = None,
        research_factory: Callable[[Settings], Any] | None = None,
        fallback_factory: Callable[[Settings], Any] | None = None,
    ) -> None:
        # Injection seams for tests, mirroring VerificationAgentAdapter: the
        # suite must be able to run the real loop with a fake source, and must
        # never shell out to a real Claude CLI.
        self._workers_factory = workers_factory
        if research_factory is not None and fallback_factory is not None:
            raise ValueError("pass research_factory or legacy fallback_factory, not both")
        self._research_factory = research_factory or fallback_factory

    def execute(self, context: AgentExecutionContext) -> AgentExecutionResult:
        from app.services.research.agent import ResearchStepKind, execute_step

        settings = get_settings()
        # The *effective* setting, not the environment's. An administrator turns
        # Company research on from the Admin Configuration screen and the next
        # job sees it; the previous read went straight to `FEATURES__` and made
        # the switch a deploy. Jobs already paused here are returned to the queue
        # by `orchestrator.reclaim_feature_paused_jobs` when it is turned on.
        #
        # Two operator controls stand in front of this stage and they are not
        # interchangeable. Since Claude became the required source,
        # `company_research` resolves to off whenever `research_claude_fallback`
        # is unavailable — so reading it alone would answer `feature_disabled`,
        # "an administrator switched Research off", for a deployment whose
        # administrator switched nothing and is simply missing the prerequisite.
        # The prerequisite is therefore established first, and when it is absent
        # the refusal is left to `_primary_research` below, which classifies it
        # as `claude_research_unavailable` and writes the full operator record
        # rather than a bare code.
        claude_available = operational.enabled(
            context.session, "research_claude_fallback", settings
        )
        if claude_available and not operational.enabled(
            context.session, "company_research", settings
        ):
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

        research, research_unavailable_reason = self._primary_research(settings, context)
        step = execute_step(
            context.session,
            job=context.job,
            contact=context.contact,
            workers=(),
            actor=context.worker_id,
            primary_source=research,
            primary_unavailable_reason=research_unavailable_reason,
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

    def _primary_research(
        self, settings: Settings, context: AgentExecutionContext
    ) -> tuple[Any | None, str | None]:
        """Build the required Claude web-research source, or explain its absence.

        The legacy ``research_claude_fallback`` deployment control remains a
        required availability switch for this transition. Turning it off no
        longer restores deterministic Research: the execution is blocked. A
        Campaign's legacy ``claude_fallback=false`` opt-out has the same meaning.
        """

        if not operational.enabled(context.session, "research_claude_fallback", settings):
            return None, (
                "Claude web research is required, but the legacy "
                "research_claude_fallback availability control is switched off."
            )
        if context.config.get("claude_fallback") is False:
            return None, (
                "This Campaign's legacy claude_fallback=false setting disables the required "
                "Claude research source; deterministic Research is not used instead."
            )

        from app.services.research.fallback import ClaudeResearchFallback, FallbackLimits

        if self._research_factory is not None:
            source = self._research_factory(settings)
            if source is None:
                # Fail closed, and say why. Returning ``(None, None)`` here would
                # leave ``primary_mode`` false and the state machine would block
                # with ``no_workers_enabled`` — advice that points the operator at
                # the deterministic worker registry this repair exists to keep out
                # of production.
                return None, (
                    "The required Claude web-research source could not be constructed "
                    "for this execution."
                )
            return source, None
        return (
            ClaudeResearchFallback(
                thinker=ClaudeCliThinker(settings=settings),
                limits=FallbackLimits.from_settings(settings),
            ),
            None,
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
        self._provider_factory = provider_factory

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
        context.job.email = email
        context.job.policy_version = policy.version
        waterfall_policy_version_id: str | None = None
        providers_attempted: tuple[str, ...] = ()
        fallback_used = False
        fallback_condition: str | None = None
        fallback_reason: str | None = None
        if self._provider_factory is not None:
            provider = self._provider_factory(settings)
            if provider.simulated:
                rejected = refusal(
                    "verification_credentials_missing",
                    "Live verification credentials are not configured; simulator "
                    "output cannot complete the Verification Agent.",
                )
                raise AgentBlocked(
                    rejected.reason_code,
                    rejected.reason,
                    detail=self._refusal_output(rejected, context=context),
                )
            outcome = verification_service.verify_exact_address(
                session,
                context.job,
                provider=provider,
                settings=settings,
                policy=policy,
            )
            providers_attempted = (provider.name,)
        else:
            try:
                traversal = verification_waterfall.verify(
                    session, context.job, settings=settings, policy=policy
                )
            except WaterfallUnavailable as exc:
                rejected = refusal("verification_credentials_missing", str(exc))
                raise AgentBlocked(
                    rejected.reason_code,
                    rejected.reason,
                    detail=self._refusal_output(rejected, context=context),
                ) from None
            outcome = traversal.outcome
            waterfall_policy_version_id = traversal.policy_version_id
            providers_attempted = traversal.providers_attempted
            fallback_used = traversal.fallback_used
            fallback_condition = traversal.fallback_condition
            fallback_reason = traversal.fallback_reason

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
            "providers_attempted": list(providers_attempted),
            # Fallback provenance, recorded on the job's durable output so an
            # operator can answer "which provider settled this, was it the
            # fallback, and why did the fallback run" without reconstructing it.
            "fallback_used": fallback_used,
            "fallback_condition": fallback_condition,
            "fallback_reason": fallback_reason,
            "waterfall_policy_version_id": waterfall_policy_version_id,
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


def _seller_summary(effective: effective_offering.EffectiveCampaignOffering) -> str:
    """The offering half of an Insights prompt, in the Campaign's own precedence.

    The rendering of the Library half moved to
    ``app.services.seller.effective.library_summary`` so that the Campaign
    offering read could reuse it rather than grow a third copy; what is added
    here is the researched primary, when this Campaign has one. Insights is asked
    which recipient facts matter, and it can only answer that against what the
    Campaign is actually selling.
    """

    return effective_offering.with_primary(
        effective, effective_offering.library_summary(effective.seller)
    )


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


class InsightsAgentAdapter:
    """Turn the stored dossier into a few evidence-backed, seller-relevant claims.

    Every claim is written through ``insights.create_insight``, which refuses a
    supported claim that has no traceable source. A claim the model offered
    without a valid handle from this execution's committed Research catalog is
    therefore not stored as a weaker fact — it is dropped and counted, and the
    gaps the model named are stored as explicit unknowns. That asymmetry is the
    point: this stage may reduce what is asserted, never expand it.
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
        research = insights_lineage.current_state(context.session, company_id=company.id)
        if research is None:
            raise AgentBlocked(
                "research_knowledge_unavailable",
                "Insights needs current Company Research knowledge: a selected dossier version "
                "and the Research execution that committed it. This Company has neither yet.",
            )

        dossier = {
            name: getattr(research.dossier, name)
            for name in dossiers.SECTION_COLUMNS
            if getattr(research.dossier, name) is not None
        }
        catalog = employee_size.research_evidence_catalog(
            context.session,
            research_job_id=research.research_job.id,
            company_id=company.id,
        )
        prompt_catalog = employee_size.bounded_prompt_catalog(catalog)
        evidence_by_handle = {item.handle: item for item in prompt_catalog}
        effective = effective_offering.resolve(context.session, context.campaign)

        settings = get_settings()
        thinker = self._thinker_factory(settings)
        request = ThinkingRequest(
            prompt=prompts.insights_prompt(
                seller_summary=_seller_summary(effective),
                company_name=company.name,
                dossier=dossier,
                evidence_catalog=[item.prompt_value() for item in prompt_catalog],
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
        stored: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        max_claims = int(context.config.get("max_claims", 5))

        for index, item in enumerate(raw_claims[:max_claims]):
            if not isinstance(item, dict):
                dropped.append({"index": index, "reason": "not_an_object"})
                continue
            claim = _text(item.get("claim"), limit=2000)
            if claim is None:
                dropped.append({"index": index, "reason": "empty_claim"})
                continue
            raw_handles = item.get("evidence_handles")
            handle_values = raw_handles if isinstance(raw_handles, list) else []
            try:
                handles = (
                    tuple(dict.fromkeys(uuid.UUID(value) for value in handle_values))
                    if 0 < len(handle_values) <= 10
                    else ()
                )
            except (TypeError, ValueError, AttributeError):
                handles = ()
            if not handles or any(handle not in evidence_by_handle for handle in handles):
                dropped.append({"index": index, "reason": "unsourced", "claim": claim[:200]})
                continue
            evidence_inputs: list[insights_evidence.EvidenceInput] = []
            invalid_evidence = False
            for handle in handles:
                source = evidence_by_handle[handle].evidence
                if (
                    source.retrieved_at is None
                    or not source.evidence_summary
                    or source.confidence is None
                    or not source.extraction_method
                ):
                    invalid_evidence = True
                    break
                evidence_inputs.append(
                    insights_evidence.EvidenceInput(
                        source_url=source.source_url,
                        source_title=source.source_title,
                        published_at=source.published_at,
                        retrieved_at=source.retrieved_at,
                        excerpt=source.excerpt,
                        evidence_summary=source.evidence_summary,
                        confidence=source.confidence,
                        extraction_method=source.extraction_method,
                        freshness_at=source.freshness_at,
                        source_record_type="insight_evidence",
                        source_record_id=source.id,
                        version=source.version,
                    )
                )
            if invalid_evidence:
                dropped.append({"index": index, "reason": "invalid_evidence"})
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
                    evidence=evidence_inputs,
                    company_id=company.id,
                    # Stable per job and position, so a retried job re-uses the
                    # same records rather than duplicating them.
                    idempotency_key=f"insights-agent:{context.job.id}:{index}",
                    actor=context.worker_id,
                    producer_job_id=context.job.id,
                    dossier_version_id=research.dossier.id,
                    derivation_version=f"{answer.producer}/{answer.producer_version}"[:64],
                )
            except InsightError as exc:
                dropped.append({"index": index, "reason": "rejected", "detail": str(exc)[:200]})
                continue
            stored.append(
                {
                    "insight_id": str(insight.id),
                    "claim": claim,
                    "kind": kind.value,
                    "evidence_handles": [str(handle) for handle in handles],
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
                    producer_job_id=context.job.id,
                    dossier_version_id=research.dossier.id,
                    derivation_version=f"{answer.producer}/{answer.producer_version}"[:64],
                )
            except InsightError:
                # A gap that will not store is not worth failing the stage over.
                continue

        employee_insight = employee_size.derive_and_store(
            context.session,
            company_id=company.id,
            insights_job=context.job,
            dossier=research.dossier,
            catalog=prompt_catalog,
            model_output=answer.payload.get("employee_size"),
            actor=context.worker_id,
        )
        employee_payload = employee_insight.structured_payload or {}
        employee_eligible, employee_reason = employee_size.downstream_eligible(employee_insight)

        if not stored and not employee_eligible:
            raise AgentBlocked(
                "insufficient_evidence",
                "No claim in the answer carried a usable source, so nothing was stored. "
                "The Contact cannot be personalized from evidence that does not exist.",
                detail={
                    "dropped": dropped[:10],
                    "unknowns_recorded": len(unknown_texts),
                    "employee_size_insight_id": str(employee_insight.id),
                    "employee_size_status": employee_payload.get("status"),
                },
                # The unknown records above are real writes worth keeping.
                preserve_outcome=True,
            )

        output = {
            "company_id": str(company.id),
            "research_job_id": str(research.research_job.id),
            "submission_id": str(research.submission.id),
            "dossier_version_id": str(research.dossier.id),
            "dossier_version": research.dossier.version_number,
            "insights_stored": len(stored),
            "claims_dropped": len(dropped),
            "unknowns_recorded": len(unknown_texts),
            "insights": stored,
            "dropped": dropped[:10],
            "employee_size_insight_id": str(employee_insight.id),
            "employee_size_status": employee_payload.get("status"),
            "employee_size_band": employee_payload.get("normalized_band"),
            "employee_size_downstream_eligible": employee_eligible,
            "employee_size_eligibility_reason": employee_reason,
            "producer": answer.producer,
        }
        return AgentExecutionResult(
            outcome_committed=True,
            result={"domain_outcome": "insights_recorded", **output},
            output_reference=output,
        )


class PersonalizationAgentAdapter:
    """Write outreach copy from stored evidence, unapproved, for one Contact.

    Three things this Agent deliberately does not do. It does not approve
    anything — neither a ``DraftVersion`` nor a sequence message carries any
    authority, and the human decision stays a separate record. It does not
    personalize from anything except context selected by the deterministic
    policy gate. Weak evidence is omitted and may lead to a valid offering-led
    result, so an unsourced prospect claim cannot reach an email through this
    path. And it re-checks the suppression ledger immediately before drafting,
    because an entry added while the job waited in the queue must still stop it:
    writing to someone is what suppression exists to prevent, and drafting is
    the first step of writing.

    **One stage, two shapes.** Personalization remains exactly one Agent stage
    with exactly one Agent Job. Seven follow-ups did not become seven stages or
    seven jobs: a sequence is one generation unit that either succeeds whole or
    fails whole, and modelling it as seven stages would have made "the
    Personalization stage completed" a statement about nothing in particular.

    Which shape runs is decided by two switches that must both be on — the
    deployment ``email_sequences`` flag and the Campaign's own opt-in. With
    either off, this adapter does precisely what it did before sequences
    existed, down to the audit action and the output keys.
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
        policy = personalization_policy.active_policy(session)
        if policy is None:
            raise AgentBlocked(
                "personalization_policy_missing",
                "No Personalization policy version is active. Activate one in Admin Agent Studio.",
            )
        settings = get_settings()
        thinker = self._thinker_factory(settings)
        if sequence_mode_enabled(context.session, settings, context.campaign):
            return self._execute_sequence(context, policy=policy, thinker=thinker)
        try:
            generated = personalization_generation.generate(
                session,
                membership=context.membership,
                policy=policy,
                thinker=thinker,
                max_words=int(context.config.get("max_words", 150)),
                timeout_seconds=float(context.config.get("timeout_seconds", 240.0)),
                purpose="email_personalization",
            )
        except ThinkingError as exc:
            raise _translate_thinking_error(exc) from exc
        except personalization_generation.PreviewError as exc:
            if exc.code == "citation_not_supplied":
                raise AgentTerminalError(exc.code, str(exc)) from exc
            raise AgentBlocked(exc.code, str(exc)) from exc

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
            subject=generated.subject,
            body=generated.body,
            rationale=generated.rationale,
            personalization_policy_version_id=generated.policy_version_id,
            personalization_strategy_id=generated.strategy_id,
            personalization_decision=generated.decision.summary(),
            producer=generated.producer,
            producer_version=generated.producer_version,
            created_by=f"{generated.producer}/{generated.producer_version}",
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
                "evidence_insight_ids": list(generated.evidence_insight_ids),
                "personalization_policy_version_id": str(generated.policy_version_id),
                "personalization_policy_version_number": generated.policy_version_number,
                "personalization_strategy_id": generated.strategy_id,
                "company_intelligence_version_id": (
                    str(generated.decision.intelligence.version_id)
                    if generated.decision.intelligence is not None
                    and generated.decision.intelligence.version_id is not None
                    else None
                ),
                "company_intelligence_used": (
                    generated.decision.intelligence.used
                    if generated.decision.intelligence is not None
                    else None
                ),
            },
        )

        output = {
            "draft_version_id": str(draft.id),
            "version_number": next_number,
            "subject": generated.subject,
            "body": generated.body,
            "rationale": generated.rationale,
            "evidence_insight_ids": list(generated.evidence_insight_ids),
            "evidence_supplied": len(generated.decision.used),
            "approved": False,
            "producer": generated.producer,
            "producer_version": generated.producer_version,
            "personalization_policy_version_id": str(generated.policy_version_id),
            "personalization_policy_version_number": generated.policy_version_number,
            "personalization_strategy_id": generated.strategy_id,
            "personalization_decision": generated.decision.summary(),
            "warnings": list(generated.warnings),
        }
        return AgentExecutionResult(
            outcome_committed=True,
            result={"domain_outcome": "draft_created", **output},
            output_reference=output,
        )

    def _execute_sequence(
        self,
        context: AgentExecutionContext,
        *,
        policy: PersonalizationPolicyVersion,
        thinker: Thinker,
    ) -> AgentExecutionResult:
        """Produce one seven-message sequence, or fail without persisting one.

        The digest is computed and checked **before** the model is called, so an
        unchanged input costs nothing and a retry after a committed sequence
        costs nothing either. Everything after the call is validated in full
        before a single row is written, and the write is one flush — there is no
        arrangement of failures that leaves six messages behind and reports a
        complete stage.
        """

        session = context.session
        try:
            digest = sequence_generation.precompute_digest(
                session, membership=context.membership, policy=policy
            )
        except CadenceError as exc:
            raise AgentBlocked("sequence_cadence_invalid", str(exc)) from exc
        except sequence_generation.SequenceGenerationError as exc:
            raise AgentBlocked(exc.code, str(exc)) from exc

        existing = sequence_persistence.existing_for_digest(
            session, campaign_contact_id=context.membership.id, input_digest=digest
        )
        if existing is not None:
            # Nothing about the inputs has changed since this sequence was
            # written, so re-running the stage must not re-write it and must not
            # spend. The stage still completes: the outcome it is responsible
            # for exists.
            return self._sequence_result(session, sequence=existing, reused=True)

        try:
            generated = sequence_generation.generate_sequence(
                session,
                membership=context.membership,
                policy=policy,
                thinker=thinker,
                timeout_seconds=float(context.config.get("timeout_seconds", 420.0)),
                purpose="email_sequence_generation",
            )
        except ThinkingError as exc:
            raise _translate_thinking_error(exc) from exc
        except SequenceValidationError as exc:
            # Content that cannot be shown to a human is a terminal outcome, not
            # a retryable one: running the same inputs again produces the same
            # refusal, and the bounded findings say why.
            raise AgentTerminalError(exc.code, str(exc)) from exc
        except CadenceError as exc:
            raise AgentBlocked("sequence_cadence_invalid", str(exc)) from exc
        except sequence_generation.SequenceGenerationError as exc:
            if exc.code in {"citation_not_supplied", "sequence_invalid_purpose"}:
                raise AgentTerminalError(exc.code, str(exc)) from exc
            raise AgentBlocked(exc.code, str(exc)) from exc

        sequence = sequence_persistence.persist_sequence(
            session,
            membership=context.membership,
            contact=context.contact,
            generated=generated,
            agent_job_id=context.job.id,
            actor=context.worker_id,
        )
        return self._sequence_result(session, sequence=sequence, reused=False)

    @staticmethod
    def _sequence_result(
        session: Session, *, sequence: EmailSequence, reused: bool
    ) -> AgentExecutionResult:
        rows = sequence_read.message_rows(session, sequence=sequence)
        reviewed_by_human = any(row.human_reviewed for row in rows)
        output = {
            "sequence_id": str(sequence.id),
            "sequence_key": str(sequence.sequence_key),
            "sequence_version": sequence.sequence_version,
            "message_count": len(rows),
            "positions": [row.position for row in rows],
            "initial_subject": rows[0].subject if rows else None,
            "input_digest": sequence.input_digest,
            "producer": sequence.producer,
            "producer_version": sequence.producer_version,
            "sequence_producer_version": sequence.sequence_producer_version,
            "personalization_policy_version_id": (
                str(sequence.personalization_policy_version_id)
                if sequence.personalization_policy_version_id
                else None
            ),
            "personalization_policy_version_number": (
                sequence.personalization_policy_version_number
            ),
            "personalization_strategy_id": sequence.personalization_strategy_id,
            "personalization_decision": sequence.personalization_decision,
            "cadence_source": sequence.cadence_source,
            "planned_span_days": sequence.planned_span_days,
            "validation_status": sequence.validation_status.value,
            "review_state": sequence.review_state.value,
            # Said explicitly rather than left to be inferred from an absence.
            #
            # A generated sequence is approved by default, so this reports the
            # derived state rather than a hardcoded ``False`` that stopped being
            # true. ``reviewed_by_human`` is what keeps the two apart, and the
            # two keys below are what stop either being read as sending
            # authority: nothing was sent, no external draft exists, and an
            # approval -- default or human -- changes neither.
            "approved": sequence.review_state is SequenceReviewState.APPROVED,
            "reviewed_by_human": reviewed_by_human,
            "sent": False,
            "external_drafts_created": 0,
            "reused_existing_sequence": reused,
        }
        return AgentExecutionResult(
            outcome_committed=True,
            result={"domain_outcome": "sequence_created", **output},
            output_reference=output,
        )


def sequence_mode_enabled(session: Session, settings: Settings, campaign: Campaign) -> bool:
    """Whether this Campaign Contact gets a sequence rather than a single draft.

    Two switches, both required. The operational setting decides whether the
    feature is in use at all; the Campaign opt-in decides whether *this* Campaign
    uses it. Requiring both is what stops turning the feature on from silently
    changing what every existing Campaign produces.

    ``session`` is here because the first of those switches is now an
    administrator's durable setting rather than an environment variable, and
    reading it needs the database.
    """

    return operational.enabled(session, "email_sequences", settings) and campaign_opted_in(campaign)


#: The adapter that runs for each Agent, unless a caller passes its own.
#:
#: **Exactly one adapter per Agent.** This module once defined
#: ``ResearchAgentAdapter`` twice — a worker-based one and, 460 lines later, a
#: model-based one that silently shadowed it. Python rebinds the name without
#: complaint, so the mapping below pointed at whichever definition came last and
#: the other became unreachable. The mapping is not a place a mistake like that
#: shows up: it reads correctly either way. It cost a red test suite and an
#: unreachable research implementation to notice.
#:
#: Two things now catch it. ``ruff``/``mypy`` report the redefinition (they always
#: did — the error was tolerated), and ``tests/test_research_agent.py`` asserts
#: this entry is the worker-based adapter by the seam only it has.
DEFAULT_ADAPTERS: dict[AgentIdentifier, AgentAdapter] = {
    AgentIdentifier.IDENTITY: IdentityAgentAdapter(),
    AgentIdentifier.COMPANY: CompanyAgentAdapter(),
    AgentIdentifier.RESEARCH: ResearchAgentAdapter(),
    AgentIdentifier.EMAIL: EmailAgentAdapter(),
    AgentIdentifier.VERIFICATION: VerificationAgentAdapter(),
    AgentIdentifier.INSIGHTS: InsightsAgentAdapter(),
    AgentIdentifier.PERSONALIZATION: PersonalizationAgentAdapter(),
}
