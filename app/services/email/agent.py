"""Durable, policy-bounded Email Agent state machine for Issue #224.

The Email Agent owns candidate policy and sequencing. Verification remains a
separate child Agent on the common Phase 2 queue: this service persists one
candidate attempt, idempotently enqueues one child job, and yields. It never
claims or executes that child and never calls a provider.

On resume, the parent reads :class:`VerificationDecision` from the child job's
committed result/error projection. Generic job success is deliberately
insufficient: an accepted address also needs the exact evidence reference and
must pass the existing freshness, provenance, and suppression gates.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.company_field_value import CompanyFieldValue
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.email_discovery import EmailCandidateAttempt, EmailCandidateAttemptStatus
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import (
    AgentIdentifier,
    AgentJobStatus,
    EmailCandidateSource,
    EmailVerificationResult,
    PipelineEventType,
    ResearchState,
)
from app.models.verification_job import AgentJob
from app.services.agents import jobs
from app.services.agents.registry import get_agent_spec
from app.services.audit import record_audit_event
from app.services.email.discovery_policy import (
    POLICY_IDENTIFIER,
    POLICY_VERSION,
    EmailDiscoveryPolicyDecision,
    EmailPolicyOutcome,
    EmployeeCountClass,
    EmployeeCountEvidence,
    EmployeeEvidenceFreshness,
    classify_employee_count,
    evaluate,
    evaluate_existing_accepted_email_reuse,
    evidence_freshness,
)
from app.services.imports.normalization import is_valid_email, normalize_domain, normalize_email
from app.services.pipeline import append_event
from app.services.resolution.gates import DownstreamStage, authorize_contact
from app.services.suppressions import evaluate_suppression
from app.services.verification.decisions import VerificationDecision
from app.services.verification.policy import VerificationPolicy, get_policy
from app.services.verification.provider import SIMULATOR_PROVIDER_LABEL

STATE_KEY = "email_discovery"
EMAIL_TASK_KIND = "discover_work_email"
EMAIL_REFRESH_TASK_KIND = "refresh_work_email"


class EmailExecutionOutcome(enum.StrEnum):
    """Truthful semantic outcomes exposed through the Phase 2 job."""

    EXISTING_EMAIL_REUSED = "existing_accepted_email_reused"
    VERIFIED_EMAIL_ACCEPTED = "verified_email_accepted"
    NO_VERIFIED_ADDRESS = "no_verified_address"
    EMPLOYEE_COUNT_UNKNOWN = "employee_count_unknown"
    EMPLOYEE_COUNT_STALE = "employee_count_stale"
    MISSING_OR_UNUSABLE_IDENTITY = "missing_or_unusable_identity"
    COMPANY_UNAVAILABLE = "company_unavailable"
    DOMAIN_UNAVAILABLE = "domain_unavailable"
    DOMAIN_INELIGIBLE = "domain_ineligible"
    SUPPRESSED = "suppressed"
    AGENT_DISABLED = "agent_disabled"
    AGENT_PAUSED = "agent_paused"
    CAMPAIGN_OVERRIDE_DISABLED = "campaign_override_disabled"
    CAMPAIGN_CONTACT_INELIGIBLE = "campaign_contact_ineligible"
    WAITING_ON_VERIFICATION = "waiting_on_verification"
    RETRYABLE_VERIFICATION_DEPENDENCY = "retryable_verification_dependency"
    TERMINAL_VERIFICATION_FAILURE = "terminal_verification_failure"
    TERMINAL_VERIFICATION_REFUSAL = "terminal_verification_refusal"
    SIMULATED_VERIFICATION_REFUSED = "simulated_verification_refused"


class EmailExecutionStepKind(enum.StrEnum):
    COMPLETE = "complete"
    WAITING = "waiting"
    BLOCKED = "blocked"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class CommittedVerificationOutcome:
    """The authoritative decision and evidence committed by one child job."""

    decision: VerificationDecision | None
    verification_id: uuid.UUID | None = None
    reason_code: str | None = None
    reason: str | None = None
    reference: dict[str, Any] | None = None


@dataclass(frozen=True)
class EmailExecutionStep:
    """One restart-safe step returned to the common Agent adapter."""

    kind: EmailExecutionStepKind
    outcome: EmailExecutionOutcome
    result: dict[str, Any]
    output_reference: dict[str, Any]
    reason_code: str | None = None
    reason: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class ReusableAcceptedEmail:
    email: str
    evidence: ExactEmailVerification


class EmailAgentStateError(Exception):
    """A stored Email execution is inconsistent and cannot continue safely."""


class EmailAcceptedWriteRefused(EmailAgentStateError):
    """A final safety gate refused an otherwise accepted child outcome."""

    def __init__(self, code: EmailExecutionOutcome, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _state(job: AgentJob) -> dict[str, Any] | None:
    value = (job.result or {}).get(STATE_KEY)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise EmailAgentStateError("stored Email discovery state must be a JSON object")
    return dict(value)


def _write_state(job: AgentJob, state: dict[str, Any]) -> None:
    root = dict(job.result or {})
    root[STATE_KEY] = state
    job.result = root


def _persist_result(
    job: AgentJob,
    state: dict[str, Any],
    value: dict[str, Any],
) -> dict[str, Any]:
    """Persist an operator-readable checkpoint without mutating job intent."""

    root = dict(job.result or {})
    root.update(value)
    root[STATE_KEY] = state
    job.result = root
    return root


def _force_settings(job: AgentJob) -> tuple[bool, str | None]:
    force_refresh = bool((job.input_reference or {}).get("force_refresh", False))
    refresh_scope = (job.input_reference or {}).get("refresh_scope")
    if force_refresh:
        if not isinstance(refresh_scope, str) or not refresh_scope.strip():
            raise EmailAgentStateError("forced Email refresh requires an explicit refresh_scope")
        if len(refresh_scope) > 128:
            raise EmailAgentStateError("Email refresh_scope must be 128 characters or fewer")
        return True, refresh_scope.strip()
    if refresh_scope is not None:
        raise EmailAgentStateError("refresh_scope is valid only when force_refresh is true")
    return False, None


def enqueue_email_job(
    session: Session,
    *,
    contact_id: uuid.UUID,
    idempotency_scope: str = "default",
    campaign_id: uuid.UUID | None = None,
    campaign_contact_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    force_refresh: bool = False,
    refresh_scope: str | None = None,
    parent_job_id: uuid.UUID | None = None,
    priority: int = 100,
    actor: str = "system",
) -> tuple[AgentJob, bool]:
    """Create one idempotently scoped Email Agent job on the shared queue."""

    clean_scope = idempotency_scope.strip()
    if not clean_scope or len(clean_scope) > 128:
        raise EmailAgentStateError("idempotency_scope must be 1 to 128 characters")
    if force_refresh:
        if refresh_scope is None or not refresh_scope.strip():
            raise EmailAgentStateError("forced Email refresh requires refresh_scope")
        clean_refresh_scope = refresh_scope.strip()
        if len(clean_refresh_scope) > 128:
            raise EmailAgentStateError("refresh_scope must be 128 characters or fewer")
    elif refresh_scope is not None:
        raise EmailAgentStateError("refresh_scope is valid only for a forced refresh")
    else:
        clean_refresh_scope = None

    suffix = f"refresh:{clean_refresh_scope}" if force_refresh else f"standard:{clean_scope}"
    key = f"email:{contact_id}:{POLICY_VERSION}:{suffix}"
    return jobs.enqueue_job(
        session,
        agent_id=AgentIdentifier.EMAIL,
        idempotency_key=key,
        task_kind=EMAIL_REFRESH_TASK_KIND if force_refresh else EMAIL_TASK_KIND,
        max_attempts=get_agent_spec(AgentIdentifier.EMAIL).max_attempts,
        priority=priority,
        campaign_id=campaign_id,
        campaign_contact_id=campaign_contact_id,
        contact_id=contact_id,
        company_id=company_id,
        entity_type="contact",
        entity_id=contact_id,
        input_reference={
            "contact_id": str(contact_id),
            "campaign_id": str(campaign_id) if campaign_id else None,
            "campaign_contact_id": (str(campaign_contact_id) if campaign_contact_id else None),
            "force_refresh": force_refresh,
            "refresh_scope": clean_refresh_scope,
            "policy_identifier": POLICY_IDENTIFIER,
            "policy_version": POLICY_VERSION,
        },
        parent_job_id=parent_job_id,
        actor=actor,
    )


def _verification_child_input(
    *,
    parent_job: AgentJob,
    attempt: EmailCandidateAttempt,
    verification_policy: VerificationPolicy,
) -> dict[str, Any]:
    """Immutable intent consumed by the authoritative Verification adapter."""

    return {
        "candidate_id": str(attempt.candidate_id),
        "candidate_attempt_id": str(attempt.id),
        "email": attempt.normalized_email,
        "policy_version": verification_policy.version,
        "requesting_email_job_id": str(parent_job.id),
        "force_refresh": attempt.force_refresh,
        "refresh_scope": attempt.refresh_scope,
    }


def _ensure_verification_child(
    session: Session,
    *,
    parent_job: AgentJob,
    attempt: EmailCandidateAttempt,
    verification_policy: VerificationPolicy,
    actor: str,
) -> AgentJob:
    """Idempotently enqueue exactly one Verification child for *attempt*."""

    spec = get_agent_spec(AgentIdentifier.VERIFICATION)
    input_reference = _verification_child_input(
        parent_job=parent_job,
        attempt=attempt,
        verification_policy=verification_policy,
    )
    child, created = jobs.enqueue_job(
        session,
        agent_id=AgentIdentifier.VERIFICATION,
        idempotency_key=(
            f"email:{parent_job.id}:candidate:{attempt.candidate_index}:"
            f"verification:{verification_policy.version}"
        ),
        task_kind="verify_email_candidate",
        max_attempts=spec.max_attempts,
        priority=parent_job.priority,
        email=attempt.normalized_email,
        policy_version=verification_policy.version,
        campaign_id=parent_job.campaign_id,
        campaign_contact_id=parent_job.campaign_contact_id,
        contact_id=parent_job.contact_id,
        company_id=attempt.company_id,
        entity_type="email_candidate_attempt",
        entity_id=attempt.id,
        input_reference=input_reference,
        parent_job_id=parent_job.id,
        actor=actor,
    )
    if (
        child.parent_job_id != parent_job.id
        or child.agent_id is not AgentIdentifier.VERIFICATION
        or child.entity_id != attempt.id
        or child.email != attempt.normalized_email
    ):
        raise EmailAgentStateError(
            "Verification enqueue returned a job outside the candidate intent"
        )
    if (
        not created
        and child.status is AgentJobStatus.PAUSED
        and child.error_class
        in {
            "requesting_email_agent_disabled",
            "requesting_email_agent_paused",
        }
    ):
        jobs.resume_paused(
            session,
            child,
            reason_codes=frozenset(
                {
                    "requesting_email_agent_disabled",
                    "requesting_email_agent_paused",
                }
            ),
        )
    if created and parent_job.campaign_contact_id is not None:
        append_event(
            session,
            campaign_contact_id=parent_job.campaign_contact_id,
            agent_id=AgentIdentifier.VERIFICATION,
            job_id=child.id,
            event_type=PipelineEventType.JOB_QUEUED,
            actor=actor,
            reason_code="email_candidate_child",
            reason_detail=(
                f"Verification child queued for Email candidate {attempt.candidate_index + 1}."
            ),
            detail={
                "parent_job_id": str(parent_job.id),
                "candidate_attempt_id": str(attempt.id),
                "candidate_index": attempt.candidate_index,
            },
        )
    return child


def _decision_payload(child: AgentJob) -> dict[str, Any] | None:
    """Return the adapter's committed output regardless of queue disposition."""

    value: object
    if child.status is AgentJobStatus.SUCCEEDED:
        value = child.result
    else:
        error = child.error
        value = error.get("detail") if isinstance(error, dict) else None
    return dict(value) if isinstance(value, dict) else None


def _committed_verification_outcome(
    child: AgentJob,
) -> CommittedVerificationOutcome | None:
    """Read one authoritative child decision after its transaction committed.

    Pending, leased, and running jobs have no committed decision. A retry
    projection is readable but remains owned by the child queue. Control pauses
    may have no Verification decision at all; those are returned as dependency
    blocks rather than relabelled as mailbox verdicts.
    """

    if child.status in {
        AgentJobStatus.PENDING,
        AgentJobStatus.LEASED,
        AgentJobStatus.IN_PROGRESS,
    }:
        return None
    payload = _decision_payload(child)
    raw_decision = payload.get("decision") if payload is not None else None
    decision: VerificationDecision | None = None
    if isinstance(raw_decision, str):
        try:
            decision = VerificationDecision(raw_decision)
        except ValueError as exc:
            raise EmailAgentStateError("Verification child committed an unknown decision") from exc

    if decision is None and child.status is AgentJobStatus.RETRY_SCHEDULED:
        raise EmailAgentStateError(
            "retry-scheduled Verification child has no committed RETRY_LATER decision"
        )
    if decision is None and child.status in {
        AgentJobStatus.SUCCEEDED,
        AgentJobStatus.FAILED,
        AgentJobStatus.CANCELLED,
    }:
        raise EmailAgentStateError(
            "terminal Verification child has no committed authoritative decision"
        )

    raw_id = payload.get("verification_id") if payload is not None else None
    verification_id = _uuid(raw_id) or child.verification_id
    reason_code = (
        str(payload.get("reason_code"))
        if payload is not None and payload.get("reason_code") is not None
        else child.error_class
    )
    reason = (
        str(payload.get("reason"))
        if payload is not None and payload.get("reason") is not None
        else child.last_error
    )
    return CommittedVerificationOutcome(
        decision=decision,
        verification_id=verification_id,
        reason_code=reason_code,
        reason=reason,
        reference=payload or {},
    )


def _employee_evidence(session: Session, company: Company) -> EmployeeCountEvidence:
    winner = session.scalars(
        select(CompanyFieldValue).where(
            CompanyFieldValue.company_id == company.id,
            CompanyFieldValue.field_name == "company_size",
            CompanyFieldValue.is_current_winner.is_(True),
        )
    ).one_or_none()
    if winner is None:
        return EmployeeCountEvidence(
            evidence_id=None,
            raw_value=None,
            source_reference=None,
            observed_at=None,
            ingested_at=None,
            source_policy_version=None,
            source_marked_stale=company.research_state is ResearchState.STALE,
        )
    return EmployeeCountEvidence(
        evidence_id=str(winner.id),
        raw_value=winner.value,
        source_reference=winner.source_reference or str(winner.id),
        observed_at=winner.observed_at,
        ingested_at=winner.ingested_at,
        source_policy_version=winner.policy_version,
        source_marked_stale=company.research_state is ResearchState.STALE,
    )


def _policy_json(decision: EmailDiscoveryPolicyDecision) -> dict[str, Any]:
    evidence = decision.evidence
    return {
        "policy_identifier": POLICY_IDENTIFIER,
        "policy_version": POLICY_VERSION,
        "policy_outcome": decision.outcome.value,
        "normalization_version": decision.normalization_version,
        "employee_count_class": decision.employee_count_class.value,
        "employee_evidence": {
            "id": evidence.evidence_id,
            "raw_value": evidence.raw_value,
            "source_reference": evidence.source_reference,
            "observed_at": (
                evidence.observed_at.isoformat() if evidence.observed_at is not None else None
            ),
            "ingested_at": (
                evidence.ingested_at.isoformat() if evidence.ingested_at is not None else None
            ),
            "effective_at": (
                evidence.effective_at.isoformat() if evidence.effective_at is not None else None
            ),
            "freshness": decision.evidence_freshness.value,
            "source_policy_version": evidence.source_policy_version,
        },
        "normalized_domain": decision.normalized_domain,
        "ordered_candidate_formats": list(decision.ordered_formats),
        "candidates": [
            {
                "candidate_index": index,
                "format": candidate.format_id,
                "local_part": candidate.local_part,
                "email": candidate.email,
            }
            for index, candidate in enumerate(decision.candidates)
        ],
        "current_candidate_index": 0,
        "accepted_candidate_index": None,
        "terminal_outcome": None,
        "reason": decision.reason,
    }


def _candidate_for_email(
    session: Session,
    *,
    contact: Contact,
    email: str,
) -> EmailCandidate | None:
    return session.scalars(
        select(EmailCandidate).where(
            EmailCandidate.contact_id == contact.id,
            EmailCandidate.email == email,
        )
    ).one_or_none()


def _materialize_candidates(
    session: Session,
    *,
    contact: Contact,
    decision: EmailDiscoveryPolicyDecision,
) -> list[EmailCandidate]:
    """Reuse the accepted candidate table without deleting its audit history."""

    rows: list[EmailCandidate] = []
    for index, candidate in enumerate(decision.candidates):
        row = _candidate_for_email(session, contact=contact, email=candidate.email)
        if row is None:
            row = EmailCandidate(
                contact_id=contact.id,
                email=candidate.email,
                source=EmailCandidateSource.GENERATED,
                pattern=candidate.format_id,
                local_part=candidate.local_part,
                domain=decision.normalized_domain or "",
                engine_version=POLICY_VERSION,
                rank=index,
                rank_score=float(index),
                rank_reason=(
                    f"locked {POLICY_IDENTIFIER} position {index + 1}; "
                    "ordering is policy, not mailbox evidence"
                ),
                selected=False,
                selection_reason=None,
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError:
                row = _candidate_for_email(session, contact=contact, email=candidate.email)
                if row is None:  # pragma: no cover - defensive
                    raise
        rows.append(row)
    return rows


def _attempt(
    session: Session,
    *,
    job: AgentJob,
    contact: Contact,
    company: Company,
    membership: CampaignContact | None,
    state: dict[str, Any],
    candidate: EmailCandidate,
    index: int,
    force_refresh: bool,
    refresh_scope: str | None,
) -> EmailCandidateAttempt:
    existing = session.scalars(
        select(EmailCandidateAttempt).where(
            EmailCandidateAttempt.email_job_id == job.id,
            EmailCandidateAttempt.candidate_index == index,
        )
    ).one_or_none()
    if existing is not None:
        return existing

    candidate_state = state["candidates"][index]
    evidence = state["employee_evidence"]
    evidence_id = _uuid(evidence.get("id"))
    effective_at_raw = evidence.get("effective_at")
    effective_at = (
        datetime.fromisoformat(effective_at_raw) if isinstance(effective_at_raw, str) else None
    )
    row = EmailCandidateAttempt(
        email_job_id=job.id,
        candidate_id=candidate.id,
        contact_id=contact.id,
        company_id=company.id,
        campaign_id=job.campaign_id,
        campaign_contact_id=membership.id if membership else None,
        candidate_index=index,
        candidate_format=str(candidate_state["format"]),
        normalized_email=str(candidate_state["email"]),
        normalized_domain=str(state["normalized_domain"]),
        policy_identifier=str(state["policy_identifier"]),
        policy_version=str(state["policy_version"]),
        employee_count_class=str(state["employee_count_class"]),
        employee_evidence_id=evidence_id,
        employee_evidence_reference=evidence.get("source_reference"),
        employee_evidence_at=effective_at,
        employee_evidence_freshness=str(evidence["freshness"]),
        force_refresh=force_refresh,
        refresh_scope=refresh_scope,
        status=EmailCandidateAttemptStatus.PENDING.value,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        row = session.scalars(
            select(EmailCandidateAttempt).where(
                EmailCandidateAttempt.email_job_id == job.id,
                EmailCandidateAttempt.candidate_index == index,
            )
        ).one()
    return row


def _policy_block(
    decision: EmailDiscoveryPolicyDecision,
) -> tuple[EmailExecutionOutcome, str]:
    if decision.outcome is EmailPolicyOutcome.EMPLOYEE_COUNT_UNKNOWN:
        return EmailExecutionOutcome.EMPLOYEE_COUNT_UNKNOWN, decision.outcome.value
    if decision.outcome is EmailPolicyOutcome.EMPLOYEE_COUNT_STALE:
        return EmailExecutionOutcome.EMPLOYEE_COUNT_STALE, decision.outcome.value
    if decision.outcome in {
        EmailPolicyOutcome.UNUSABLE_FIRST_NAME,
        EmailPolicyOutcome.UNUSABLE_LAST_NAME,
    }:
        return (
            EmailExecutionOutcome.MISSING_OR_UNUSABLE_IDENTITY,
            decision.outcome.value,
        )
    return EmailExecutionOutcome.DOMAIN_INELIGIBLE, decision.outcome.value


def _persisted_evidence_block(
    *,
    state: dict[str, Any],
    current: EmployeeCountEvidence,
    now: datetime,
) -> tuple[EmailExecutionOutcome, str] | None:
    """Refuse to mix a stored candidate plan with changed or stale evidence."""

    stored = state.get("employee_evidence")
    if not isinstance(stored, dict):
        raise EmailAgentStateError("stored Email policy has no employee evidence object")
    freshness = evidence_freshness(current, now=now)
    classification = classify_employee_count(current.raw_value)
    if freshness is not EmployeeEvidenceFreshness.FRESH:
        classification = EmployeeCountClass.UNKNOWN
    # Size becoming unknown or stale mid-execution no longer stops the run: the
    # candidate order was already chosen and re-deriving it would only reshuffle
    # three formats. What still stops it is the evidence being *different* from
    # what the order was chosen against — that means the plan in flight was built
    # on a fact that has since changed, and finishing it would silently attribute
    # results to a classification nobody made.
    if (
        stored.get("id") != current.evidence_id
        or state.get("employee_count_class") != classification.value
    ):
        return (
            EmailExecutionOutcome.EMPLOYEE_COUNT_UNKNOWN,
            "employee-count evidence changed after candidate policy selection; "
            "an explicitly scoped Email refresh is required",
        )
    return None


def _latest_exact_evidence(
    session: Session,
    *,
    contact_id: uuid.UUID,
    email: str,
) -> ExactEmailVerification | None:
    return session.scalars(
        select(ExactEmailVerification)
        .where(
            ExactEmailVerification.contact_id == contact_id,
            ExactEmailVerification.email == email,
        )
        .order_by(
            ExactEmailVerification.checked_at.desc(),
            ExactEmailVerification.created_at.desc(),
        )
        .limit(1)
    ).first()


def _production_eligible_evidence(
    evidence: ExactEmailVerification,
    *,
    email: str,
    contact_id: uuid.UUID,
    verification_policy: VerificationPolicy,
    now: datetime,
) -> bool:
    return (
        evidence.email == email
        and evidence.contact_id == contact_id
        and evidence.result is EmailVerificationResult.VALID
        and evidence.is_role is not True
        and evidence.provider != SIMULATOR_PROVIDER_LABEL
        and verification_policy.is_fresh(evidence.result, evidence.checked_at, now)
    )


def reusable_accepted_email(
    session: Session,
    *,
    contact: Contact,
    company: Company,
    verification_policy: VerificationPolicy,
    now: datetime,
) -> ReusableAcceptedEmail | None:
    """Return a fresh, production-eligible accepted address for this boundary."""

    normalized = normalize_email(contact.email)
    domain = normalize_domain(company.domain)
    if (
        normalized is None
        or not is_valid_email(normalized)
        or domain is None
        or normalized.rpartition("@")[2] != domain
    ):
        return None
    suppression = evaluate_suppression(session, email=normalized, domain=domain)
    if suppression.blocked:
        return None
    evidence = _latest_exact_evidence(
        session,
        contact_id=contact.id,
        email=normalized,
    )
    if evidence is None or not _production_eligible_evidence(
        evidence,
        email=normalized,
        contact_id=contact.id,
        verification_policy=verification_policy,
        now=now,
    ):
        return None
    return ReusableAcceptedEmail(email=normalized, evidence=evidence)


def _cancel_unstarted_child(
    session: Session,
    *,
    attempt: EmailCandidateAttempt,
    reason: str,
) -> None:
    if attempt.verification_job_id is None:
        return
    child = session.get(AgentJob, attempt.verification_job_id)
    if child is None or child.status not in {
        AgentJobStatus.PENDING,
        AgentJobStatus.RETRY_SCHEDULED,
        AgentJobStatus.PAUSED,
    }:
        return
    jobs.cancel_job(
        session,
        child,
        reason=reason,
        reason_code="existing_email_reused",
    )


def _reuse_step(
    session: Session,
    *,
    job: AgentJob,
    reusable: ReusableAcceptedEmail,
    state: dict[str, Any],
) -> EmailExecutionStep:
    index = int(state.get("current_candidate_index", 0))
    attempt = session.scalars(
        select(EmailCandidateAttempt).where(
            EmailCandidateAttempt.email_job_id == job.id,
            EmailCandidateAttempt.candidate_index == index,
        )
    ).one_or_none()
    if attempt is not None and attempt.status not in {
        EmailCandidateAttemptStatus.ACCEPTED.value,
        EmailCandidateAttemptStatus.REJECTED.value,
        EmailCandidateAttemptStatus.TERMINAL_NO_RESULT.value,
        EmailCandidateAttemptStatus.REFUSED.value,
        EmailCandidateAttemptStatus.SIMULATED.value,
    }:
        _cancel_unstarted_child(
            session,
            attempt=attempt,
            reason="A fresh accepted email was committed by another execution.",
        )
        attempt.status = EmailCandidateAttemptStatus.REFUSED.value
        attempt.verification_decision = "superseded_by_existing_email"
        attempt.refusal_reason = "A fresh accepted email was committed by another execution."
        attempt.resolved_at = _now()
    state["terminal_outcome"] = EmailExecutionOutcome.EXISTING_EMAIL_REUSED.value
    state.pop("blocked_outcome", None)
    state["accepted_email"] = reusable.email
    state["verification_id"] = str(reusable.evidence.id)
    result = {
        "domain_outcome": EmailExecutionOutcome.EXISTING_EMAIL_REUSED.value,
        "email": reusable.email,
        "verification_id": str(reusable.evidence.id),
        "verification_policy_version": reusable.evidence.policy_version,
        "verification_provider": reusable.evidence.provider,
        "email_policy_identifier": POLICY_IDENTIFIER,
        "email_policy_version": POLICY_VERSION,
        "provider_call_created": False,
    }
    persisted = _persist_result(job, state, result)
    return EmailExecutionStep(
        kind=EmailExecutionStepKind.COMPLETE,
        outcome=EmailExecutionOutcome.EXISTING_EMAIL_REUSED,
        result=persisted,
        output_reference={
            "email": reusable.email,
            "verification_id": str(reusable.evidence.id),
            "reused": True,
        },
    )


def _terminal_replay_step(
    *,
    job: AgentJob,
    state: dict[str, Any],
) -> EmailExecutionStep | None:
    """Return the already-committed terminal outcome without repeating writes."""

    raw_outcome = state.get("terminal_outcome")
    if not isinstance(raw_outcome, str):
        return None
    try:
        outcome = EmailExecutionOutcome(raw_outcome)
    except ValueError as exc:
        raise EmailAgentStateError(
            "stored Email execution has an unknown terminal outcome"
        ) from exc

    if outcome in {
        EmailExecutionOutcome.EXISTING_EMAIL_REUSED,
        EmailExecutionOutcome.VERIFIED_EMAIL_ACCEPTED,
    }:
        kind = EmailExecutionStepKind.COMPLETE
    elif outcome in {
        EmailExecutionOutcome.EMPLOYEE_COUNT_UNKNOWN,
        EmailExecutionOutcome.EMPLOYEE_COUNT_STALE,
        EmailExecutionOutcome.MISSING_OR_UNUSABLE_IDENTITY,
        EmailExecutionOutcome.COMPANY_UNAVAILABLE,
        EmailExecutionOutcome.DOMAIN_UNAVAILABLE,
        EmailExecutionOutcome.DOMAIN_INELIGIBLE,
        EmailExecutionOutcome.SUPPRESSED,
        EmailExecutionOutcome.CAMPAIGN_CONTACT_INELIGIBLE,
        EmailExecutionOutcome.SIMULATED_VERIFICATION_REFUSED,
    }:
        kind = EmailExecutionStepKind.BLOCKED
    else:
        kind = EmailExecutionStepKind.TERMINAL

    result = dict(job.result or {})
    result.setdefault("domain_outcome", outcome.value)
    result[STATE_KEY] = state
    output = {
        "email": state.get("accepted_email"),
        "verification_id": state.get("verification_id"),
        "accepted_candidate_index": state.get("accepted_candidate_index"),
        "replayed": True,
    }
    return EmailExecutionStep(
        kind=kind,
        outcome=outcome,
        result=result,
        output_reference=output,
        reason_code=None if kind is EmailExecutionStepKind.COMPLETE else outcome.value,
        reason=state.get("reason") if isinstance(state.get("reason"), str) else None,
    )


def _accepted_write(
    session: Session,
    *,
    job: AgentJob,
    contact: Contact,
    company: Company,
    attempt: EmailCandidateAttempt,
    verification_outcome: CommittedVerificationOutcome,
    verification_policy: VerificationPolicy,
    now: datetime,
) -> tuple[EmailExecutionOutcome, ExactEmailVerification]:
    if (
        verification_outcome.decision is not VerificationDecision.ACCEPT
        or verification_outcome.verification_id is None
    ):
        raise EmailAcceptedWriteRefused(
            EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL,
            "Verification ACCEPT lacked production-eligible exact-address evidence",
        )
    evidence = session.get(ExactEmailVerification, verification_outcome.verification_id)
    if evidence is None or not _production_eligible_evidence(
        evidence,
        email=attempt.normalized_email,
        contact_id=contact.id,
        verification_policy=verification_policy,
        now=now,
    ):
        raise EmailAcceptedWriteRefused(
            EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL,
            "Verification ACCEPT did not reference matching fresh production evidence",
        )

    domain = normalize_domain(company.domain)
    suppression = evaluate_suppression(
        session,
        email=attempt.normalized_email,
        domain=domain,
    )
    if suppression.blocked:
        raise EmailAcceptedWriteRefused(
            EmailExecutionOutcome.SUPPRESSED,
            suppression.blocked_reason
            or "suppression appeared before the accepted email could be written",
        )

    # A competing accepted result is evaluated before overwriting.  Fresh,
    # production-eligible evidence on the existing address wins; stale or
    # unsupported text does not outrank this exact Verification result.
    existing = reusable_accepted_email(
        session,
        contact=contact,
        company=company,
        verification_policy=verification_policy,
        now=now,
    )
    if existing is not None and existing.email != attempt.normalized_email:
        attempt.status = EmailCandidateAttemptStatus.REFUSED.value
        attempt.verification_id = evidence.id
        attempt.verification_decision = "superseded_by_existing_email"
        attempt.refusal_reason = (
            "A fresh accepted email was concurrently committed for this Contact."
        )
        attempt.resolved_at = now
        return EmailExecutionOutcome.EXISTING_EMAIL_REUSED, existing.evidence

    owner = session.scalars(
        select(Contact).where(
            Contact.email == attempt.normalized_email,
            Contact.id != contact.id,
            Contact.merged_into_id.is_(None),
        )
    ).first()
    if owner is not None:
        raise EmailAcceptedWriteRefused(
            EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL,
            "the verified address already belongs to another permanent Contact",
        )

    previous = contact.email
    if normalize_email(previous) != attempt.normalized_email:
        try:
            with session.begin_nested():
                contact.email = attempt.normalized_email
                session.flush()
        except IntegrityError as exc:
            raise EmailAcceptedWriteRefused(
                EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL,
                "the verified address was concurrently assigned to another Contact",
            ) from exc

    for candidate in session.scalars(
        select(EmailCandidate).where(EmailCandidate.contact_id == contact.id)
    ).all():
        candidate.selected = candidate.id == attempt.candidate_id
        candidate.selection_reason = (
            "accepted by the authoritative Verification Agent"
            if candidate.id == attempt.candidate_id
            else None
        )
    attempt.status = EmailCandidateAttemptStatus.ACCEPTED.value
    attempt.verification_id = evidence.id
    attempt.verification_decision = VerificationDecision.ACCEPT.value
    attempt.verification_result = verification_outcome.reference or {}
    attempt.resolved_at = now
    session.flush()
    record_audit_event(
        session,
        actor="email-agent",
        action="contact.email_accepted",
        entity_type="contact",
        entity_id=str(contact.id),
        previous_state=previous,
        new_state=attempt.normalized_email,
        reason="first production-eligible exact-address Verification result",
        context={
            "email_job_id": str(job.id),
            "candidate_attempt_id": str(attempt.id),
            "candidate_id": str(attempt.candidate_id),
            "verification_job_id": (
                str(attempt.verification_job_id)
                if attempt.verification_job_id is not None
                else None
            ),
            "verification_id": str(evidence.id),
            "verification_provider": evidence.provider,
            "verification_policy_version": evidence.policy_version,
            "email_policy_identifier": attempt.policy_identifier,
            "email_policy_version": attempt.policy_version,
            "employee_evidence_id": (
                str(attempt.employee_evidence_id)
                if attempt.employee_evidence_id is not None
                else None
            ),
        },
    )
    return EmailExecutionOutcome.VERIFIED_EMAIL_ACCEPTED, evidence


def _waiting_step(
    *,
    job: AgentJob,
    state: dict[str, Any],
    attempt: EmailCandidateAttempt,
    retryable: bool = False,
    reason: str | None = None,
) -> EmailExecutionStep:
    outcome = (
        EmailExecutionOutcome.RETRYABLE_VERIFICATION_DEPENDENCY
        if retryable
        else EmailExecutionOutcome.WAITING_ON_VERIFICATION
    )
    result = _persist_result(
        job,
        state,
        {
            "domain_outcome": outcome.value,
            "candidate_attempt_id": str(attempt.id),
            "candidate_index": attempt.candidate_index,
            "verification_job_id": (
                str(attempt.verification_job_id)
                if attempt.verification_job_id is not None
                else None
            ),
        },
    )
    return EmailExecutionStep(
        kind=EmailExecutionStepKind.WAITING,
        outcome=outcome,
        result=result,
        output_reference={
            "candidate_attempt_id": str(attempt.id),
            "candidate_index": attempt.candidate_index,
            "verification_job_id": (
                str(attempt.verification_job_id)
                if attempt.verification_job_id is not None
                else None
            ),
        },
        reason_code=outcome.value,
        reason=reason or "Waiting for the child Verification Agent outcome.",
        retryable=retryable,
    )


def execute_step(
    session: Session,
    *,
    job: AgentJob,
    contact: Contact,
    membership: CampaignContact | None,
    verification_policy: VerificationPolicy | None = None,
    now: datetime | None = None,
    actor: str = "email-agent",
) -> EmailExecutionStep:
    """Advance one durable Email execution until it must yield or terminate."""

    if job.agent_id is not AgentIdentifier.EMAIL:
        raise EmailAgentStateError("Email state machine received a non-Email Agent job")
    if job.contact_id != contact.id:
        raise EmailAgentStateError("Email Agent job and Contact identity do not match")
    now = now or _now()
    verification_policy = verification_policy or get_policy(get_settings())
    force_refresh, refresh_scope = _force_settings(job)
    # The Campaign owns two policy answers this execution needs: how far a
    # provisional company domain reaches, and whether employee size chooses the
    # candidate order. Read once here so both use the same Campaign row.
    campaign = session.get(Campaign, membership.campaign_id) if membership is not None else None
    consult_employee_size = campaign.consult_employee_size if campaign is not None else True

    # Lock the permanent person so a competing accepted-email write serializes
    # with this decision rather than racing it.
    locked_contact = session.scalars(
        select(Contact).where(Contact.id == contact.id).with_for_update()
    ).one()
    contact = locked_contact
    state = _state(job)
    if state is not None:
        replay = _terminal_replay_step(job=job, state=state)
        if replay is not None:
            return replay
    if contact.merged_into_id is not None:
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.BLOCKED,
            outcome=EmailExecutionOutcome.MISSING_OR_UNUSABLE_IDENTITY,
            result={"domain_outcome": "identity_ambiguous"},
            output_reference={"contact_id": str(contact.id)},
            reason_code="identity_ambiguous",
            reason="The Contact was merged or its permanent identity is no longer usable.",
        )
    if membership is not None and membership.eligibility_status.value != "eligible":
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.BLOCKED,
            outcome=EmailExecutionOutcome.CAMPAIGN_CONTACT_INELIGIBLE,
            result={
                "domain_outcome": EmailExecutionOutcome.CAMPAIGN_CONTACT_INELIGIBLE.value,
                "eligibility_status": membership.eligibility_status.value,
                "blocking_reasons": membership.blocking_reasons,
            },
            output_reference={"campaign_contact_id": str(membership.id)},
            reason_code=EmailExecutionOutcome.CAMPAIGN_CONTACT_INELIGIBLE.value,
            reason="The Campaign Contact is not currently eligible for Email discovery.",
        )

    if contact.company_id is None:
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.BLOCKED,
            outcome=EmailExecutionOutcome.COMPANY_UNAVAILABLE,
            result={"domain_outcome": EmailExecutionOutcome.COMPANY_UNAVAILABLE.value},
            output_reference={"contact_id": str(contact.id)},
            reason_code=EmailExecutionOutcome.COMPANY_UNAVAILABLE.value,
            reason="The Contact has no canonical Company relationship.",
        )
    company = session.get(Company, contact.company_id)
    if company is None:  # pragma: no cover - protected by FK
        raise EmailAgentStateError("the Contact's canonical Company no longer exists")
    if job.company_id is not None and job.company_id != company.id:
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.BLOCKED,
            outcome=EmailExecutionOutcome.COMPANY_UNAVAILABLE,
            result={"domain_outcome": EmailExecutionOutcome.COMPANY_UNAVAILABLE.value},
            output_reference={"company_id": str(company.id)},
            reason_code="canonical_company_changed",
            reason=(
                "The Contact's canonical Company changed during Email execution; "
                "an explicitly scoped refresh is required."
            ),
        )
    canonical_domain = normalize_domain(company.domain)
    if canonical_domain is None:
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.BLOCKED,
            outcome=EmailExecutionOutcome.DOMAIN_UNAVAILABLE,
            result={"domain_outcome": EmailExecutionOutcome.DOMAIN_UNAVAILABLE.value},
            output_reference={"company_id": str(company.id)},
            reason_code=EmailExecutionOutcome.DOMAIN_UNAVAILABLE.value,
            reason="The canonical Company has no usable domain.",
        )
    contact_domain = normalize_domain(contact.company_domain)
    if contact_domain is not None and contact_domain != canonical_domain:
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.BLOCKED,
            outcome=EmailExecutionOutcome.DOMAIN_INELIGIBLE,
            result={
                "domain_outcome": EmailExecutionOutcome.DOMAIN_INELIGIBLE.value,
                "contact_domain": contact_domain,
                "company_domain": canonical_domain,
            },
            output_reference={"company_id": str(company.id)},
            reason_code="company_domain_boundary_mismatch",
            reason="The Contact and canonical Company domains disagree.",
        )
    # The Campaign decides how far a provisional company domain reaches. This is
    # one of only two places that can honour that, because it is one of the only
    # two with a Campaign in scope; the contact-scoped candidate generator has no
    # Campaign and deliberately keeps the strict default.
    domain_gate = authorize_contact(
        session,
        contact=contact,
        stage=DownstreamStage.EMAIL_DISCOVERY,
        campaign=campaign,
    )
    if domain_gate.blocked:
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.BLOCKED,
            outcome=EmailExecutionOutcome.DOMAIN_INELIGIBLE,
            result={
                "domain_outcome": EmailExecutionOutcome.DOMAIN_INELIGIBLE.value,
                "domain_resolution_state": (
                    domain_gate.state.value if domain_gate.state is not None else None
                ),
            },
            output_reference={"company_id": str(company.id)},
            reason_code=EmailExecutionOutcome.DOMAIN_INELIGIBLE.value,
            reason=domain_gate.reason or "The Company domain is not eligible.",
        )
    suppression = evaluate_suppression(
        session,
        email=contact.email,
        domain=canonical_domain,
    )
    if suppression.blocked:
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.BLOCKED,
            outcome=EmailExecutionOutcome.SUPPRESSED,
            result={
                "domain_outcome": EmailExecutionOutcome.SUPPRESSED.value,
                "reason": suppression.blocked_reason,
            },
            output_reference={"contact_id": str(contact.id)},
            reason_code=EmailExecutionOutcome.SUPPRESSED.value,
            reason=suppression.blocked_reason or "The suppression ledger blocks this Contact.",
        )

    prior_policy_outcomes: list[dict[str, Any]] = []
    if (
        state is not None
        and state.get("blocked_outcome") is not None
        and state.get("policy_outcome") != EmailPolicyOutcome.READY.value
    ):
        stored_history = state.get("prior_policy_outcomes")
        if isinstance(stored_history, list):
            prior_policy_outcomes.extend(item for item in stored_history if isinstance(item, dict))
        prior_policy_outcomes.append(
            {
                "policy_outcome": state.get("policy_outcome"),
                "domain_outcome": state.get("blocked_outcome"),
                "employee_count_class": state.get("employee_count_class"),
                "employee_evidence": state.get("employee_evidence"),
                "reason": state.get("reason"),
            }
        )
        state = None

    if state is None:
        employee_evidence = _employee_evidence(session, company)
        if not force_refresh:
            reusable = reusable_accepted_email(
                session,
                contact=contact,
                company=company,
                verification_policy=verification_policy,
                now=now,
            )
            if reusable is not None:
                reuse_decision = evaluate_existing_accepted_email_reuse(
                    domain=company.domain,
                    employee_evidence=employee_evidence,
                    now=now,
                    consult_employee_size=consult_employee_size,
                )
                state = _policy_json(reuse_decision)
                state["force_refresh"] = False
                state["refresh_scope"] = None
                state["prior_policy_outcomes"] = prior_policy_outcomes
                if reuse_decision.outcome is EmailPolicyOutcome.EXISTING_ACCEPTED_EMAIL_REUSE:
                    return _reuse_step(
                        session,
                        job=job,
                        reusable=reusable,
                        state=state,
                    )
                outcome, reason_code = _policy_block(reuse_decision)
                state["blocked_outcome"] = outcome.value
                result = _persist_result(
                    job,
                    state,
                    {"domain_outcome": outcome.value},
                )
                return EmailExecutionStep(
                    kind=EmailExecutionStepKind.BLOCKED,
                    outcome=outcome,
                    result=result,
                    output_reference={
                        "company_id": str(company.id),
                        "policy_identifier": POLICY_IDENTIFIER,
                        "policy_version": POLICY_VERSION,
                    },
                    reason_code=reason_code,
                    reason=reuse_decision.reason,
                )

        decision = evaluate(
            first_name=contact.first_name,
            last_name=contact.last_name,
            domain=company.domain,
            employee_evidence=employee_evidence,
            now=now,
            consult_employee_size=consult_employee_size,
        )
        state = _policy_json(decision)
        state["force_refresh"] = force_refresh
        state["refresh_scope"] = refresh_scope
        state["prior_policy_outcomes"] = prior_policy_outcomes
        _write_state(job, state)
        if not decision.ready:
            outcome, reason_code = _policy_block(decision)
            state["blocked_outcome"] = outcome.value
            result = _persist_result(
                job,
                state,
                {"domain_outcome": outcome.value},
            )
            return EmailExecutionStep(
                kind=EmailExecutionStepKind.BLOCKED,
                outcome=outcome,
                result=result,
                output_reference={
                    "company_id": str(company.id),
                    "policy_identifier": POLICY_IDENTIFIER,
                    "policy_version": POLICY_VERSION,
                },
                reason_code=reason_code,
                reason=decision.reason,
            )
        rows = _materialize_candidates(session, contact=contact, decision=decision)
        state["candidates"] = [
            {**candidate_state, "candidate_id": str(row.id)}
            for candidate_state, row in zip(state["candidates"], rows, strict=True)
        ]
        _write_state(job, state)
        session.flush()
    else:
        if (
            state.get("policy_identifier") != POLICY_IDENTIFIER
            or state.get("policy_version") != POLICY_VERSION
        ):
            raise EmailAgentStateError(
                "stored Email execution belongs to a different discovery policy version"
            )
        if state.get("normalized_domain") != canonical_domain:
            state["blocked_outcome"] = EmailExecutionOutcome.DOMAIN_INELIGIBLE.value
            state["reason"] = (
                "The canonical Company domain changed during Email execution; "
                "an explicitly scoped refresh is required."
            )
            result = _persist_result(
                job,
                state,
                {"domain_outcome": EmailExecutionOutcome.DOMAIN_INELIGIBLE.value},
            )
            return EmailExecutionStep(
                kind=EmailExecutionStepKind.BLOCKED,
                outcome=EmailExecutionOutcome.DOMAIN_INELIGIBLE,
                result=result,
                output_reference={
                    "company_id": str(company.id),
                    "policy_identifier": POLICY_IDENTIFIER,
                    "policy_version": POLICY_VERSION,
                },
                reason_code="canonical_domain_changed",
                reason=str(state["reason"]),
            )
        evidence_block = _persisted_evidence_block(
            state=state,
            current=_employee_evidence(session, company),
            now=now,
        )
        if evidence_block is not None:
            outcome, reason = evidence_block
            state["blocked_outcome"] = outcome.value
            state["reason"] = reason
            result = _persist_result(
                job,
                state,
                {"domain_outcome": outcome.value},
            )
            return EmailExecutionStep(
                kind=EmailExecutionStepKind.BLOCKED,
                outcome=outcome,
                result=result,
                output_reference={
                    "company_id": str(company.id),
                    "policy_identifier": POLICY_IDENTIFIER,
                    "policy_version": POLICY_VERSION,
                },
                reason_code=outcome.value,
                reason=reason,
            )
        if not force_refresh:
            reusable = reusable_accepted_email(
                session,
                contact=contact,
                company=company,
                verification_policy=verification_policy,
                now=now,
            )
            if reusable is not None:
                return _reuse_step(session, job=job, reusable=reusable, state=state)

    candidates = state.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise EmailAgentStateError("stored Email policy has no candidate plan")
    index = int(state.get("current_candidate_index", 0))
    if index >= len(candidates):
        state["terminal_outcome"] = EmailExecutionOutcome.NO_VERIFIED_ADDRESS.value
        result = _persist_result(
            job,
            state,
            {"domain_outcome": EmailExecutionOutcome.NO_VERIFIED_ADDRESS.value},
        )
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.TERMINAL,
            outcome=EmailExecutionOutcome.NO_VERIFIED_ADDRESS,
            result=result,
            output_reference={
                "policy_identifier": POLICY_IDENTIFIER,
                "policy_version": POLICY_VERSION,
                "attempted_candidates": len(candidates),
            },
            reason_code=EmailExecutionOutcome.NO_VERIFIED_ADDRESS.value,
            reason="None of the allowed candidate formats produced a verified address.",
        )

    candidate_state = candidates[index]
    candidate_id = _uuid(candidate_state.get("candidate_id"))
    if candidate_id is None:
        raise EmailAgentStateError("stored Email candidate has no durable candidate id")
    candidate = session.get(EmailCandidate, candidate_id)
    if candidate is None or candidate.contact_id != contact.id:
        raise EmailAgentStateError("stored Email candidate no longer belongs to the Contact")
    attempt = _attempt(
        session,
        job=job,
        contact=contact,
        company=company,
        membership=membership,
        state=state,
        candidate=candidate,
        index=index,
        force_refresh=force_refresh,
        refresh_scope=refresh_scope,
    )
    candidate_suppression = evaluate_suppression(
        session,
        email=attempt.normalized_email,
        domain=attempt.normalized_domain,
    )
    if candidate_suppression.blocked:
        attempt.status = EmailCandidateAttemptStatus.REFUSED.value
        attempt.verification_decision = EmailExecutionOutcome.SUPPRESSED.value
        attempt.refusal_reason = candidate_suppression.blocked_reason
        attempt.resolved_at = now
        state["blocked_outcome"] = EmailExecutionOutcome.SUPPRESSED.value
        state["reason"] = candidate_suppression.blocked_reason
        result = _persist_result(
            job,
            state,
            {
                "domain_outcome": EmailExecutionOutcome.SUPPRESSED.value,
                "candidate_attempt_id": str(attempt.id),
            },
        )
        session.flush()
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.BLOCKED,
            outcome=EmailExecutionOutcome.SUPPRESSED,
            result=result,
            output_reference={"candidate_attempt_id": str(attempt.id)},
            reason_code=EmailExecutionOutcome.SUPPRESSED.value,
            reason=(candidate_suppression.blocked_reason or "The exact candidate is suppressed."),
        )

    if state.get("blocked_outcome") == EmailExecutionOutcome.SUPPRESSED.value:
        state.pop("blocked_outcome", None)
        state["reason"] = None
        _write_state(job, state)

    if attempt.verification_job_id is None:
        child: AgentJob | None = _ensure_verification_child(
            session,
            parent_job=job,
            attempt=attempt,
            verification_policy=verification_policy,
            actor=actor,
        )
        assert child is not None
        if child.parent_job_id != job.id or child.agent_id is not AgentIdentifier.VERIFICATION:
            raise EmailAgentStateError(
                "Verification enqueue returned a child outside the parent/Agent contract"
            )
        attempt.verification_job_id = child.id
        attempt.status = EmailCandidateAttemptStatus.VERIFICATION_QUEUED.value
        attempt.verification_queued_at = now
        session.flush()
    else:
        child = session.get(AgentJob, attempt.verification_job_id)
        if child is None:
            # A deleted child does not authorize an arbitrary replacement. The
            # deterministic idempotency key can only recreate the same intent.
            child = _ensure_verification_child(
                session,
                parent_job=job,
                attempt=attempt,
                verification_policy=verification_policy,
                actor=actor,
            )
            attempt.verification_job_id = child.id
            session.flush()

    assert child is not None
    if child.status is AgentJobStatus.PAUSED and child.error_class in {
        "requesting_email_agent_disabled",
        "requesting_email_agent_paused",
    }:
        # Reaching this point proves the common worker re-authorized the Email
        # parent. Resume only child pauses owned by that requesting control;
        # Verification, suppression, eligibility, and operator pauses remain
        # untouched.
        jobs.resume_paused(
            session,
            child,
            reason_codes=frozenset(
                {
                    "requesting_email_agent_disabled",
                    "requesting_email_agent_paused",
                }
            ),
        )
    verification_outcome = _committed_verification_outcome(child)
    if verification_outcome is None:
        attempt.status = EmailCandidateAttemptStatus.WAITING.value
        session.flush()
        return _waiting_step(job=job, state=state, attempt=attempt)

    attempt.verification_decision = (
        verification_outcome.decision.value if verification_outcome.decision is not None else None
    )
    attempt.verification_result = verification_outcome.reference or {}
    if verification_outcome.decision is VerificationDecision.RETRY_LATER:
        attempt.status = EmailCandidateAttemptStatus.RETRYABLE.value
        session.flush()
        return _waiting_step(
            job=job,
            state=state,
            attempt=attempt,
            retryable=True,
            reason=verification_outcome.reason,
        )
    if verification_outcome.decision is VerificationDecision.TRY_NEXT_CANDIDATE:
        attempt.status = EmailCandidateAttemptStatus.REJECTED.value
        attempt.verification_id = verification_outcome.verification_id
        attempt.resolved_at = now
        state["current_candidate_index"] = index + 1
        _write_state(job, state)
        session.flush()
        # The rejected child is terminal before the next candidate is created.
        return execute_step(
            session,
            job=job,
            contact=contact,
            membership=membership,
            verification_policy=verification_policy,
            now=now,
            actor=actor,
        )

    evidence = (
        session.get(ExactEmailVerification, verification_outcome.verification_id)
        if verification_outcome.verification_id is not None
        else None
    )
    simulated = verification_outcome.reason_code == "verification_simulated" or (
        evidence is not None and evidence.provider == SIMULATOR_PROVIDER_LABEL
    )
    if verification_outcome.decision is VerificationDecision.REFUSED and simulated:
        attempt.status = EmailCandidateAttemptStatus.SIMULATED.value
        attempt.verification_id = verification_outcome.verification_id
        attempt.refusal_reason = (
            verification_outcome.reason
            or "Simulated Verification cannot produce a production-ready email."
        )
        attempt.resolved_at = now
        state["terminal_outcome"] = EmailExecutionOutcome.SIMULATED_VERIFICATION_REFUSED.value
        state["reason"] = attempt.refusal_reason
        result = _persist_result(
            job,
            state,
            {
                "domain_outcome": (EmailExecutionOutcome.SIMULATED_VERIFICATION_REFUSED.value),
                "candidate_attempt_id": str(attempt.id),
            },
        )
        session.flush()
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.BLOCKED,
            outcome=EmailExecutionOutcome.SIMULATED_VERIFICATION_REFUSED,
            result=result,
            output_reference={"candidate_attempt_id": str(attempt.id)},
            reason_code=EmailExecutionOutcome.SIMULATED_VERIFICATION_REFUSED.value,
            reason=attempt.refusal_reason,
        )
    if verification_outcome.decision is VerificationDecision.REFUSED:
        attempt.status = EmailCandidateAttemptStatus.REFUSED.value
        attempt.verification_id = verification_outcome.verification_id
        attempt.refusal_reason = verification_outcome.reason
        attempt.resolved_at = now
        state["terminal_outcome"] = EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL.value
        state["reason"] = verification_outcome.reason
        result = _persist_result(
            job,
            state,
            {
                "domain_outcome": (EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL.value),
                "candidate_attempt_id": str(attempt.id),
            },
        )
        session.flush()
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.TERMINAL,
            outcome=EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL,
            result=result,
            output_reference={"candidate_attempt_id": str(attempt.id)},
            reason_code=EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL.value,
            reason=verification_outcome.reason or "Verification refused the candidate.",
        )
    if verification_outcome.decision is VerificationDecision.STOP_NO_RESULT:
        attempt.status = EmailCandidateAttemptStatus.TERMINAL_NO_RESULT.value
        attempt.verification_id = verification_outcome.verification_id
        attempt.refusal_reason = verification_outcome.reason
        attempt.resolved_at = now
        state["terminal_outcome"] = EmailExecutionOutcome.TERMINAL_VERIFICATION_FAILURE.value
        state["reason"] = verification_outcome.reason
        result = _persist_result(
            job,
            state,
            {
                "domain_outcome": (EmailExecutionOutcome.TERMINAL_VERIFICATION_FAILURE.value),
                "candidate_attempt_id": str(attempt.id),
            },
        )
        session.flush()
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.TERMINAL,
            outcome=EmailExecutionOutcome.TERMINAL_VERIFICATION_FAILURE,
            result=result,
            output_reference={"candidate_attempt_id": str(attempt.id)},
            reason_code=EmailExecutionOutcome.TERMINAL_VERIFICATION_FAILURE.value,
            reason=verification_outcome.reason or "Verification ended without address evidence.",
        )

    if verification_outcome.decision is None:
        # A shared queue/control refusal can stop a child before the Verification
        # adapter has a mailbox decision. Preserve that distinction rather than
        # inventing REFUSED evidence.
        attempt.status = EmailCandidateAttemptStatus.WAITING.value
        attempt.refusal_reason = (
            verification_outcome.reason or "The Verification dependency is paused or disabled."
        )
        state["blocked_outcome"] = EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL.value
        state["reason"] = attempt.refusal_reason
        dependency_code = (
            f"verification_dependency_{verification_outcome.reason_code}"
            if verification_outcome.reason_code
            else EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL.value
        )
        result = _persist_result(
            job,
            state,
            {
                "domain_outcome": EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL.value,
                "candidate_attempt_id": str(attempt.id),
                "verification_dependency_reason_code": verification_outcome.reason_code,
            },
        )
        session.flush()
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.BLOCKED,
            outcome=EmailExecutionOutcome.TERMINAL_VERIFICATION_REFUSAL,
            result=result,
            output_reference={
                "candidate_attempt_id": str(attempt.id),
                "verification_job_id": str(child.id),
            },
            reason_code=(dependency_code),
            reason=attempt.refusal_reason,
        )

    try:
        accepted_outcome, evidence = _accepted_write(
            session,
            job=job,
            contact=contact,
            company=company,
            attempt=attempt,
            verification_outcome=verification_outcome,
            verification_policy=verification_policy,
            now=now,
        )
    except EmailAcceptedWriteRefused as exc:
        # Suppression, identity ownership, or evidence mismatch discovered at the
        # final write is a refusal, never permission to try another format.
        attempt.status = EmailCandidateAttemptStatus.REFUSED.value
        attempt.refusal_reason = str(exc)
        attempt.resolved_at = now
        if exc.code is EmailExecutionOutcome.SUPPRESSED:
            state["blocked_outcome"] = exc.code.value
        else:
            state["terminal_outcome"] = exc.code.value
        state["reason"] = str(exc)
        result = _persist_result(
            job,
            state,
            {
                "domain_outcome": exc.code.value,
                "candidate_attempt_id": str(attempt.id),
            },
        )
        session.flush()
        return EmailExecutionStep(
            kind=EmailExecutionStepKind.BLOCKED,
            outcome=exc.code,
            result=result,
            output_reference={"candidate_attempt_id": str(attempt.id)},
            reason_code=exc.code.value,
            reason=str(exc),
        )

    if accepted_outcome is EmailExecutionOutcome.EXISTING_EMAIL_REUSED:
        return _reuse_step(
            session,
            job=job,
            reusable=ReusableAcceptedEmail(email=evidence.email, evidence=evidence),
            state=state,
        )

    state["accepted_candidate_index"] = index
    state["accepted_email"] = contact.email
    state["verification_id"] = str(evidence.id)
    state["terminal_outcome"] = accepted_outcome.value
    state.pop("blocked_outcome", None)
    _write_state(job, state)
    result = {
        "domain_outcome": accepted_outcome.value,
        "email": contact.email,
        "candidate_attempt_id": str(attempt.id),
        "candidate_id": str(attempt.candidate_id),
        "verification_job_id": (
            str(attempt.verification_job_id) if attempt.verification_job_id is not None else None
        ),
        "verification_id": str(evidence.id),
        "verification_provider": evidence.provider,
        "verification_policy_version": evidence.policy_version,
        "email_policy_identifier": attempt.policy_identifier,
        "email_policy_version": attempt.policy_version,
        "employee_count_class": attempt.employee_count_class,
        "employee_evidence_id": (
            str(attempt.employee_evidence_id) if attempt.employee_evidence_id is not None else None
        ),
        "attempted_candidates": index + 1,
        "address_derivation": "generated_candidate_verified_by_exact_address_evidence",
    }
    result = _persist_result(job, state, result)
    return EmailExecutionStep(
        kind=EmailExecutionStepKind.COMPLETE,
        outcome=accepted_outcome,
        result=result,
        output_reference={
            "email": contact.email,
            "candidate_attempt_id": str(attempt.id),
            "verification_id": str(evidence.id),
            "reused": False,
        },
    )
