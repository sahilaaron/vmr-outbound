"""FND-009 connection-safety rules: targets, TLS, masking, guards, pooling.

These tests exercise the safety layer with local/synthetic URLs only. No test
here opens a network connection to any non-loopback host, and no real endpoint,
username, or password appears anywhere in this file (all remote-looking values
are synthetic ``example``-domain placeholders).
"""

from __future__ import annotations

import pytest
from app.core.config import Settings, get_settings
from app.db import safety
from app.db.safety import (
    DatabaseConfigurationError,
    RemoteDatabaseRefused,
    assert_connection_encrypted,
    enforce_engine_url,
    ensure_local_only_operation,
    ensure_migration_allowed,
    mask_database_url,
    validate_database_settings,
)
from app.db.session import create_db_engine

_SYNTH_REMOTE = "postgresql+psycopg://appuser:s3cret@db-dev.example.amazonaws.com:5432/vmr_dev"
_SYNTH_REMOTE_SSL = _SYNTH_REMOTE + "?sslmode=require"
_LOCAL = "postgresql+psycopg://dev@127.0.0.1:5433/vmr_dev"


# --- masking ------------------------------------------------------------------


def test_mask_removes_password_and_remote_identity() -> None:
    masked = mask_database_url(_SYNTH_REMOTE_SSL)
    assert "s3cret" not in masked
    assert "appuser" not in masked
    assert "example.amazonaws.com" not in masked
    assert "db-dev" not in masked
    assert "[masked-host]" in masked
    assert "[masked-user]" in masked
    # Operator-relevant, non-secret parts stay visible.
    assert "/vmr_dev" in masked
    assert "sslmode=require" in masked
    assert ":5432" in masked


def test_mask_keeps_loopback_host_but_never_password() -> None:
    masked = mask_database_url("postgresql+psycopg://dev:pw@127.0.0.1:5433/vmr_dev")
    assert "127.0.0.1" in masked
    assert "dev@" in masked
    assert "pw" not in masked.replace("psycopg", "")  # password gone (driver name has no 'pw')


def test_mask_never_raises_on_garbage() -> None:
    assert mask_database_url("not a url at all ://") == "[unparseable-database-url]"


def test_describe_database_error_is_type_name_only() -> None:
    exc = RuntimeError(f"connect failed for {_SYNTH_REMOTE}")
    assert safety.describe_database_error(exc) == "RuntimeError"


# --- mode validation (fail closed) ---------------------------------------------


def test_local_mode_accepts_loopback() -> None:
    validate_database_settings(target="local", url=_LOCAL, url_explicitly_set=True)


def test_local_mode_refuses_remote_host() -> None:
    with pytest.raises(DatabaseConfigurationError, match="loopback"):
        validate_database_settings(target="local", url=_SYNTH_REMOTE_SSL, url_explicitly_set=True)


def test_rds_mode_requires_explicit_url() -> None:
    with pytest.raises(DatabaseConfigurationError, match="explicitly"):
        validate_database_settings(
            target="rds-dev", url=_SYNTH_REMOTE_SSL, url_explicitly_set=False
        )


def test_rds_mode_refuses_loopback() -> None:
    with pytest.raises(DatabaseConfigurationError, match="loopback"):
        validate_database_settings(target="rds-dev", url=_LOCAL, url_explicitly_set=True)


@pytest.mark.parametrize("suffix", ["", "?sslmode=prefer", "?sslmode=disable", "?sslmode=allow"])
def test_rds_mode_fails_closed_without_strong_ssl(suffix: str) -> None:
    with pytest.raises(DatabaseConfigurationError, match="sslmode"):
        validate_database_settings(
            target="rds-dev", url=_SYNTH_REMOTE + suffix, url_explicitly_set=True
        )


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_rds_mode_accepts_strong_ssl(mode: str) -> None:
    validate_database_settings(
        target="rds-dev", url=f"{_SYNTH_REMOTE}?sslmode={mode}", url_explicitly_set=True
    )


def test_settings_construction_enforces_the_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", _SYNTH_REMOTE)  # no sslmode
    monkeypatch.setenv("DATABASE_TARGET", "rds-dev")
    get_settings.cache_clear()
    try:
        with pytest.raises(DatabaseConfigurationError, match="sslmode") as excinfo:
            Settings(_env_file=None)  # type: ignore[call-arg]
        # The failure message never leaks the endpoint or credentials.
        assert "s3cret" not in str(excinfo.value)
        assert "example.amazonaws.com" not in str(excinfo.value)
    finally:
        get_settings.cache_clear()


def test_settings_rds_mode_with_ssl_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", _SYNTH_REMOTE_SSL)
    monkeypatch.setenv("DATABASE_TARGET", "rds-dev")
    get_settings.cache_clear()
    try:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.database_target == "rds-dev"
    finally:
        get_settings.cache_clear()


# --- engine gate ----------------------------------------------------------------


def test_engine_gate_refuses_remote_url_in_local_mode() -> None:
    with pytest.raises(DatabaseConfigurationError, match="rds-dev"):
        enforce_engine_url(_SYNTH_REMOTE_SSL, target="local")


def test_engine_gate_refuses_loopback_url_in_rds_mode() -> None:
    with pytest.raises(DatabaseConfigurationError, match="disagree"):
        enforce_engine_url(_LOCAL, target="rds-dev")


def test_create_db_engine_refuses_remote_url_under_local_target() -> None:
    with pytest.raises(DatabaseConfigurationError):
        create_db_engine(_SYNTH_REMOTE_SSL)


def test_create_db_engine_applies_conservative_pooling() -> None:
    settings = get_settings()
    engine = create_db_engine(_LOCAL)
    try:
        assert engine.pool.size() == settings.db_pool_size  # type: ignore[attr-defined]
        assert engine.pool._max_overflow == settings.db_max_overflow  # type: ignore[attr-defined]
        assert engine.pool._recycle == settings.db_pool_recycle_seconds  # type: ignore[attr-defined]
        assert engine.pool._pre_ping is True  # type: ignore[attr-defined]
    finally:
        engine.dispose()


def test_engine_applies_server_side_timeouts() -> None:
    from sqlalchemy import text

    settings = get_settings()
    engine = create_db_engine(_LOCAL)
    try:
        query = text("SELECT setting FROM pg_settings WHERE name = :n")  # value in ms
        with engine.connect() as conn:
            statement = conn.execute(query, {"n": "statement_timeout"}).scalar()
            lock = conn.execute(query, {"n": "lock_timeout"}).scalar()
            idle = conn.execute(query, {"n": "idle_in_transaction_session_timeout"}).scalar()
        assert statement == str(settings.db_statement_timeout_ms)
        assert lock == str(settings.db_lock_timeout_ms)
        assert idle == str(settings.db_idle_in_transaction_timeout_ms)
    finally:
        engine.dispose()


# --- live TLS verification (fail closed) -----------------------------------------


class _FakePGConn:
    def __init__(self, ssl_in_use: object) -> None:
        self.ssl_in_use = ssl_in_use


class _FakeDBAPIConn:
    def __init__(self, pgconn: object) -> None:
        self.pgconn = pgconn


def test_tls_assert_accepts_encrypted_connection() -> None:
    assert_connection_encrypted(_FakeDBAPIConn(_FakePGConn(True)))


@pytest.mark.parametrize("ssl_in_use", [False, None])
def test_tls_assert_refuses_unencrypted_connection(ssl_in_use: object) -> None:
    with pytest.raises(DatabaseConfigurationError, match="unencrypted"):
        assert_connection_encrypted(_FakeDBAPIConn(_FakePGConn(ssl_in_use)))


def test_tls_assert_fails_closed_when_undeterminable() -> None:
    with pytest.raises(DatabaseConfigurationError):
        assert_connection_encrypted(object())  # no pgconn attribute at all


# --- local-only operation guard ---------------------------------------------------


def test_local_only_guard_allows_local() -> None:
    ensure_local_only_operation(get_settings(), operation="test op")


def test_local_only_guard_refuses_rds_target() -> None:
    bad = Settings.model_construct(database_target="rds-dev", database_url=_SYNTH_REMOTE_SSL)
    with pytest.raises(RemoteDatabaseRefused, match="DATABASE_TARGET"):
        ensure_local_only_operation(bad, operation="test op")


def test_local_only_guard_refuses_remote_host_and_masks_it() -> None:
    bad = Settings.model_construct(database_target="local", database_url=_SYNTH_REMOTE_SSL)
    with pytest.raises(RemoteDatabaseRefused) as excinfo:
        ensure_local_only_operation(bad, operation="test op")
    message = str(excinfo.value)
    assert "non-loopback" in message
    assert "example.amazonaws.com" not in message
    assert "appuser" not in message
    assert "s3cret" not in message


# --- migration gate ------------------------------------------------------------------


def test_migration_gate_allows_loopback_without_token() -> None:
    ensure_migration_allowed(_LOCAL, command="upgrade", environ={})


def test_migration_gate_refuses_remote_without_token() -> None:
    with pytest.raises(RemoteDatabaseRefused, match="rds_migrate"):
        ensure_migration_allowed(_SYNTH_REMOTE_SSL, command="upgrade", environ={})


def test_migration_gate_allows_remote_with_token_and_ssl() -> None:
    ensure_migration_allowed(
        _SYNTH_REMOTE_SSL,
        command="upgrade",
        environ={safety.RDS_MIGRATION_ENV_VAR: safety.RDS_MIGRATION_ENV_VALUE},
    )


def test_migration_gate_refuses_remote_with_token_but_no_ssl() -> None:
    with pytest.raises(DatabaseConfigurationError, match="sslmode"):
        ensure_migration_allowed(
            _SYNTH_REMOTE,
            command="upgrade",
            environ={safety.RDS_MIGRATION_ENV_VAR: safety.RDS_MIGRATION_ENV_VALUE},
        )


def test_migration_gate_refuses_remote_downgrade_even_with_token() -> None:
    with pytest.raises(RemoteDatabaseRefused, match="downgrade"):
        ensure_migration_allowed(
            _SYNTH_REMOTE_SSL,
            command="downgrade",
            environ={safety.RDS_MIGRATION_ENV_VAR: safety.RDS_MIGRATION_ENV_VALUE},
        )


# --- dev_up refuses non-local -----------------------------------------------------


def test_dev_up_refuses_rds_target(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "dev_up_script_rds_guard", repo_root / "scripts" / "dev_up.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setenv("DATABASE_TARGET", "rds-dev")
    monkeypatch.setenv("DATABASE_URL", _SYNTH_REMOTE_SSL)
    get_settings.cache_clear()
    try:
        with pytest.raises(RemoteDatabaseRefused, match="dev_up"):
            module.database_url()
    finally:
        get_settings.cache_clear()
