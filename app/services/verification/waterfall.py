"""Ordered multi-provider traversal inside one Verification Agent attempt.

VER-02 makes this a *fallback* traversal rather than a poll of vendors. One
provider is asked; a second is asked only when the first could not answer. The
"could not answer" test is not written here — it lives in
:mod:`app.services.verification.fallback` so there is exactly one definition of
it, and this module reads that answer rather than re-deriving it.

The two rules that keep the traversal honest:

* A provider that produced an authoritative verdict ends the traversal, whatever
  the verdict was. A confirmed INVALID stops as firmly as a confirmed VALID;
  paying a second vendor to see whether it disagrees is not a fallback.
* Every step may reuse durable evidence *that provider* already produced for the
  same address. Only the first step may reuse another provider's evidence,
  because letting a later step do so would hand it the answer that made it run.
  That asymmetry is what stops a worker restart from buying the same fallback
  verification twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.email_verification_studio import (
    VerificationProviderAttempt,
    VerificationWaterfallPolicyVersion,
)
from app.models.enums import VerificationFailureClass
from app.models.usage_ledger import UsageLedgerEntry
from app.models.verification_job import VerificationJob
from app.services.verification import attempts as job_attempts
from app.services.verification import fallback as fallback_policy
from app.services.verification import service
from app.services.verification.policy import VerificationPolicy
from app.services.verification.provider import build_provider_by_id
from app.services.verification.provider_registry import descriptor
from app.services.verification.studio import (
    StudioConfigurationError,
    active_secret,
    active_waterfall,
)

#: The traversal used when no Studio waterfall version has been activated. It is
#: the MillionVerifier-only path this product has always run; DeBounce is
#: appended only when it is both flagged on and credentialed, so an unconfigured
#: environment behaves exactly as it did before VER-02.
PRIMARY_PROVIDER_ID = "millionverifier"
FALLBACK_PROVIDER_ID = "debounce"


class WaterfallUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class WaterfallOutcome:
    outcome: service.VerificationOutcome
    policy_version_id: str | None
    providers_attempted: tuple[str, ...]
    # True when a provider beyond the first actually ran. Derived here rather
    # than inferred downstream so "was this a fallback?" has one answer.
    fallback_used: bool = False
    # Machine-readable ``ProviderCondition`` naming why the primary could not
    # settle the address, and the operator-readable sentence beside it. Both are
    # None when no fallback happened.
    fallback_condition: str | None = None
    fallback_reason: str | None = None


def default_configuration(settings: Settings) -> dict[str, list[dict[str, object]]]:
    """The provider order to run when no Studio policy version is active."""

    providers: list[dict[str, object]] = [{"id": PRIMARY_PROVIDER_ID, "enabled": True}]
    if settings.debounce_fallback_available():
        providers.append({"id": FALLBACK_PROVIDER_ID, "enabled": True})
    return {"providers": providers}


def _credential(session: Session, provider_id: str, settings: Settings) -> str | None:
    """The live secret for *provider_id*, or None when it is not configured.

    Studio-managed credentials win. The environment is the backward-compatible
    second source, and it is read only: Studio never displays or persists it.
    """

    configured = active_secret(session, provider_id, settings)
    if configured is not None:
        return configured[0]
    if provider_id == PRIMARY_PROVIDER_ID:
        return settings.millionverifier_api_key
    if provider_id == FALLBACK_PROVIDER_ID:
        # The flag is checked as well as the key: an environment that happens to
        # carry a DeBounce key has not thereby authorized spending its credits.
        return settings.debounce_api_key if settings.debounce_fallback_available() else None
    return None


def _timeout_seconds(provider_id: str, settings: Settings) -> int:
    """The per-call budget for *provider_id*: operator setting, else registry."""

    if provider_id == PRIMARY_PROVIDER_ID:
        return settings.millionverifier_timeout_seconds
    if provider_id == FALLBACK_PROVIDER_ID:
        return settings.debounce_timeout_seconds
    return descriptor(provider_id).timeout_seconds


def _base_url(provider_id: str, settings: Settings) -> str | None:
    """The operator-configured endpoint for *provider_id*, if it has one."""

    if provider_id == PRIMARY_PROVIDER_ID:
        return settings.millionverifier_base_url
    if provider_id == FALLBACK_PROVIDER_ID:
        return settings.debounce_base_url
    return None


def _resolve_configuration(
    session: Session, job: VerificationJob, settings: Settings
) -> tuple[VerificationWaterfallPolicyVersion | None, dict[str, object]]:
    raw_policy_id = (job.input_reference or {}).get("waterfall_policy_version_id")
    requested_policy_id: uuid.UUID | None = None
    if raw_policy_id is not None:
        if not isinstance(raw_policy_id, str):
            raise WaterfallUnavailable("Queued waterfall policy id is malformed.")
        try:
            requested_policy_id = uuid.UUID(raw_policy_id)
        except ValueError:
            raise WaterfallUnavailable("Queued waterfall policy id is malformed.") from None
    configured = (
        session.get(VerificationWaterfallPolicyVersion, requested_policy_id)
        if requested_policy_id is not None
        else active_waterfall(session)
    )
    if requested_policy_id is not None and configured is None:
        raise WaterfallUnavailable("The queued waterfall policy no longer exists.")
    configuration: dict[str, object] = (
        dict(configured.configuration) if configured else dict(default_configuration(settings))
    )
    return configured, configuration


def verify(
    session: Session,
    job: VerificationJob,
    *,
    settings: Settings,
    policy: VerificationPolicy,
) -> WaterfallOutcome:
    """Traverse the active policy while preserving one Agent attempt lifecycle."""

    configured, configuration = _resolve_configuration(session, job, settings)
    raw_providers = configuration.get("providers", [])
    provider_ids = [
        str(item["id"])
        for item in (raw_providers if isinstance(raw_providers, list) else [])
        if isinstance(item, dict)
        and item.get("enabled") is True
        and isinstance(item.get("id"), str)
    ]
    started_at = datetime.now(UTC)
    rows: list[tuple[str, service.VerificationOutcome, datetime, datetime]] = []
    assessments: list[fallback_policy.FallbackAssessment] = []
    for provider_id in provider_ids:
        descriptor(provider_id)  # rejects any id outside the fixed registry
        try:
            secret = _credential(session, provider_id, settings)
        except StudioConfigurationError as exc:
            raise WaterfallUnavailable(str(exc)) from None
        if not secret:
            continue
        if rows and not assessments[-1].fallback_eligible:
            # Defensive: the loop below already breaks on an authoritative
            # verdict. Restating it here means no future edit can add a path
            # that spends a fallback credit on a settled address.
            break
        provider = build_provider_by_id(
            provider_id,
            api_key=secret,
            timeout_seconds=_timeout_seconds(provider_id, settings),
            live=True,
            base_url=_base_url(provider_id, settings),
        )
        step_started = datetime.now(UTC)
        outcome = service.verify_exact_address(
            session,
            job,
            provider=provider,
            settings=settings,
            policy=policy,
            # Every step may reuse its *own* durable evidence, so a retry after a
            # completed fallback call re-reads the row instead of buying it again.
            reuse_fresh=True,
            record_attempt=False,
            record_retry_hint=False,
            # Only the primary may answer from another provider's evidence. A
            # fallback step that could would reuse exactly the unsettled result
            # that caused it to run, and would never actually run.
            allow_cross_provider_reuse=not rows,
        )
        step_finished = datetime.now(UTC)
        rows.append((provider_id, outcome, step_started, step_finished))
        assessment = fallback_policy.assess(outcome)
        assessments.append(assessment)
        if not assessment.fallback_eligible:
            break
    if not rows:
        raise WaterfallUnavailable("No enabled provider has a live credential.")

    final = rows[-1][1]
    fallback_used = len(rows) > 1
    triggering = assessments[-2] if fallback_used else None
    aggregate = job_attempts.record_attempt(
        session,
        job,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        provider=" -> ".join(item[1].provider_label for item in rows),
        provider_called=any(item[1].provider_called for item in rows),
        reused_evidence=final.reused,
        failure_class=final.failure_class,
        precise_status=final.precise.value,
        verification_result=final.result,
        error_summary=final.reason,
        verification_id=final.evidence.id if final.evidence else None,
        settings=settings,
    )
    verdicts = {item[1].precise for item in rows if item[1].is_address_evidence}
    conflict = len(verdicts) > 1
    for index, (provider_id, outcome, step_started, step_finished) in enumerate(rows):
        ledger_id = session.scalars(
            select(UsageLedgerEntry.id)
            .where(
                UsageLedgerEntry.job_id == job.id,
                UsageLedgerEntry.provider == provider_id,
                UsageLedgerEntry.attempted_at >= step_started,
            )
            .order_by(UsageLedgerEntry.attempted_at.desc())
            .limit(1)
        ).first()
        session.add(
            VerificationProviderAttempt(
                verification_attempt_id=aggregate.id,
                job_id=job.id,
                provider_order=index,
                provider_id=provider_id,
                adapter_version=descriptor(provider_id).adapter_version,
                simulated=outcome.simulated,
                provider_called=outcome.provider_called,
                precise_status=outcome.precise.value,
                result=outcome.result.value if outcome.result else None,
                retryable=outcome.failure_class is VerificationFailureClass.TRANSIENT_PROVIDER,
                conflict=conflict,
                # The step's own condition is prefixed onto its sanitized
                # summary so a later reader can see *why* the next provider ran
                # without re-deriving it from a precise status.
                error_summary=_step_summary(assessments[index], outcome),
                verification_id=outcome.evidence.id if outcome.evidence else None,
                usage_ledger_entry_id=ledger_id,
                started_at=step_started,
                finished_at=step_finished,
            )
        )
    session.flush()
    return WaterfallOutcome(
        outcome=service.VerificationOutcome(
            email=final.email,
            precise=final.precise,
            result=final.result,
            evidence=final.evidence,
            reused=final.reused,
            provider_called=final.provider_called,
            provider_label=final.provider_label,
            failure_class=final.failure_class,
            policy_version=final.policy_version,
            reason=final.reason,
            attempt=aggregate,
            condition=final.condition,
        ),
        policy_version_id=str(configured.id) if configured else None,
        providers_attempted=tuple(item[0] for item in rows),
        fallback_used=fallback_used,
        fallback_condition=triggering.condition.value if triggering else None,
        fallback_reason=triggering.reason if triggering else None,
    )


def _step_summary(
    assessment: fallback_policy.FallbackAssessment,
    outcome: service.VerificationOutcome,
) -> str | None:
    """The durable, credential-free explanation of one provider step."""

    if assessment.authoritative:
        return outcome.reason
    detail = outcome.reason or assessment.reason
    return f"{assessment.condition.value}: {detail}"
