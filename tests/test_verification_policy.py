"""VER-002 / VER-003: outcome mapping and freshness/TTL policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.models.enums import EmailPreciseStatus, EmailVerificationResult
from app.services.verification.policy import get_policy
from app.services.verification.provider import ProviderResponse


def _policy():
    return get_policy(get_settings())


def _resp(**kw: object) -> ProviderResponse:
    base = dict(email="a@b.com", result=None, resultcode=None)
    base.update(kw)
    return ProviderResponse(**base)  # type: ignore[arg-type]


def test_maps_all_address_results() -> None:
    p = _policy()
    assert p.map_response(_resp(result="ok", resultcode=1)).result == EmailVerificationResult.VALID
    assert (
        p.map_response(_resp(result="invalid", resultcode=6)).result
        == EmailVerificationResult.INVALID
    )
    assert (
        p.map_response(_resp(result="catch_all", resultcode=2)).result
        == EmailVerificationResult.CATCH_ALL
    )
    assert (
        p.map_response(_resp(result="unknown", resultcode=3)).result
        == EmailVerificationResult.UNKNOWN
    )
    assert (
        p.map_response(_resp(result="disposable", resultcode=5)).result
        == EmailVerificationResult.DISPOSABLE
    )


def test_only_ok_invalid_disposable_are_billable() -> None:
    p = _policy()
    assert p.map_response(_resp(result="ok", resultcode=1)).credited is True
    assert p.map_response(_resp(result="invalid", resultcode=6)).credited is True
    assert p.map_response(_resp(result="disposable", resultcode=5)).credited is True
    assert p.map_response(_resp(result="catch_all", resultcode=2)).credited is False
    assert p.map_response(_resp(result="unknown", resultcode=3)).credited is False


def test_valid_role_maps_to_warning_precise() -> None:
    p = _policy()
    m = p.map_response(_resp(result="ok", resultcode=1, role=True))
    assert m.result == EmailVerificationResult.VALID  # still valid evidence
    assert m.precise == EmailPreciseStatus.ROLE_BASED  # but not a green success


def test_insufficient_credits_is_not_address_evidence_and_not_retryable() -> None:
    m = _policy().map_response(_resp(error="insufficient_credits"))
    assert m.is_address_evidence is False
    assert m.retryable is False
    assert m.precise == EmailPreciseStatus.INSUFFICIENT_CREDITS


def test_transient_errors_are_retryable_and_not_evidence() -> None:
    p = _policy()
    for err in ("ip_address_blocked", "internal_error"):
        m = p.map_response(_resp(error=err))
        assert m.retryable is True
        assert m.is_address_evidence is False
        assert m.precise == EmailPreciseStatus.PROVIDER_ERROR


def test_config_error_is_not_retryable() -> None:
    m = _policy().map_response(_resp(error="invalid_api_key"))
    assert m.retryable is False
    assert m.is_address_evidence is False


def test_result_error_code_is_transient() -> None:
    m = _policy().map_response(_resp(result="error", resultcode=4))
    assert m.retryable is True
    assert m.is_address_evidence is False


def test_freshness_ttls() -> None:
    p = _policy()
    now = datetime(2026, 7, 24, tzinfo=UTC)
    fresh = now - timedelta(days=1)
    stale = now - timedelta(days=400)
    assert p.is_fresh(EmailVerificationResult.VALID, fresh, now) is True
    assert p.is_fresh(EmailVerificationResult.VALID, stale, now) is False
    # Unknown has a short TTL by default.
    assert p.is_fresh(EmailVerificationResult.UNKNOWN, now - timedelta(days=10), now) is False
