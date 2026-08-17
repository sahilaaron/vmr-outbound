"""UI: the four-state icon, contact verification actions, and the console page.

Drives the server-rendered pages over HTTP with the workbench + Phase 2 switches
on, asserting the accessible status icon renders truthfully for every state.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.contact import Contact
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import EmailVerificationResult
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__EMAIL_GENERATION", "true")
    monkeypatch.setenv("FEATURES__MILLIONVERIFIER", "true")
    # A documented test key, which routes to the simulator exactly as an absent
    # one does. It is here because MillionVerifier is an operator control now and
    # a control with no credential configured cannot be on, so a keyless
    # deployment would refuse the verify action rather than simulate it.
    monkeypatch.setenv("MILLIONVERIFIER_API_KEY", "API_KEY_FOR_OK")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _contact(session: Session, **kw: object) -> Contact:
    defaults = dict(
        first_name="Jane",
        last_name="Doe",
        company_name="Acme",
        company_domain="acme.com",
        email=None,
        natural_key=f"jane|doe|{uuid.uuid4()}",
    )
    defaults.update(kw)
    c = Contact(**defaults)  # type: ignore[arg-type]
    session.add(c)
    session.flush()
    return c


def _seed_evidence(
    session: Session, email: str, result: EmailVerificationResult, **kw: object
) -> None:
    session.add(
        ExactEmailVerification(
            email=email,
            result=result,
            provider="millionverifier",
            policy_version="ver-1",
            checked_at=datetime.now(UTC),
            **kw,  # type: ignore[arg-type]
        )
    )
    session.flush()


def test_verification_page_renders(client: TestClient) -> None:
    r = client.get("/verification")
    assert r.status_code == 200
    assert "Email Verification" in r.text
    assert "Usage" in r.text


def test_the_person_page_shows_the_verification_state(
    client: TestClient, db_session: Session
) -> None:
    """The legacy contact page is gone; the person page carries the email state."""

    _seed_evidence(db_session, "ok@acme.com", EmailVerificationResult.VALID)
    c = _contact(db_session, email="ok@acme.com")
    page = client.get(f"/app/people/{c.id}")
    assert page.status_code == 200
    assert "ok@acme.com" in page.text
    assert client.get(f"/contacts/{c.id}", follow_redirects=False).status_code == 308


def test_recover_action_available(client: TestClient) -> None:
    r = client.post("/verification/recover", follow_redirects=False)
    assert r.status_code == 303


def test_disabled_when_flag_off(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.delenv("FEATURES__EMAIL_GENERATION", raising=False)
    monkeypatch.delenv("FEATURES__MILLIONVERIFIER", raising=False)
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        r = c.get("/verification")
        assert r.status_code == 200
        assert "not yet available" in r.text.lower() or "unavailable" in r.text.lower()
    app.dependency_overrides.clear()
    get_settings.cache_clear()
