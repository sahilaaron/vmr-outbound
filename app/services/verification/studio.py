"""Admin-only configuration and test operations for Verification Studio."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.email_evidence import ExactEmailVerification
from app.models.email_verification_studio import (
    EmailPatternPolicyActivation,
    EmailPatternPolicyVersion,
    LearnedDomainEmailFormat,
    ProviderCredentialActivation,
    ProviderCredentialVersion,
    ProviderTestRun,
    VerificationWaterfallActivation,
    VerificationWaterfallPolicyVersion,
)
from app.models.enums import EmailVerificationResult, UsageCacheStatus, UsageChargeStatus
from app.models.usage_ledger import UsageLedgerEntry
from app.services import usage_ledger
from app.services.imports.normalization import is_valid_email, normalize_email
from app.services.verification.policy import get_policy
from app.services.verification.provider import (
    ProviderTransientError,
    build_provider_by_id,
    evidence_provider_label,
)
from app.services.verification.provider_registry import PROVIDERS, descriptor

SCHEMA_WATERFALL = "verification-waterfall/v1"
SCHEMA_PATTERNS = "email-pattern-policy/v1"
ALLOWED_PATTERNS = (
    "firstname.lastname",
    "firstname",
    "finitiallastname",
    "firstnameinitial.lastname",
    "firstnamelastname",
    "firstnamelastinitial",
    "lastname.firstname",
    "lastnamefinitial",
)


class StudioConfigurationError(ValueError):
    pass


def _sanitize_text(value: str | None, *, limit: int = 500) -> str | None:
    # Lazy import avoids pulling the Workbench command package into the Agent
    # adapter import graph. The implementation remains the repository's single
    # sanitizer rather than a competing redaction rule.
    from app.services.workbench_agents.sanitize import sanitize_text

    return sanitize_text(value, limit=limit)


def _sanitize_mapping(value: dict[str, Any]) -> dict[str, Any] | None:
    from app.services.workbench_agents.sanitize import sanitize_mapping

    return sanitize_mapping(value)


@dataclass(frozen=True)
class CredentialStatus:
    provider_id: str
    configured: bool
    credential_version_id: uuid.UUID | None
    label: str | None
    fingerprint: str | None
    activated_at: datetime | None


@dataclass(frozen=True)
class UsageOriginSummary:
    origin: str
    calls: int
    cache_hits: int
    units: int
    estimated_cost: Decimal
    provider_cost: Decimal | None


def _fernet(settings: Settings) -> Fernet:
    key = settings.provider_credential_encryption_key
    if not key:
        raise StudioConfigurationError(
            "Provider credential encryption is unavailable until an explicit Fernet key "
            "is configured."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise StudioConfigurationError("Provider credential encryption key is invalid.") from exc


def credential_status(session: Session, provider_id: str) -> CredentialStatus:
    descriptor(provider_id)
    row = session.execute(
        select(ProviderCredentialActivation, ProviderCredentialVersion)
        .outerjoin(
            ProviderCredentialVersion,
            ProviderCredentialVersion.id == ProviderCredentialActivation.credential_version_id,
        )
        .where(ProviderCredentialActivation.provider_id == provider_id)
        .order_by(
            ProviderCredentialActivation.activated_at.desc(), ProviderCredentialActivation.id.desc()
        )
        .limit(1)
    ).first()
    if row is None or row[1] is None:
        return CredentialStatus(
            provider_id, False, None, None, None, row[0].activated_at if row else None
        )
    activation, credential = row
    return CredentialStatus(
        provider_id,
        True,
        credential.id,
        credential.label,
        credential.fingerprint,
        activation.activated_at,
    )


def rotate_credential(
    session: Session,
    *,
    provider_id: str,
    secret: str,
    label: str,
    actor: str,
    settings: Settings,
    reason: str | None = None,
) -> ProviderCredentialVersion:
    descriptor(provider_id)
    clean_secret = secret.strip()
    clean_label = label.strip()
    if not clean_secret or len(clean_secret) > 4096:
        raise StudioConfigurationError("Credential must contain 1 to 4096 characters.")
    if not clean_label or len(clean_label) > 160:
        raise StudioConfigurationError("Credential label must contain 1 to 160 characters.")
    previous = credential_status(session, provider_id)
    row = ProviderCredentialVersion(
        provider_id=provider_id,
        label=clean_label,
        encrypted_secret=_fernet(settings).encrypt(clean_secret.encode()).decode(),
        fingerprint=hashlib.sha256(clean_secret.encode()).hexdigest()[:12],
        created_by=actor,
    )
    session.add(row)
    session.flush()
    session.add(
        ProviderCredentialActivation(
            provider_id=provider_id,
            credential_version_id=row.id,
            previous_credential_version_id=previous.credential_version_id,
            activated_by=actor,
            reason=_sanitize_text(reason, limit=500),
        )
    )
    session.flush()
    return row


def deactivate_credential(
    session: Session, *, provider_id: str, actor: str, reason: str | None = None
) -> None:
    previous = credential_status(session, provider_id)
    session.add(
        ProviderCredentialActivation(
            provider_id=provider_id,
            credential_version_id=None,
            previous_credential_version_id=previous.credential_version_id,
            activated_by=actor,
            reason=_sanitize_text(reason, limit=500),
        )
    )
    session.flush()


def active_secret(
    session: Session, provider_id: str, settings: Settings
) -> tuple[str, uuid.UUID] | None:
    status = credential_status(session, provider_id)
    if not status.credential_version_id:
        return None
    row = session.get(ProviderCredentialVersion, status.credential_version_id)
    if row is None:
        return None
    try:
        return _fernet(settings).decrypt(row.encrypted_secret.encode()).decode(), row.id
    except InvalidToken as exc:
        raise StudioConfigurationError("The active credential cannot be decrypted.") from exc


def active_waterfall(session: Session) -> VerificationWaterfallPolicyVersion | None:
    return session.scalars(
        select(VerificationWaterfallPolicyVersion)
        .join(
            VerificationWaterfallActivation,
            VerificationWaterfallActivation.policy_version_id
            == VerificationWaterfallPolicyVersion.id,
        )
        .order_by(
            VerificationWaterfallActivation.activated_at.desc(),
            VerificationWaterfallActivation.id.desc(),
        )
        .limit(1)
    ).first()


def active_pattern_policy(session: Session) -> EmailPatternPolicyVersion | None:
    return session.scalars(
        select(EmailPatternPolicyVersion)
        .join(
            EmailPatternPolicyActivation,
            EmailPatternPolicyActivation.policy_version_id == EmailPatternPolicyVersion.id,
        )
        .order_by(
            EmailPatternPolicyActivation.activated_at.desc(),
            EmailPatternPolicyActivation.id.desc(),
        )
        .limit(1)
    ).first()


def pattern_plan(
    session: Session,
    domain: str,
    *,
    policy_version_id: uuid.UUID | None = None,
    use_active: bool = True,
) -> tuple[EmailPatternPolicyVersion | None, tuple[tuple[str, str], ...], int]:
    """Return learned-first patterns, source labels and the configured bound."""

    policy = (
        session.get(EmailPatternPolicyVersion, policy_version_id)
        if policy_version_id is not None
        else (active_pattern_policy(session) if use_active else None)
    )
    if policy_version_id is not None and policy is None:
        raise StudioConfigurationError("The queued Email pattern policy no longer exists.")
    configuration: dict[str, Any]
    if policy:
        configuration = dict(policy.configuration)
    else:
        configuration = {
            "learned_formats_first": True,
            "max_candidates": 3,
            "patterns": [
                {"id": "firstname.lastname", "enabled": True},
                {"id": "firstname", "enabled": True},
                {"id": "finitiallastname", "enabled": True},
            ],
        }
    result: list[tuple[str, str]] = []
    if configuration.get("learned_formats_first") is True:
        latest: dict[str, LearnedDomainEmailFormat] = {}
        rows = session.scalars(
            select(LearnedDomainEmailFormat)
            .where(LearnedDomainEmailFormat.domain == domain)
            .order_by(
                LearnedDomainEmailFormat.last_observed_at.desc(), LearnedDomainEmailFormat.id.desc()
            )
        ).all()
        for row in rows:
            latest.setdefault(row.pattern_id, row)
        result.extend(
            (row.pattern_id, "learned")
            for row in latest.values()
            if row.active and row.pattern_id in ALLOWED_PATTERNS
        )
    configured_patterns = configuration.get("patterns")
    if not isinstance(configured_patterns, list):
        configured_patterns = []
    for item in configured_patterns:
        if (
            isinstance(item, dict)
            and item.get("enabled") is True
            and item.get("id") in ALLOWED_PATTERNS
            and not any(existing[0] == item["id"] for existing in result)
        ):
            result.append((str(item["id"]), "configured"))
    configured_maximum = configuration.get("max_candidates")
    maximum = configured_maximum if isinstance(configured_maximum, int) else 3
    return policy, tuple(result), maximum


def learn_domain_format(
    session: Session,
    *,
    domain: str,
    pattern_id: str,
    evidence: ExactEmailVerification,
    provenance: dict[str, Any],
) -> LearnedDomainEmailFormat | None:
    """Append a learned snapshot only from safe accepted mailbox evidence."""

    if (
        pattern_id not in ALLOWED_PATTERNS
        or evidence.result is not EmailVerificationResult.VALID
        or bool(evidence.is_role)
        or "simulator" in evidence.provider
        or evidence.email.rsplit("@", 1)[-1] != domain
    ):
        return None
    previous = session.scalars(
        select(LearnedDomainEmailFormat)
        .where(
            LearnedDomainEmailFormat.domain == domain,
            LearnedDomainEmailFormat.pattern_id == pattern_id,
        )
        .order_by(
            LearnedDomainEmailFormat.last_observed_at.desc(),
            LearnedDomainEmailFormat.id.desc(),
        )
        .limit(1)
    ).first()
    now = evidence.checked_at
    support = (previous.support_count if previous else 0) + 1
    other_patterns = session.scalars(
        select(LearnedDomainEmailFormat.pattern_id)
        .where(
            LearnedDomainEmailFormat.domain == domain,
            LearnedDomainEmailFormat.active.is_(True),
            LearnedDomainEmailFormat.pattern_id != pattern_id,
        )
        .distinct()
    ).all()
    row = LearnedDomainEmailFormat(
        domain=domain,
        pattern_id=pattern_id,
        human_format=pattern_id.replace("firstname", "first").replace("lastname", "last"),
        support_count=support,
        first_observed_at=previous.first_observed_at if previous else now,
        last_observed_at=now,
        last_verified_at=now,
        confidence=min(0.95, 0.55 + (support - 1) * 0.10),
        active=True,
        conflicts=[{"pattern_id": item} for item in sorted(other_patterns)],
        provenance=_sanitize_mapping(provenance) or {},
        source_verification_id=evidence.id,
    )
    session.add(row)
    session.flush()
    return row


def validate_waterfall(configuration: dict[str, Any]) -> dict[str, Any]:
    providers = configuration.get("providers")
    if not isinstance(providers, list) or not providers:
        raise StudioConfigurationError("The waterfall must contain at least one provider.")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in providers:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise StudioConfigurationError("Every waterfall entry needs a provider id.")
        provider_id = item["id"]
        descriptor(provider_id)
        if provider_id in seen:
            raise StudioConfigurationError("A provider may appear only once in the waterfall.")
        seen.add(provider_id)
        normalized.append({"id": provider_id, "enabled": bool(item.get("enabled", True))})
    if not any(item["enabled"] for item in normalized):
        raise StudioConfigurationError("At least one waterfall provider must be enabled.")
    return {
        "schema_version": SCHEMA_WATERFALL,
        "providers": normalized,
        "stop_on": ["valid", "invalid", "disposable", "role_based"],
        "continue_on": ["catch_all", "unknown", "transient_provider"],
    }


def validate_pattern_policy(configuration: dict[str, Any]) -> dict[str, Any]:
    patterns = configuration.get("patterns")
    if not isinstance(patterns, list) or not patterns:
        raise StudioConfigurationError("The Email pattern policy must contain patterns.")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in patterns:
        if not isinstance(item, dict) or item.get("id") not in ALLOWED_PATTERNS:
            raise StudioConfigurationError("The Email pattern policy contains an unknown pattern.")
        pattern_id = str(item["id"])
        if pattern_id in seen:
            raise StudioConfigurationError("An Email pattern may appear only once.")
        seen.add(pattern_id)
        normalized.append(
            {
                "id": pattern_id,
                "enabled": bool(item.get("enabled", True)),
                "example": str(item.get("example", ""))[:160],
            }
        )
    max_candidates = configuration.get("max_candidates", 8)
    if not isinstance(max_candidates, int) or not 1 <= max_candidates <= 24:
        raise StudioConfigurationError("Maximum candidates must be between 1 and 24.")
    return {
        "schema_version": SCHEMA_PATTERNS,
        "learned_formats_first": bool(configuration.get("learned_formats_first", True)),
        "max_candidates": max_candidates,
        "stop_after_accepted": True,
        "patterns": normalized,
    }


def create_waterfall_version(
    session: Session,
    *,
    configuration: dict[str, Any],
    name: str,
    actor: str,
    based_on_version_id: uuid.UUID | None = None,
    change_note: str | None = None,
) -> VerificationWaterfallPolicyVersion:
    latest = (
        session.scalar(select(func.max(VerificationWaterfallPolicyVersion.version_number))) or 0
    )
    row = VerificationWaterfallPolicyVersion(
        version_number=int(latest) + 1,
        schema_version=SCHEMA_WATERFALL,
        name=name.strip()[:160] or f"Waterfall v{int(latest) + 1}",
        configuration=validate_waterfall(configuration),
        based_on_version_id=based_on_version_id,
        change_note=_sanitize_text(change_note, limit=1000),
        created_by=actor,
    )
    session.add(row)
    session.flush()
    return row


def activate_waterfall(
    session: Session, *, policy_version_id: uuid.UUID, actor: str, reason: str | None = None
) -> VerificationWaterfallActivation:
    row = session.get(VerificationWaterfallPolicyVersion, policy_version_id)
    if row is None:
        raise StudioConfigurationError("That waterfall policy version does not exist.")
    previous = active_waterfall(session)
    activation = VerificationWaterfallActivation(
        policy_version_id=row.id,
        previous_policy_version_id=previous.id if previous else None,
        activated_by=actor,
        reason=_sanitize_text(reason, limit=1000),
    )
    session.add(activation)
    session.flush()
    return activation


def create_pattern_version(
    session: Session,
    *,
    configuration: dict[str, Any],
    name: str,
    actor: str,
    based_on_version_id: uuid.UUID | None = None,
    change_note: str | None = None,
) -> EmailPatternPolicyVersion:
    latest = session.scalar(select(func.max(EmailPatternPolicyVersion.version_number))) or 0
    row = EmailPatternPolicyVersion(
        version_number=int(latest) + 1,
        schema_version=SCHEMA_PATTERNS,
        name=name.strip()[:160] or f"Email patterns v{int(latest) + 1}",
        configuration=validate_pattern_policy(configuration),
        based_on_version_id=based_on_version_id,
        change_note=_sanitize_text(change_note, limit=1000),
        created_by=actor,
    )
    session.add(row)
    session.flush()
    return row


def activate_pattern_policy(
    session: Session, *, policy_version_id: uuid.UUID, actor: str, reason: str | None = None
) -> EmailPatternPolicyActivation:
    row = session.get(EmailPatternPolicyVersion, policy_version_id)
    if row is None:
        raise StudioConfigurationError("That Email pattern policy version does not exist.")
    previous = active_pattern_policy(session)
    activation = EmailPatternPolicyActivation(
        policy_version_id=row.id,
        previous_policy_version_id=previous.id if previous else None,
        activated_by=actor,
        reason=_sanitize_text(reason, limit=1000),
    )
    session.add(activation)
    session.flush()
    return activation


def usage_by_origin(session: Session) -> tuple[UsageOriginSummary, ...]:
    rows = session.execute(
        select(
            UsageLedgerEntry.origin,
            func.count().filter(UsageLedgerEntry.cache_status == UsageCacheStatus.MISS),
            func.count().filter(UsageLedgerEntry.cache_status == UsageCacheStatus.HIT),
            func.coalesce(func.sum(UsageLedgerEntry.units), 0),
            func.coalesce(func.sum(UsageLedgerEntry.estimated_cost), 0),
            func.sum(UsageLedgerEntry.provider_cost),
        )
        .group_by(UsageLedgerEntry.origin)
        .order_by(UsageLedgerEntry.origin)
    ).all()
    return tuple(
        UsageOriginSummary(
            origin=origin,
            calls=int(calls),
            cache_hits=int(cache_hits),
            units=int(units),
            estimated_cost=Decimal(estimated),
            provider_cost=Decimal(provider_cost) if provider_cost is not None else None,
        )
        for origin, calls, cache_hits, units, estimated, provider_cost in rows
    )


def provider_test(
    session: Session,
    *,
    provider_id: str,
    email: str,
    live: bool,
    actor: str,
    settings: Settings,
) -> ProviderTestRun:
    """Run exactly one provider call, recording only Studio usage and safe output."""

    if provider_id not in PROVIDERS:
        raise StudioConfigurationError("Choose a registered verification provider.")
    spec = descriptor(provider_id)
    normalized = normalize_email(email)
    if not normalized or not is_valid_email(normalized):
        raise StudioConfigurationError("Enter one valid exact email address.")
    secret_and_id = active_secret(session, provider_id, settings) if live else None
    if live and secret_and_id is None:
        raise StudioConfigurationError("A live test requires an active encrypted credential.")
    secret, credential_id = secret_and_id if secret_and_id is not None else (None, None)
    provider = build_provider_by_id(
        provider_id,
        api_key=secret,
        timeout_seconds=spec.timeout_seconds,
        live=live,
    )
    attempted_at = datetime.now(UTC)
    result: str | None = None
    precise: str | None = None
    error: str | None = None
    summary: dict[str, Any] | None = None
    credits_remaining: int | None = None
    charge = UsageChargeStatus.UNCERTAIN if live else UsageChargeStatus.NONE
    try:
        response = provider.verify(normalized)
        mapped = get_policy(settings).map_response(response)
        result = mapped.result.value if mapped.result else None
        precise = mapped.precise.value
        error = _sanitize_text(response.error, limit=500)
        credits_remaining = response.credits
        summary = _sanitize_mapping(
            {
                "provider": evidence_provider_label(provider),
                "result": result,
                "precise_status": precise,
                "role": response.role,
                "free": response.free,
                "did_you_mean": response.didyoumean,
                "credits_remaining": response.credits,
            }
        )
        if live and provider_id == "millionverifier" and mapped.credited:
            charge = UsageChargeStatus.CONFIRMED
        elif live and provider_id != "millionverifier":
            charge = UsageChargeStatus.UNCERTAIN
        else:
            charge = UsageChargeStatus.NONE
    except ProviderTransientError as exc:
        error = _sanitize_text(str(exc), limit=500)
    ledger = usage_ledger.record_entry(
        session,
        provider=provider_id,
        operation="verify_email",
        attempted_at=attempted_at,
        cache_status=UsageCacheStatus.MISS,
        charge_status=charge,
        units=1 if charge is UsageChargeStatus.CONFIRMED else 0,
        result=result,
        reason=error or "explicit one-address Agent Studio provider test",
        origin="agent_studio",
        credits_remaining=credits_remaining,
    )
    run = ProviderTestRun(
        provider_id=provider_id,
        credential_version_id=credential_id,
        normalized_email=normalized,
        live=live,
        result=result,
        precise_status=precise,
        error_summary=error,
        response_summary=summary,
        usage_ledger_entry_id=ledger.id,
        actor=actor,
    )
    session.add(run)
    session.flush()
    return run


def provider_cards(session: Session) -> tuple[tuple[Any, CredentialStatus], ...]:
    return tuple(
        (spec, credential_status(session, provider_id)) for provider_id, spec in PROVIDERS.items()
    )


def provider_usage_summaries(
    session: Session, settings: Settings
) -> dict[str, usage_ledger.LedgerSummary]:
    """Shared-ledger balance and spend projection for every registered provider."""

    return {
        provider_id: usage_ledger.provider_summary(
            session,
            provider=provider_id,
            currency=settings.millionverifier_currency,
            cost_per_unit=(
                Decimal(str(settings.millionverifier_cost_per_credit))
                if provider_id == "millionverifier"
                else Decimal("0")
            ),
        )
        for provider_id in PROVIDERS
    }
