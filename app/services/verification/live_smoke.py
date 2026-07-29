"""Deliberate, operator-run single live MillionVerifier smoke check (VER-007).

This is the one controlled entry point that performs **exactly one** real
MillionVerifier request for a single supplied address, proving credentials,
mapping, storage, and truthful display end to end. Everything else in the
verification path is proven offline against the deterministic simulator.

Safety contract (all enforced here, never bypassed):

* the verification feature must be deliberately enabled (``FEATURES__MILLIONVERIFIER``);
* a real, non-empty API key must be configured;
* documented MillionVerifier test keys are refused (they route to the simulator);
* live mode is explicit — the HTTP client is selected, never the simulator, and
  the run aborts rather than fall back to a simulated success;
* the operator must confirm before a credit is consumed;
* a provider request must actually occur — a fresh cache hit is refused so a cache
  reuse is never reported as a completed live smoke test;
* exactly one address is verified; there is no bulk path.

The API key is never printed, logged, stored, placed in an exception, or written
into the returned result. The result carries only sanitized, non-secret fields.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.email_evidence import ExactEmailVerification
from app.models.usage_ledger import UsageLedgerEntry
from app.services.imports.normalization import is_valid_email, normalize_email
from app.services.verification import queue as jobs
from app.services.verification import service
from app.services.verification.policy import VerificationPolicy, get_policy
from app.services.verification.provider import (
    LIVE_PROVIDER_LABEL,
    TEST_KEYS,
    HttpMillionVerifier,
    ProviderResponse,
    ProviderTransientError,
    Transport,
    VerificationProvider,
)
from app.services.verification.status import StatusView, derive_status_for_email


class LiveSmokeError(Exception):
    """A deliberate refusal to run the live smoke test.

    The message is always safe to show the operator: it never contains the API
    key or an unredacted provider URL.
    """


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class LiveSmokeResult:
    """Sanitized, non-secret record of one live smoke run (safe to print/commit)."""

    normalized_email: str
    live_provider_selected: bool
    transport_ok: bool
    provider_request_made: bool
    cache_hit_avoided: bool
    livemode: bool | None = None
    provider_result: str | None = None
    provider_result_code: int | None = None
    canonical_result: str | None = None
    precise_status: str | None = None
    credited: bool | None = None
    credits_remaining: int | None = None
    subresult: str | None = None
    is_role: bool | None = None
    is_free: bool | None = None
    did_you_mean: str | None = None
    checked_at: datetime | None = None
    policy_version: str | None = None
    evidence_stored: bool = False
    evidence_id: str | None = None
    evidence_source: str | None = None
    ledger_recorded: bool = False
    ledger_cache_status: str | None = None
    ledger_charge_status: str | None = None
    status_visual: str | None = None
    status_explanation: str | None = None
    provider_error: str | None = None
    warnings: list[str] = field(default_factory=list)


class _RecordingProvider:
    """Wraps the live provider to capture the single response/exception.

    Delegates identity (``name``/``simulated``) so downstream code stores correct
    provenance, and makes exactly one underlying call. It never touches the key.
    """

    def __init__(self, inner: VerificationProvider) -> None:
        self._inner = inner
        self.name = inner.name
        self.simulated = getattr(inner, "simulated", False)
        self.called = False
        self.response: ProviderResponse | None = None
        self.error: str | None = None

    def verify(self, email: str) -> ProviderResponse:
        self.called = True
        try:
            resp = self._inner.verify(email)
        except ProviderTransientError as exc:
            # ``str(exc)`` is already key-redacted by the HTTP client.
            self.error = str(exc)
            raise
        self.response = resp
        return resp


def run_live_smoke(
    session: Session,
    *,
    email: str,
    confirm: bool,
    settings: Settings | None = None,
    transport: Transport | None = None,
    allow_existing_fresh: bool = False,
) -> LiveSmokeResult:
    """Perform exactly one deliberate live MillionVerifier verification.

    Raises :class:`LiveSmokeError` (with a safe, secret-free message) when any
    precondition is not met. On success returns a sanitized :class:`LiveSmokeResult`.
    The caller owns the transaction (commit/rollback).

    ``transport`` is an injection seam for tests only, so the live selection path
    and mapping/storage are proven without a real network call; production leaves
    it ``None`` and uses the real HTTP transport.
    """

    settings = settings or get_settings()

    # 1) The verification feature must be deliberately enabled.
    if not settings.features.millionverifier:
        raise LiveSmokeError(
            "verification feature is disabled; set FEATURES__MILLIONVERIFIER=true "
            "(local .env) and restart before running the live smoke test"
        )

    # 2) A real, non-empty key must be configured.
    if not settings.has_millionverifier_key():
        raise LiveSmokeError(
            "no MillionVerifier API key configured; set MILLIONVERIFIER_API_KEY in "
            "your local .env (never commit it) and restart"
        )
    key = (settings.millionverifier_api_key or "").strip()

    # 3) Documented test keys must never reach the network.
    if key in TEST_KEYS:
        raise LiveSmokeError(
            "the configured key is a documented MillionVerifier test key; the live "
            "smoke test refuses to run with a test key (it routes to the simulator)"
        )

    # 4) Deliberate confirmation before a credit is consumed.
    if not confirm:
        raise LiveSmokeError(
            "the live smoke test consumes one MillionVerifier credit; re-run with "
            "--confirm to proceed deliberately"
        )

    # 5) Exactly one valid address (avoid spending a credit on obvious garbage).
    norm = normalize_email(email)
    if not norm or not is_valid_email(norm):
        raise LiveSmokeError("a single valid email address is required")

    policy = get_policy(settings)

    # 6) Cache guard: a fresh cached result would skip the provider call, so a run
    # against it could never prove a live request occurred.
    fresh = service.find_fresh_evidence(
        session,
        norm,
        policy,
        _now(),
        required_provider_label=LIVE_PROVIDER_LABEL,
    )
    if fresh is not None and not allow_existing_fresh:
        raise LiveSmokeError(
            "fresh cached evidence already exists for this exact address, so a live "
            "call would be skipped. Use an address with no fresh evidence to prove a "
            "real provider request (a cache hit does not complete the live smoke test)"
        )

    # 7) Select the live HTTP client explicitly; never silently fall back to the
    # simulator. The ``transport`` seam keeps the automated test network-free.
    if transport is not None:
        inner: VerificationProvider = HttpMillionVerifier(
            key,
            base_url=settings.millionverifier_base_url,
            timeout_seconds=settings.millionverifier_timeout_seconds,
            transport=transport,
        )
    else:
        inner = service.get_provider(settings, live=True)
    if getattr(inner, "simulated", True):
        raise LiveSmokeError(
            "live provider selection resolved to the simulator; aborting so a "
            "simulated result is never reported as a live verification"
        )
    provider = _RecordingProvider(inner)

    # 8) One provider request through the real production mapping/store/ledger path.
    job, _created = jobs.enqueue_verification(
        session,
        email=norm,
        policy_version=policy.version,
        max_attempts=settings.verification_max_attempts,
    )
    worker_id = f"live-smoke:{uuid.uuid4()}"
    claimed = jobs.claim_job(
        session,
        job_id=job.id,
        worker_id=worker_id,
        lease_seconds=max(30.0, settings.millionverifier_timeout_seconds + 10.0),
    )
    if claimed is None:
        raise LiveSmokeError(
            "the exact verification job could not be claimed; another worker may be "
            "processing it or it is not yet due"
        )
    processed = service.process_job(
        session, claimed, provider=provider, settings=settings, policy=policy
    )
    session.flush()

    if not provider.called:
        raise LiveSmokeError(
            "no provider request was made (unexpected cache reuse); the live smoke "
            "test did not prove a live call and is not complete"
        )

    return _build_result(
        session,
        norm=norm,
        provider=provider,
        job_id=job.id,
        policy=policy,
        outcome_status=processed.outcome_status,
    )


def _build_result(
    session: Session,
    *,
    norm: str,
    provider: _RecordingProvider,
    job_id: uuid.UUID,
    policy: VerificationPolicy,
    outcome_status: str | None,
) -> LiveSmokeResult:
    response = provider.response
    transport_ok = response is not None

    result = LiveSmokeResult(
        normalized_email=norm,
        live_provider_selected=provider.simulated is False,
        transport_ok=transport_ok,
        provider_request_made=provider.called,
        cache_hit_avoided=True,
        precise_status=outcome_status,
        provider_error=provider.error,
    )

    if response is not None:
        mapped = policy.map_response(response)
        result.livemode = response.livemode
        result.provider_result = response.result
        result.provider_result_code = response.resultcode
        result.canonical_result = mapped.result.value if mapped.result is not None else None
        result.credited = mapped.credited
        result.credits_remaining = response.credits
        result.subresult = response.subresult
        result.is_role = response.role
        result.is_free = response.free
        result.did_you_mean = response.didyoumean

    # Evidence stored for the exact address (only for address-evidence outcomes).
    evidence = session.scalars(
        select(ExactEmailVerification)
        .where(ExactEmailVerification.email == norm)
        .order_by(ExactEmailVerification.checked_at.desc())
        .limit(1)
    ).first()
    if evidence is not None:
        result.evidence_stored = True
        result.evidence_id = str(evidence.id)
        result.policy_version = evidence.policy_version
        result.checked_at = evidence.checked_at
        result.evidence_source = "simulated" if evidence.provider.endswith("-simulator") else "live"

    # Ledger entry recorded for this attempt (cache miss + charge semantics).
    ledger = session.scalars(
        select(UsageLedgerEntry)
        .where(UsageLedgerEntry.job_id == job_id)
        .order_by(UsageLedgerEntry.attempted_at.desc())
        .limit(1)
    ).first()
    if ledger is not None:
        result.ledger_recorded = True
        result.ledger_cache_status = ledger.cache_status.value
        result.ledger_charge_status = ledger.charge_status.value
        if result.credits_remaining is None:
            result.credits_remaining = ledger.credits_remaining

    status: StatusView = derive_status_for_email(session, norm)
    result.status_visual = status.visual.value
    result.status_explanation = status.explanation

    # Truthfulness guards: a simulated row must never be reported by the live path.
    if result.evidence_stored and result.evidence_source == "simulated":
        result.warnings.append("stored evidence is simulated — this is NOT a live provider result")
    if not transport_ok:
        result.warnings.append("transport failed before a verdict — the provider was not reached")
    return result
