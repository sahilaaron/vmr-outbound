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
    def test_a_development_service_host_is_refused(self, host: str) -> None:
        """`postgres` is the Compose service name for real development data."""

        with pytest.raises(root_conftest.UnsafeTestDatabase):
            root_conftest._assert_safe(f"postgresql+psycopg://u@{host}:5432/vmr_test")

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
