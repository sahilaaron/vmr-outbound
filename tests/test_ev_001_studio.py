"""EV-001 Email Discovery and Verification Studio contracts."""

from __future__ import annotations

from app.core.config import Settings
from app.models.email_evidence import ExactEmailVerification
from app.models.email_verification_studio import (
    ImmutableStudioHistoryError,
    ProviderTestRun,
)
from app.models.enums import UsageCacheStatus
from app.models.usage_ledger import UsageLedgerEntry
from app.models.verification_job import AgentJob
from app.services.verification import studio
from app.services.verification.provider_registry import PROVIDERS
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="local",
        database_url="postgresql+psycopg://postgres:postgres@127.0.0.1:5433/vmr_test",
        provider_credential_encryption_key=Fernet.generate_key().decode(),
    )


def test_registry_and_policy_validation_are_bounded_and_size_independent() -> None:
    assert tuple(PROVIDERS) == ("millionverifier", "debounce")
    assert all(provider.enabled for provider in PROVIDERS.values())
    assert PROVIDERS["millionverifier"].single_address is True
    assert PROVIDERS["debounce"].domain_probe is True

    waterfall = studio.validate_waterfall(
        {"providers": [{"id": "debounce"}, {"id": "millionverifier"}]}
    )
    assert [item["id"] for item in waterfall["providers"]] == [
        "debounce",
        "millionverifier",
    ]

    patterns = studio.validate_pattern_policy(
        {
            "patterns": [
                {"id": "firstname.lastname"},
                {"id": "finitiallastname"},
            ],
            "max_candidates": 6,
            # Unknown input is discarded; employee size is not part of the
            # versioned decision contract.
            "employee_size_ordering": {"large": ["finitiallastname"]},
        }
    )
    assert "employee_size_ordering" not in patterns
    assert patterns["max_candidates"] == 6


def test_credentials_are_versioned_encrypted_and_never_returned_by_status(
    db_session: Session,
) -> None:
    settings = _settings()
    row = studio.rotate_credential(
        db_session,
        provider_id="debounce",
        secret="provider-secret-value",
        label="Primary DeBounce key",
        actor="test:operator",
        settings=settings,
        reason="initial setup",
    )

    assert "provider-secret-value" not in row.encrypted_secret
    status = studio.credential_status(db_session, "debounce")
    assert status.configured is True
    assert status.label == "Primary DeBounce key"
    assert not hasattr(status, "secret")
    secret, version_id = studio.active_secret(db_session, "debounce", settings) or (None, None)
    assert secret == "provider-secret-value"
    assert version_id == row.id

    studio.deactivate_credential(
        db_session,
        provider_id="debounce",
        actor="test:operator",
        reason="rotation hold",
    )
    assert studio.active_secret(db_session, "debounce", settings) is None


def test_simulated_studio_test_is_one_call_with_no_execution_or_evidence_side_effect(
    db_session: Session,
) -> None:
    before = {
        model: db_session.scalar(select(func.count()).select_from(model))
        for model in (AgentJob, ExactEmailVerification)
    }

    run = studio.provider_test(
        db_session,
        provider_id="debounce",
        email="ada@kiln.example",
        live=False,
        actor="test:operator",
        settings=_settings(),
    )

    after = {model: db_session.scalar(select(func.count()).select_from(model)) for model in before}
    assert before == after
    assert run.live is False
    assert db_session.scalar(select(func.count()).select_from(ProviderTestRun)) == 1
    ledger = db_session.get(UsageLedgerEntry, run.usage_ledger_entry_id)
    assert ledger is not None
    assert ledger.origin == "agent_studio"
    assert ledger.account_reference is None
    assert ledger.cache_status is UsageCacheStatus.MISS
    assert ledger.units == 0


def test_policy_versions_are_inactive_until_explicit_activation(db_session: Session) -> None:
    waterfall = studio.create_waterfall_version(
        db_session,
        configuration={"providers": [{"id": "millionverifier"}]},
        name="MillionVerifier first",
        actor="test:operator",
        change_note="bounded first version",
    )
    patterns = studio.create_pattern_version(
        db_session,
        configuration={
            "patterns": [
                {"id": "firstname.lastname"},
                {"id": "firstname"},
            ],
            "max_candidates": 4,
        },
        name="Generic patterns",
        actor="test:operator",
        change_note="bounded first version",
    )
    assert studio.active_waterfall(db_session) is None
    assert studio.active_pattern_policy(db_session) is None

    studio.activate_waterfall(
        db_session,
        policy_version_id=waterfall.id,
        actor="test:operator",
        reason="approved for development",
    )
    studio.activate_pattern_policy(
        db_session,
        policy_version_id=patterns.id,
        actor="test:operator",
        reason="approved for development",
    )
    assert studio.active_waterfall(db_session).id == waterfall.id  # type: ignore[union-attr]
    assert studio.active_pattern_policy(db_session).id == patterns.id  # type: ignore[union-attr]

    waterfall.name = "mutated history"
    try:
        db_session.flush()
    except ImmutableStudioHistoryError:
        db_session.rollback()
    else:  # pragma: no cover - ORM protection is deterministic
        raise AssertionError("immutable policy history accepted an update")
