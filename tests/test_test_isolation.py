"""The test harness's own guarantees.

These tests exist because the suite once passed while silently reading the
operator's development database and ``.env``. That produced two bad outcomes at
once: ~99 failures that looked like product regressions but were pollution, and
— far worse — a green run that proved nothing, because the assertions had never
been isolated from real data.

So the isolation itself is now covered. If someone weakens the root
``conftest.py``, these fail rather than the suite quietly going back to reading
live data.
"""

from __future__ import annotations

import os

import pytest
from app.core.config import Settings, get_settings

import conftest as root_conftest


class TestDatabaseSafety:
    """The suite must be provably pointed at a disposable test database."""

    def test_the_configured_database_is_a_test_database(self) -> None:
        name = root_conftest._database_name(get_settings().database_url)
        assert root_conftest.TEST_DB_NAME_PATTERN.match(name), (
            f"The suite is running against {name!r}, which is not a recognised test database name."
        )

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+psycopg://dev@127.0.0.1:5433/vmr_dev",
            "postgresql+psycopg://u:p@rds.amazonaws.com:5432/vmroutbound",
            "postgresql+psycopg://postgres@127.0.0.1:5432/postgres",
            "postgresql+psycopg://postgres@127.0.0.1:5432/vmr",
            "postgresql+psycopg://postgres@127.0.0.1:5432/vmr_development",
        ],
    )
    def test_a_non_test_database_is_refused(self, url: str) -> None:
        """The guard must refuse before anything connects, let alone writes."""

        with pytest.raises(root_conftest.UnsafeTestDatabase):
            root_conftest._assert_safe(url)

    @pytest.mark.parametrize("host", ["postgres", "db", "database", "POSTGRES"])
    def test_a_service_host_is_refused_outside_ci(self, host: str) -> None:
        """`postgres` is a container service name, not a machine.

        On a developer box it resolves to the real development database, so it
        is refused — even though the database name itself is acceptable.
        """

        with pytest.raises(root_conftest.UnsafeTestDatabase):
            root_conftest._assert_safe(
                f"postgresql+psycopg://u@{host}:5432/vmr_test", trusted_ci=False
            )

    def test_a_service_host_is_accepted_inside_verified_ci(self) -> None:
        """GitHub Actions legitimately addresses its own throwaway service."""

        root_conftest._assert_safe(
            "postgresql+psycopg://dev:dev@postgres:5432/vmr_test", trusted_ci=True
        )

    @pytest.mark.parametrize("name", ["vmr_dev", "postgres", "vmroutbound"])
    def test_ci_never_unlocks_a_non_test_database_name(self, name: str) -> None:
        """The CI allowance narrows the *host* rule only.

        A verified CI run may use the service host; it may not use a database
        the suite is not allowed to truncate. If this ever passed, a workflow
        misconfiguration could wipe a real database.
        """

        with pytest.raises(root_conftest.UnsafeTestDatabase):
            root_conftest._assert_safe(
                f"postgresql+psycopg://dev:dev@postgres:5432/{name}", trusted_ci=True
            )

    @pytest.mark.parametrize("name", ["vmr_test", "vmr_test_local", "vmr_test_ci_2"])
    def test_clearly_named_test_databases_are_accepted(self, name: str) -> None:
        root_conftest._assert_safe(f"postgresql+psycopg://postgres@127.0.0.1:5433/{name}")


class TestEnvIsolation:
    """`.env` must not reach the suite, in either direction."""

    def test_test_mode_is_active(self) -> None:
        assert os.environ.get("VMR_TEST_MODE") == "1"

    def test_settings_do_not_read_the_project_dotenv(self) -> None:
        """The whole defect in one assertion.

        A real ``.env`` may sit beside this checkout with the workbench on and a
        live MillionVerifier key in it. Under test it must be invisible.
        """

        assert Settings.model_config.get("env_file") is None

    def test_every_feature_switch_defaults_off(self) -> None:
        enabled = [name for name, on in get_settings().features.model_dump().items() if on]
        assert enabled == [], f"features leaked in as enabled: {enabled}"

    def test_no_provider_credential_is_reachable(self) -> None:
        """A live key here would spend real credits from an automated run."""

        settings = get_settings()
        assert settings.millionverifier_api_key is None
        assert settings.logo_dev_api_key is None
        assert settings.has_millionverifier_key() is False
        assert settings.has_logo_dev_key() is False

    def test_no_feature_variable_survives_in_the_environment(self) -> None:
        leaked = [name for name in os.environ if name.startswith("FEATURES__")]
        assert leaked == [], f"FEATURES__ variables leaked from the shell: {leaked}"

    def test_deleting_a_feature_variable_restores_the_disabled_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The specific trap: delenv must not fall back through to `.env`.

        With ``env_file`` still set, removing the variable would hand control
        back to the dotenv file — so a test that thought it had disabled a
        feature would silently run with it enabled.
        """

        monkeypatch.setenv("FEATURES__WORKBENCH", "true")
        get_settings.cache_clear()
        assert get_settings().features.workbench is True

        monkeypatch.delenv("FEATURES__WORKBENCH")
        get_settings.cache_clear()
        assert get_settings().features.workbench is False

    def test_a_feature_fixture_still_works(self, enable_csv_import: None) -> None:
        """Opting in must remain possible, or the isolation is useless."""

        assert get_settings().features.csv_import is True


class TestDatabaseIsolationBetweenTests:
    """Each test must see only rows it created."""

    def test_first_test_creates_a_contact(self, db_session: object) -> None:
        from app.models.contact import Contact

        db_session.add(  # type: ignore[attr-defined]
            Contact(
                first_name="Leak",
                last_name="Detector",
                company_name="Co",
                company_domain="leak.test",
                natural_key="leak-detector",
            )
        )
        db_session.flush()  # type: ignore[attr-defined]

    def test_second_test_sees_an_empty_table(self, db_session: object) -> None:
        """Runs after the test above; must not observe its row."""

        from app.models.contact import Contact
        from sqlalchemy import func, select

        count = db_session.scalar(select(func.count()).select_from(Contact))  # type: ignore[attr-defined]
        assert count == 0, (
            "A previous test's data survived — the suite is not isolated, and "
            "any count-based assertion in it is meaningless."
        )


class TestAlembicPercentEscaping:
    """Percent-encoded credentials must survive Alembic's ConfigParser."""

    def test_a_percent_encoded_password_round_trips(self) -> None:
        """The reported failure: `%23` in a password.

        ConfigParser interpolates on read, so a raw ``%23`` raises
        ``ValueError: invalid interpolation syntax``. Doubling on write means it
        collapses back to exactly the original URL on read.
        """

        from configparser import ConfigParser

        from app.db.alembic_url import escape_for_alembic_config

        url = "postgresql+psycopg://vmr:dbPost%232026@rds.example.com:5432/vmr_test"

        parser = ConfigParser()
        parser.add_section("alembic")
        parser.set("alembic", "sqlalchemy.url", escape_for_alembic_config(url))

        assert parser.get("alembic", "sqlalchemy.url") == url

    def test_a_raw_percent_url_would_have_failed(self) -> None:
        """Proves the escaping is load-bearing, not decorative.

        ConfigParser validates interpolation syntax on *write*, so the
        unescaped URL raises at ``set`` rather than at ``get``. That is exactly
        the reported failure: ``alembic upgrade head`` died with
        ``ValueError: invalid interpolation syntax`` before it ran anything.
        """

        from configparser import ConfigParser

        parser = ConfigParser()
        parser.add_section("alembic")
        with pytest.raises(ValueError, match="invalid interpolation syntax"):
            parser.set(
                "alembic",
                "sqlalchemy.url",
                "postgresql+psycopg://vmr:dbPost%232026@rds.example.com:5432/vmr_test",
            )

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+psycopg://postgres@127.0.0.1:5433/vmr_test",
            "postgresql+psycopg://u:p%40ss@h:5432/d",
            "postgresql+psycopg://u:a%23b%25c@h:5432/d",
        ],
    )
    def test_escaping_is_lossless_for_any_url(self, url: str) -> None:
        from configparser import ConfigParser

        from app.db.alembic_url import escape_for_alembic_config

        parser = ConfigParser()
        parser.add_section("alembic")
        parser.set("alembic", "sqlalchemy.url", escape_for_alembic_config(url))
        assert parser.get("alembic", "sqlalchemy.url") == url


class TestTrustedCiDetection:
    """What counts as a verified GitHub Actions run.

    Every condition must hold together. A single exported variable must not
    unlock the service-host allowance, or a developer who habitually sets CI=true
    would silently lose the protection.
    """

    @pytest.fixture(autouse=True)
    def _clean_ci_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("CI", "GITHUB_ACTIONS"):
            monkeypatch.delenv(name, raising=False)
        for name in root_conftest.PROVIDER_KEY_VARS:
            monkeypatch.delenv(name, raising=False)

    def test_both_variables_together_are_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        assert root_conftest.is_trusted_ci() is True

    @pytest.mark.parametrize("only", ["CI", "GITHUB_ACTIONS"])
    def test_spoofing_one_variable_is_not_enough(
        self, monkeypatch: pytest.MonkeyPatch, only: str
    ) -> None:
        monkeypatch.setenv(only, "true")
        assert root_conftest.is_trusted_ci() is False

    def test_a_provider_credential_revokes_trust(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A live key present means the scrub did not happen — so this is not our CI."""

        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("MILLIONVERIFIER_API_KEY", "real-key")
        assert root_conftest.is_trusted_ci() is False

    def test_an_inherited_feature_flag_revokes_trust(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("FEATURES__WORKBENCH", "true")
        assert root_conftest.is_trusted_ci() is False

    @pytest.mark.parametrize("value", ["1", "yes", "True", "TRUE", ""])
    def test_only_the_exact_string_true_counts(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """GitHub sets exactly "true"; anything else is someone else's variable."""

        monkeypatch.setenv("CI", value)
        monkeypatch.setenv("GITHUB_ACTIONS", value)
        assert root_conftest.is_trusted_ci() is False

    def test_a_service_host_is_refused_when_ci_is_only_half_spoofed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: the guard consults is_trusted_ci when not told explicitly."""

        monkeypatch.setenv("CI", "true")
        with pytest.raises(root_conftest.UnsafeTestDatabase):
            root_conftest._assert_safe("postgresql+psycopg://u@postgres:5432/vmr_test")


class TestDatabaseUrlResolution:
    """The suite borrows a server, never a database name."""

    def test_an_inherited_url_contributes_credentials_but_not_its_database(self) -> None:
        """The fix for CI #81.

        The runner's Postgres service has its own superuser (`dev`), so the
        previously hard-coded `postgres` login could not even open a maintenance
        connection. Borrowing the inherited credentials fixes that; replacing the
        database name keeps `vmr_dev` unreachable.
        """

        resolved = root_conftest.resolve_test_database_url(
            inherited="postgresql+psycopg://dev:dev@127.0.0.1:5433/vmr_dev"
        )
        assert resolved == "postgresql+psycopg://dev:dev@127.0.0.1:5433/vmr_test"
        root_conftest._assert_safe(resolved)

    def test_an_explicit_override_wins(self) -> None:
        resolved = root_conftest.resolve_test_database_url(
            explicit="postgresql+psycopg://me@127.0.0.1:5434/vmr_test_local",
            inherited="postgresql+psycopg://dev:dev@127.0.0.1:5433/vmr_dev",
        )
        assert resolved.endswith("/vmr_test_local")

    def test_the_default_is_used_when_nothing_is_inherited(self) -> None:
        assert root_conftest.resolve_test_database_url() == root_conftest.DEFAULT_TEST_URL

    def test_an_rds_url_contributes_only_its_server_and_is_then_guarded(self) -> None:
        """Even a production URL cannot nominate the database."""

        resolved = root_conftest.resolve_test_database_url(
            inherited="postgresql+psycopg://u:p@rds.amazonaws.com:5432/vmroutbound"
        )
        assert _name_of(resolved) == "vmr_test"
        # The name now passes, but the host is not a service name either, so the
        # guard permits it. That is intentional: the protection is that the
        # suite can only ever touch a database it created and may truncate.
        root_conftest._assert_safe(resolved)


def _name_of(url: str) -> str:
    return root_conftest._database_name(url)
