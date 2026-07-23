"""Guarded local-development tools: safety guards, fixtures, reset."""

from __future__ import annotations

import pytest
from app.core.config import get_settings
from app.models.audit_event import AuditEvent
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.services import devtools
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def _clear_settings_cache() -> None:
    get_settings.cache_clear()


def test_guard_refuses_non_local_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    _clear_settings_cache()
    try:
        with pytest.raises(devtools.LocalOnlyViolation, match="APP_ENV"):
            devtools.ensure_local_database()
    finally:
        _clear_settings_cache()


def test_guard_refuses_non_loopback_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:pw@prod-rds.example.amazonaws.com:5432/vmr",
    )
    _clear_settings_cache()
    try:
        with pytest.raises(devtools.LocalOnlyViolation, match="non-loopback"):
            devtools.ensure_local_database()
    finally:
        _clear_settings_cache()


def test_guard_allows_local_loopback() -> None:
    devtools.ensure_local_database()  # default test config: local + 127.0.0.1


@pytest.mark.usefixtures("enable_csv_import")
def test_csv_fixture_loads_synthetic_demo_data(db_session: Session) -> None:
    result = devtools.load_csv_fixture(db_session)
    summary = result.summary
    assert summary.total_rows == 10
    assert summary.accepted_rows > 0
    assert summary.rejected_rows >= 1  # the deliberately broken row
    assert summary.duplicate_rows >= 1
    assert summary.suppressed_rows >= 1  # the synthetic opt-out
    # Synthetic-only data: every imported domain is under .example.com.
    domains = db_session.scalars(select(Contact.company_domain)).all()
    assert domains and all(d.endswith(".example.com") for d in domains)


@pytest.mark.usefixtures("enable_csv_import")
def test_xlsx_fixture_loads_multi_sheet_workbook(db_session: Session) -> None:
    result = devtools.load_xlsx_fixture(db_session)
    summary = result.summary
    assert summary.total_rows == 5  # two selected sheets, Notes sheet excluded
    assert summary.accepted_rows == 5


@pytest.mark.usefixtures("enable_csv_import")
def test_fixture_reload_is_idempotent(db_session: Session) -> None:
    first = devtools.load_csv_fixture(db_session)
    second = devtools.load_csv_fixture(db_session)
    assert second.summary.reused_existing_batch
    assert second.summary.batch_id == first.summary.batch_id


@pytest.mark.usefixtures("enable_csv_import")
def test_clear_local_data_wipes_and_audits(db_session: Session) -> None:
    devtools.load_csv_fixture(db_session)
    assert (db_session.scalar(select(func.count(Contact.id))) or 0) > 0

    tables = devtools.clear_local_data(db_session)
    assert "contacts" in tables and "alembic_version" not in tables
    assert (db_session.scalar(select(func.count(Contact.id))) or 0) == 0
    assert (db_session.scalar(select(func.count(Campaign.id))) or 0) == 0
    # The wipe itself leaves an audit trace.
    reset_events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "devtools.local_reset")
    ).all()
    assert len(reset_events) == 1


@pytest.mark.usefixtures("enable_csv_import")
def test_reset_to_demo_state_yields_known_state(db_session: Session) -> None:
    results = devtools.reset_to_demo_state(db_session)
    assert {r.campaign_name for r in results} == {"Demo — CSV Import", "Demo — XLSX Import"}
    assert (db_session.scalar(select(func.count(Campaign.id))) or 0) == 2
