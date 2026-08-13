"""The Email and Verification Studio pages render once a policy version exists.

Why this file exists
--------------------
Real UAT reported ``GET /admin/agents/studio/email`` returning
``internal_server_error`` on a deployed staging host while every test in the
suite passed. The cause was a Jinja filter-name mismatch: the environment
registers ``pretty_json`` (``app/web/routes.py``) and both Studio templates
called ``prettyjson``. Jinja resolves the name only when the branch that uses it
is taken, and that branch is guarded by ``{% if pattern_policy %}`` /
``{% if waterfall %}``.

That guard is exactly why no existing test caught it. The suite builds its schema
with ``create_all``, which does not run the EV-001 data migration, so the seeded
policy row is absent, the ``{% else %}`` branch renders, and the missing filter
is never looked up. A migrated database — every real deployment — takes the
other branch and raises ``TemplateRuntimeError: No filter named 'prettyjson'``.

The tests below therefore seed the policy rows explicitly. Seeding is the whole
point: a version of this test that relied on the ambient fixture state would be
green against the defect it exists to catch.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.email_verification_studio import (
    EmailPatternPolicyActivation,
    EmailPatternPolicyVersion,
    VerificationWaterfallActivation,
    VerificationWaterfallPolicyVersion,
)
from app.web.routes import templates
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

PATTERN_CONFIGURATION = {
    "schema_version": "email-pattern-policy/v1",
    "patterns": [
        {"id": "firstname.lastname", "enabled": True, "example": "ada.lovelace"},
        {"id": "firstname", "enabled": True, "example": "ada"},
    ],
    "max_candidates": 8,
    "learned_formats_first": True,
    "stop_after_accepted": True,
}

WATERFALL_CONFIGURATION = {
    "schema_version": "verification-waterfall/v1",
    "providers": [{"id": "millionverifier", "enabled": True}],
}


@pytest.fixture()
def studio_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _seed_pattern_policy(db: Session) -> EmailPatternPolicyVersion:
    row = EmailPatternPolicyVersion(
        id=uuid.uuid4(),
        version_number=1,
        schema_version="email-pattern-policy/v1",
        name="Initial Email pattern policy",
        configuration=PATTERN_CONFIGURATION,
        created_by="test",
    )
    db.add(row)
    db.flush()
    # A version alone is not the *active* policy — the page reads the one an
    # activation names, exactly as the EV-001 migration seeds it.
    db.add(
        EmailPatternPolicyActivation(
            id=uuid.uuid4(), policy_version_id=row.id, activated_by="test", reason="seed"
        )
    )
    db.flush()
    return row


def _seed_waterfall_policy(db: Session) -> VerificationWaterfallPolicyVersion:
    row = VerificationWaterfallPolicyVersion(
        id=uuid.uuid4(),
        version_number=1,
        schema_version="verification-waterfall/v1",
        name="Initial verification waterfall",
        configuration=WATERFALL_CONFIGURATION,
        created_by="test",
    )
    db.add(row)
    db.flush()
    db.add(
        VerificationWaterfallActivation(
            id=uuid.uuid4(), policy_version_id=row.id, activated_by="test", reason="seed"
        )
    )
    db.flush()
    return row


def test_the_email_studio_renders_when_a_pattern_policy_exists(
    studio_client: TestClient, db_session: Session
) -> None:
    """The exact UAT reproduction: a seeded policy took the branch that 500ed."""

    _seed_pattern_policy(db_session)

    response = studio_client.get("/admin/agents/studio/email")

    assert response.status_code == 200, response.text[:500]
    # The policy branch really was taken — otherwise this test would pass
    # against the defect, which is how the defect reached a deployment.
    assert "Initial Email pattern policy" in response.text
    assert "Unavailable until the EV-001 migration" not in response.text
    # And the configuration was rendered through the filter, not swallowed.
    assert "firstname.lastname" in response.text


def test_the_verification_studio_renders_when_a_waterfall_exists(
    studio_client: TestClient, db_session: Session
) -> None:
    """The same defect, same commit, on the sibling template."""

    _seed_waterfall_policy(db_session)

    response = studio_client.get("/admin/agents/studio/verification")

    assert response.status_code == 200, response.text[:500]
    assert "Initial verification waterfall" in response.text
    assert "Unavailable until the EV-001 migration" not in response.text
    assert "millionverifier" in response.text


def test_the_email_studio_still_renders_with_no_policy_seeded(
    studio_client: TestClient,
) -> None:
    """The pre-existing path stays working, so the repair is not a swap."""

    response = studio_client.get("/admin/agents/studio/email")

    assert response.status_code == 200
    assert "Unavailable until the EV-001 migration" in response.text


def test_every_studio_template_filter_is_registered() -> None:
    """The class of defect, not just its two instances.

    A filter name that no template can resolve is a page that 500s only for the
    data shape that reaches it. Asserting the whole registry closes the family
    rather than the two spellings that happened to be found in UAT.
    """

    missing: list[str] = []
    for name in ("dt", "pretty_json"):
        if name not in templates.env.filters:
            missing.append(name)
    assert not missing, f"templates reference filters that are not registered: {missing}"

    # And nothing still calls the unregistered spelling that caused the outage.
    import pathlib

    template_root = pathlib.Path(templates.env.loader.searchpath[0])  # type: ignore[union-attr]
    offenders = [
        str(path.relative_to(template_root))
        for path in template_root.rglob("*.html")
        if "prettyjson" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"templates still call the unregistered 'prettyjson' filter: {offenders}"
