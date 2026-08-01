"""EV-001 provider, policy, credential, usage and read-only report contracts."""

from __future__ import annotations

import uuid

from app.core.config import Settings
from app.models.email_evidence import ExactEmailVerification
from app.models.email_verification_studio import ProviderCredentialVersion, ProviderTestRun
from app.models.enums import AgentIdentifier
from app.models.usage_ledger import UsageLedgerEntry
from app.models.verification_job import AgentJob
from app.services.agent_studio.email_verification_report import EmailVerificationReportReader
from app.services.verification.provider import HttpDebounce
from app.services.verification.provider_registry import PROVIDERS
from app.services.verification.studio import (
    active_secret,
    credential_status,
    provider_test,
    rotate_credential,
    validate_pattern_policy,
    validate_waterfall,
)
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.orm import Session


class _Transport:
    def get(self, url: str, timeout: float) -> str:
        assert timeout > 0
        assert "secret-value" in url
        return (
            '{"debounce":{"email":"ada@example.com","code":"5",'
            '"role":false,"free_email":false,"balance":"42"}}'
        )


class _EchoingTransport:
    def get(self, url: str, timeout: float) -> str:
        return (
            '{"debounce":{"email":"ada@example.com","code":"7",'
            '"error":"credential secret-value rejected",'
            '"diagnostic":"secret-value"}}'
        )


def test_registry_and_debounce_adapter_are_bounded_and_normalized() -> None:
    assert tuple(PROVIDERS) == ("millionverifier", "debounce")
    provider = HttpDebounce("secret-value", transport=_Transport())
    response = provider.verify("ada@example.com")
    assert response.result == "ok"
    assert response.resultcode == 5
    assert response.credits == 42
    assert "secret-value" not in provider.redacted_url("ada@example.com")

    echoing = HttpDebounce("secret-value", transport=_EchoingTransport()).verify("ada@example.com")
    assert "secret-value" not in str(echoing.error)
    assert "secret-value" not in str(echoing.raw)


def test_credential_rotation_is_encrypted_write_only(db_session: Session) -> None:
    settings = Settings(provider_credential_encryption_key=Fernet.generate_key().decode())
    row = rotate_credential(
        db_session,
        provider_id="debounce",
        secret="secret-value",
        label="Development key",
        actor="test",
        settings=settings,
        reason="exercise rotation",
    )
    assert "secret-value" not in row.encrypted_secret
    assert len(row.fingerprint) == 12
    assert credential_status(db_session, "debounce").credential_version_id == row.id
    assert active_secret(db_session, "debounce", settings) == ("secret-value", row.id)
    assert db_session.scalar(select(func.count()).select_from(ProviderCredentialVersion)) == 1


def test_policies_validate_fixed_registries_and_bounds() -> None:
    waterfall = validate_waterfall({"providers": [{"id": "millionverifier"}, {"id": "debounce"}]})
    assert [item["id"] for item in waterfall["providers"]] == [
        "millionverifier",
        "debounce",
    ]
    patterns = validate_pattern_policy(
        {
            "patterns": [
                {"id": "firstname.lastname"},
                {"id": "finitiallastname"},
            ],
            "max_candidates": 8,
        }
    )
    assert patterns["max_candidates"] == 8
    assert patterns["stop_after_accepted"] is True


def test_simulated_provider_test_has_only_test_and_usage_side_effects(
    db_session: Session,
) -> None:
    before_jobs = db_session.scalar(select(func.count()).select_from(AgentJob))
    before_evidence = db_session.scalar(select(func.count()).select_from(ExactEmailVerification))
    run = provider_test(
        db_session,
        provider_id="debounce",
        email="ada@example.com",
        live=False,
        actor="test",
        settings=Settings(),
    )
    assert run.live is False
    assert run.precise_status == "valid"
    assert db_session.scalar(select(func.count()).select_from(AgentJob)) == before_jobs
    assert (
        db_session.scalar(select(func.count()).select_from(ExactEmailVerification))
        == before_evidence
    )
    ledger = db_session.get(UsageLedgerEntry, run.usage_ledger_entry_id)
    assert ledger is not None
    assert ledger.origin == "agent_studio"
    assert db_session.get(ProviderTestRun, run.id) is run


def test_report_refuses_unknown_job_without_writes(db_session: Session) -> None:
    reader = EmailVerificationReportReader(db_session)
    before = len(db_session.new)
    assert reader.read(uuid.uuid4(), AgentIdentifier.EMAIL) is None
    assert len(db_session.new) == before
