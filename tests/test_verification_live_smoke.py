"""VER-007: the deliberate live MillionVerifier smoke command.

Every test here is network-free: the live HTTP client is exercised through an
injected fake transport, so the *live selection path*, mapping, storage, ledger,
and truthful display are all proven without a real call or a real key. No test
adds a real key to the environment.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from app.core.config import Settings, get_settings
from app.models.email_evidence import ExactEmailVerification
from app.services.verification.live_smoke import LiveSmokeError, run_live_smoke
from app.services.verification.provider import LIVE_PROVIDER_LABEL
from sqlalchemy import select
from sqlalchemy.orm import Session

REAL_KEY = "not-a-test-key-live-abc123"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(
    monkeypatch: pytest.MonkeyPatch, *, key: str | None = REAL_KEY, feature: bool = True
) -> Settings:
    monkeypatch.setenv("FEATURES__MILLIONVERIFIER", "true" if feature else "false")
    if key is None:
        monkeypatch.delenv("MILLIONVERIFIER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("MILLIONVERIFIER_API_KEY", key)
    get_settings.cache_clear()
    return get_settings()


class _FakeTransport:
    """Returns a canned Single API body; records the URL it was asked to fetch."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.last_url: str | None = None

    def get(self, url: str, timeout: float) -> str:
        self.last_url = url
        return json.dumps(self.payload)


class _BoomTransport:
    def get(self, url: str, timeout: float) -> str:
        raise OSError(f"connection refused {REAL_KEY}")  # includes the key on purpose


def _ok_payload(email: str) -> dict:
    return {
        "email": email,
        "result": "ok",
        "resultcode": 1,
        "credits": 950,
        "role": False,
        "free": False,
        "livemode": True,
        "subresult": "none",
        "quality": "good",
    }


# --- Refusals ----------------------------------------------------------------


def test_refuses_when_feature_disabled(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, feature=False)
    with pytest.raises(LiveSmokeError, match="feature is disabled"):
        run_live_smoke(
            db_session,
            email="a@b.com",
            confirm=True,
            settings=settings,
            transport=_FakeTransport(_ok_payload("a@b.com")),
        )


def test_refuses_when_no_key(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refused before any network call, and told why.

    The refusal stayed exactly where it was, and that is worth pinning. Making
    MillionVerifier an operator control briefly moved it: a control whose
    credential is absent cannot be on, so the *control* refused first and the
    specific "no API key configured" sentence became unreachable.

    That was wrong here. With no key, verification deliberately routes to a
    deterministic simulator, and that is documented, tested behaviour local
    development depends on — so the capability requires the credential only in
    hosted environments, where a simulated answer would be misleading. This test
    runs local, the control is therefore available, and the key check behind it
    answers with the sentence it always used.
    """

    settings = _settings(monkeypatch, key=None)
    with pytest.raises(LiveSmokeError, match="no MillionVerifier API key configured"):
        run_live_smoke(db_session, email="a@b.com", confirm=True, settings=settings)


def test_refuses_documented_test_key(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, key="API_KEY_FOR_OK")
    with pytest.raises(LiveSmokeError, match="documented MillionVerifier test key"):
        run_live_smoke(
            db_session,
            email="a@b.com",
            confirm=True,
            settings=settings,
            transport=_FakeTransport(_ok_payload("a@b.com")),
        )


def test_refuses_without_confirm(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    with pytest.raises(LiveSmokeError, match="--confirm"):
        run_live_smoke(
            db_session,
            email="a@b.com",
            confirm=False,
            settings=settings,
            transport=_FakeTransport(_ok_payload("a@b.com")),
        )


def test_refuses_invalid_email(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    with pytest.raises(LiveSmokeError, match="valid email"):
        run_live_smoke(
            db_session,
            email="not-an-email",
            confirm=True,
            settings=settings,
            transport=_FakeTransport(_ok_payload("x")),
        )


# --- Happy path: authentic live interaction, truthfully recorded --------------


def test_live_path_records_truthful_live_evidence(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch)
    transport = _FakeTransport(_ok_payload("owner@vmr-controlled.com"))
    result = run_live_smoke(
        db_session,
        email="owner@vmr-controlled.com",
        confirm=True,
        settings=settings,
        transport=transport,
    )

    # The HTTP client — not the simulator — was selected and actually called.
    assert result.live_provider_selected is True
    assert result.provider_request_made is True
    assert result.transport_ok is True
    assert result.livemode is True
    # The request was built for the exact address (proves a real request shape).
    assert "email=owner%40vmr-controlled.com" in (transport.last_url or "")

    # Provider response mapped truthfully.
    assert result.provider_result == "ok"
    assert result.provider_result_code == 1
    assert result.canonical_result == "valid"
    assert result.precise_status == "valid"
    assert result.credited is True
    assert result.credits_remaining == 950

    # Evidence stored with LIVE provenance (never simulated).
    assert result.evidence_stored is True
    assert result.evidence_source == "live"
    row = db_session.scalars(
        select(ExactEmailVerification).where(
            ExactEmailVerification.email == "owner@vmr-controlled.com"
        )
    ).first()
    assert row is not None
    assert row.provider == LIVE_PROVIDER_LABEL

    # Ledger recorded a real attempt (cache MISS, confirmed charge for an ok result).
    assert result.ledger_recorded is True
    assert result.ledger_cache_status == "miss"
    assert result.ledger_charge_status == "confirmed"

    # The UI/status explanation states the result is live, not simulated.
    assert "Live MillionVerifier" in (result.status_explanation or "")
    assert not result.warnings


def test_refuses_when_fresh_cached_evidence_exists(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch)
    # First run stores fresh evidence.
    run_live_smoke(
        db_session,
        email="owner@vmr-controlled.com",
        confirm=True,
        settings=settings,
        transport=_FakeTransport(_ok_payload("owner@vmr-controlled.com")),
    )
    # Second run must refuse — a cache hit would not prove a live call.
    with pytest.raises(LiveSmokeError, match="fresh cached evidence"):
        run_live_smoke(
            db_session,
            email="owner@vmr-controlled.com",
            confirm=True,
            settings=settings,
            transport=_FakeTransport(_ok_payload("owner@vmr-controlled.com")),
        )


def test_transport_failure_is_not_reported_as_valid(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch)
    result = run_live_smoke(
        db_session,
        email="owner@vmr-controlled.com",
        confirm=True,
        settings=settings,
        transport=_BoomTransport(),
    )
    assert result.provider_request_made is True
    assert result.transport_ok is False
    assert result.canonical_result is None  # no verdict
    assert result.evidence_stored is False  # a transport failure is never evidence
    assert any("provider was not reached" in w for w in result.warnings)


# --- Secret safety -----------------------------------------------------------


def test_api_key_never_appears_in_result_or_error(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch)
    # Success path: the key must not appear anywhere in the sanitized result.
    result = run_live_smoke(
        db_session,
        email="owner@vmr-controlled.com",
        confirm=True,
        settings=settings,
        transport=_FakeTransport(_ok_payload("owner@vmr-controlled.com")),
    )
    assert REAL_KEY not in repr(result)

    # Failure path: the transport error text contained the key; it must be redacted.
    get_settings.cache_clear()
    settings2 = _settings(monkeypatch)
    failed = run_live_smoke(
        db_session,
        email="other@vmr-controlled.com",
        confirm=True,
        settings=settings2,
        transport=_BoomTransport(),
    )
    assert REAL_KEY not in repr(failed)
    assert REAL_KEY not in (failed.provider_error or "")
