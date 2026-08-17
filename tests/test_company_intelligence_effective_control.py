"""The Company Intelligence control is the *effective* one, not the raw env flag.

The 2026-08-16 Google Sheets UAT produced a deployment in a state nothing in the
test suite covered: ``FEATURES__COMPANY_INTELLIGENCE`` false in the environment,
and an administrator's ``operational_settings`` row turning Company Intelligence
**on**. Admin → Configuration reported the control as effective, because it reads
the effective layer. Two enforcement points did not:

* ``scripts/run_agent_worker.py`` refused to drain the queue, so 24 jobs enqueued
  by the Research handoff sat at ``PENDING`` with ``attempts=0`` and no lease, and
  every sequence written in that window recorded
  ``intelligence_lineage.status = "no_current_version"``.
* ``app.main.create_app`` did not mount the router, so every page in the area
  answered 404 while the screen said the control was on.

Both now resolve the control through ``app.services.operations.settings``. These
tests pin that, and — just as importantly — pin that the *off* case still behaves
exactly as it did: a control nobody has turned on is still off, and the routes
still do not exist.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.core.config import get_settings
from app.main import create_app
from app.services.operations import settings as operational
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

ACTOR = "operator@example.com"


@pytest.fixture()
def env_flag_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The staging shape: workbench on, Company Intelligence off in the environment."""

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.delenv("FEATURES__COMPANY_INTELLIGENCE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def client() -> TestClient:
    return TestClient(create_app(get_settings()))


def _turn_on(session: Session) -> None:
    operational.set_control(
        session,
        key="company_intelligence",
        enabled_value=True,
        actor=ACTOR,
        reason="UAT: classification never ran",
        settings=get_settings(),
    )
    session.commit()


# ---------------------------------------------------------------------------
# The deployment default is still respected when nobody has decided otherwise
# ---------------------------------------------------------------------------


def test_routes_still_absent_when_nobody_turned_the_control_on(
    env_flag_off: None, committed_session: Session
) -> None:
    """The original contract: off means the paths do not exist."""

    with client() as http:
        assert http.get("/admin/company-intelligence").status_code == 404
        assert http.get("/admin/company-intelligence/taxonomy").status_code == 404
        assert http.get("/admin/company-intelligence/backfill").status_code == 404


def test_worker_declines_to_drain_when_nobody_turned_the_control_on(
    env_flag_off: None, committed_session: Session
) -> None:
    from scripts import run_agent_worker

    assert (
        run_agent_worker._run_company_intelligence_once(worker_id="w1", lease_seconds=60.0) is None
    )


# ---------------------------------------------------------------------------
# The administrator's row wins over the deployment default
# ---------------------------------------------------------------------------


def test_admin_switch_makes_the_routes_exist_without_touching_the_environment(
    env_flag_off: None, committed_session: Session
) -> None:
    """The regression: the env flag is false and the area must still be reachable."""

    settings = get_settings()
    assert settings.features.company_intelligence is False, (
        "the fixture must reproduce the staging shape: env flag off"
    )

    with client() as http:
        assert http.get("/admin/company-intelligence").status_code == 404

    _turn_on(committed_session)
    assert operational.enabled(committed_session, "company_intelligence", settings) is True

    with client() as http:
        for path in (
            "/admin/company-intelligence",
            "/admin/company-intelligence/taxonomy",
            "/admin/company-intelligence/backfill",
        ):
            assert http.get(path).status_code == 200, (
                f"{path} answered 404 while the effective control was on — the router "
                "is gated on the environment again"
            )


def test_admin_switch_makes_the_shared_worker_drain_the_queue(
    env_flag_off: None, committed_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue was full and nothing claimed from it. Prove the claim is attempted."""

    from scripts import run_agent_worker

    claimed: list[str] = []

    def _record(session: Session, *, worker_id: str, lease_seconds: float) -> None:
        claimed.append(worker_id)
        return None

    monkeypatch.setattr(run_agent_worker.ci_runner, "run_next", _record)

    # Off: the runner must never be reached at all.
    run_agent_worker._run_company_intelligence_once(worker_id="before", lease_seconds=60.0)
    assert claimed == [], "the drain gate let a claim through while the control was off"

    _turn_on(committed_session)

    run_agent_worker._run_company_intelligence_once(worker_id="after", lease_seconds=60.0)
    assert claimed == ["after"], (
        "the shared worker did not attempt a claim while the effective control was on — "
        "this is the defect that left 24 jobs at PENDING with attempts=0"
    )


def test_standalone_worker_starts_on_the_effective_control(
    env_flag_off: None, committed_session: Session
) -> None:
    """It refused to start on the env flag, contradicting the Admin screen."""

    from scripts import run_company_intelligence_worker as standalone

    assert standalone.main(["--once"]) == 2, "must refuse while the control is genuinely off"

    _turn_on(committed_session)

    # `--once` claims at most one job and exits; the queue is empty here, so a
    # start that gets as far as reporting an idle queue is the proof we want.
    assert standalone.main(["--once"]) == 0, (
        "the standalone worker refused to start while the effective control was on"
    )
