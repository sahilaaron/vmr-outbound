"""Ordered multi-provider traversal inside one Verification Agent attempt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.email_verification_studio import VerificationProviderAttempt
from app.models.enums import EmailPreciseStatus, VerificationFailureClass
from app.models.usage_ledger import UsageLedgerEntry
from app.models.verification_job import VerificationJob
from app.services.verification import attempts as job_attempts
from app.services.verification import service
from app.services.verification.policy import VerificationPolicy
from app.services.verification.provider import build_provider_by_id
from app.services.verification.provider_registry import descriptor
from app.services.verification.studio import (
    StudioConfigurationError,
    active_secret,
    active_waterfall,
)


class WaterfallUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class WaterfallOutcome:
    outcome: service.VerificationOutcome
    policy_version_id: str | None
    providers_attempted: tuple[str, ...]


def _credential(session: Session, provider_id: str, settings: Settings) -> str | None:
    configured = active_secret(session, provider_id, settings)
    if configured is not None:
        return configured[0]
    # Backward-compatible source during credential migration. It is read only;
    # Studio never displays or persists the environment value.
    if provider_id == "millionverifier":
        return settings.millionverifier_api_key
    return None


def verify(
    session: Session,
    job: VerificationJob,
    *,
    settings: Settings,
    policy: VerificationPolicy,
) -> WaterfallOutcome:
    """Traverse the active policy while preserving one Agent attempt lifecycle."""

    configured = active_waterfall(session)
    configuration = (
        dict(configured.configuration)
        if configured
        else {"providers": [{"id": "millionverifier", "enabled": True}]}
    )
    provider_ids = [
        str(item["id"])
        for item in configuration.get("providers", [])
        if isinstance(item, dict)
        and item.get("enabled") is True
        and isinstance(item.get("id"), str)
    ]
    started_at = datetime.now(UTC)
    rows: list[tuple[str, service.VerificationOutcome, datetime, datetime]] = []
    for provider_id in provider_ids:
        spec = descriptor(provider_id)
        try:
            secret = _credential(session, provider_id, settings)
        except StudioConfigurationError as exc:
            raise WaterfallUnavailable(str(exc)) from None
        if not secret:
            continue
        provider = build_provider_by_id(
            provider_id,
            api_key=secret,
            timeout_seconds=spec.timeout_seconds,
            live=True,
        )
        step_started = datetime.now(UTC)
        outcome = service.verify_exact_address(
            session,
            job,
            provider=provider,
            settings=settings,
            policy=policy,
            reuse_fresh=not rows,
            record_attempt=False,
        )
        step_finished = datetime.now(UTC)
        rows.append((provider_id, outcome, step_started, step_finished))
        if outcome.precise not in {
            EmailPreciseStatus.CATCH_ALL,
            EmailPreciseStatus.UNKNOWN,
            EmailPreciseStatus.PROVIDER_ERROR,
            EmailPreciseStatus.INSUFFICIENT_CREDITS,
        }:
            break
    if not rows:
        raise WaterfallUnavailable("No enabled provider has a live credential.")

    final = rows[-1][1]
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
                error_summary=outcome.reason,
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
        ),
        policy_version_id=str(configured.id) if configured else None,
        providers_attempted=tuple(item[0] for item in rows),
    )
