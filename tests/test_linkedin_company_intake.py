"""LinkedIn company-page capture intake tests (DAT-012G).

Proves: immutable evidence storage, idempotency, exact-only matching (URL
lineage / unique domain), review-only name similarity, deterministic-only
headquarters parsing, and that no canonical company record is ever rewritten.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.company import Company
from app.models.enums import LinkedInSnapshotOutcome
from app.models.linkedin_company import LinkedInCompanySnapshot
from app.services.imports.linkedin_company_intake import (
    parse_headquarters,
    stage_company_snapshot,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PAYLOAD = json.loads(
    (
        REPO_ROOT
        / "extensions"
        / "salesnav-capture"
        / "docs"
        / "fixtures"
        / "company.payload.example.json"
    ).read_text("utf-8")
)

INTAKE_URL = "/api/intake/linkedin-company/stage"
EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


@pytest.fixture()
def enable_company_intake(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FEATURES__LINKEDIN_COMPANY_INTAKE", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _payload(**overrides) -> dict:
    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    payload["client_capture_id"] = str(uuid.uuid4())
    payload["campaign_id"] = None
    payload.update(overrides)
    return payload


def _stage(db: Session, payload: dict):
    return stage_company_snapshot(db, payload=payload, operator_base_url="http://127.0.0.1:8000")


# --- Route + storage ----------------------------------------------------------


def test_endpoint_is_404_while_feature_disabled(client: TestClient) -> None:
    resp = client.post(INTAKE_URL, json=_payload(), headers={"origin": EXTENSION_ORIGIN})
    assert resp.status_code == 404


def test_stage_persists_immutable_company_evidence(
    client: TestClient, enable_company_intake: None, db_session: Session
) -> None:
    payload = _payload()
    resp = client.post(INTAKE_URL, json=payload, headers={"origin": EXTENSION_ORIGIN})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["outcome"] == "unmatched_staged"
    assert body["operator_workbench_url"].endswith(f"/company-profiles/{body['snapshot_id']}")

    snapshot = db_session.get(LinkedInCompanySnapshot, uuid.UUID(body["snapshot_id"]))
    assert snapshot is not None
    assert snapshot.payload == payload
    assert snapshot.normalized_company_url == "https://www.linkedin.com/company/meridian-works"
    assert snapshot.company_linkedin_id == "meridian-works"
    assert snapshot.website_domain == "meridianworks.example"
    # Deterministic three-part headquarters parse.
    assert (snapshot.hq_city, snapshot.hq_region, snapshot.hq_country) == (
        "Austin",
        "Texas",
        "United States",
    )
    # No canonical company was created or modified.
    assert db_session.scalar(select(func.count()).select_from(Company)) == 0


def test_retry_is_idempotent_and_conflict_detected(
    db_session: Session,
) -> None:
    payload = _payload()
    first = _stage(db_session, payload)
    replay = _stage(db_session, payload)
    assert replay.already_received is True
    assert replay.snapshot_id == first.snapshot_id

    changed = copy.deepcopy(payload)
    changed["company"]["industry"] = "Changed"
    from app.services.imports.linkedin_company_intake import IdempotencyConflictError

    with pytest.raises(IdempotencyConflictError):
        _stage(db_session, changed)


# --- Matching ------------------------------------------------------------------


def test_exact_unique_domain_links_existing_company_without_rewriting_it(
    db_session: Session,
) -> None:
    company = Company(name="Meridian Works LLC", domain="meridianworks.example")
    db_session.add(company)
    db_session.flush()
    before_name = company.name

    result = _stage(db_session, _payload())
    snapshot = db_session.get(LinkedInCompanySnapshot, uuid.UUID(result.snapshot_id))
    assert snapshot is not None
    assert result.outcome == LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED.value
    assert snapshot.matched_company_id == company.id
    # Evidence linked; canonical company untouched.
    assert company.name == before_name
    assert company.industry is None


def test_url_lineage_links_later_snapshots_to_the_same_company(
    db_session: Session,
) -> None:
    company = Company(name="Meridian Works", domain="meridianworks.example")
    db_session.add(company)
    db_session.flush()
    _stage(db_session, _payload())  # links via domain

    # A second capture of the same page, now WITHOUT a usable website value:
    payload = _payload()
    payload["company"]["website"] = None
    result = _stage(db_session, payload)
    snapshot = db_session.get(LinkedInCompanySnapshot, uuid.UUID(result.snapshot_id))
    assert snapshot is not None
    assert snapshot.matched_company_id == company.id
    assert result.outcome == LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED.value


def test_name_similarity_is_review_only(db_session: Session) -> None:
    company = Company(name="Meridian Works Group", domain="different.example")
    db_session.add(company)
    db_session.flush()

    payload = _payload()
    payload["company"]["website"] = None  # no domain evidence
    result = _stage(db_session, payload)
    snapshot = db_session.get(LinkedInCompanySnapshot, uuid.UUID(result.snapshot_id))
    assert snapshot is not None
    assert result.outcome == LinkedInSnapshotOutcome.UNMATCHED_STAGED.value
    assert snapshot.matched_company_id is None
    assert snapshot.review_candidates
    assert snapshot.review_candidates[0]["company_id"] == str(company.id)
    assert snapshot.review_candidates[0]["auto_merge"] is False


def test_ambiguous_domain_requires_review_not_a_link(db_session: Session) -> None:
    # Two companies, neither carrying the captured domain, both name-similar:
    db_session.add(Company(name="Meridian Works East", domain="east.example"))
    db_session.add(Company(name="Meridian Works West", domain="west.example"))
    db_session.flush()
    payload = _payload()
    payload["company"]["website"] = None
    result = _stage(db_session, payload)
    snapshot = db_session.get(LinkedInCompanySnapshot, uuid.UUID(result.snapshot_id))
    assert snapshot is not None
    assert result.outcome == LinkedInSnapshotOutcome.UNMATCHED_STAGED.value
    assert len(snapshot.review_candidates or []) == 2


# --- Headquarters parsing -------------------------------------------------------


def test_headquarters_parse_is_deterministic_only() -> None:
    assert parse_headquarters("Austin, Texas, United States") == (
        "Austin",
        "Texas",
        "United States",
    )
    # Two parts are ambiguous (region vs country): nothing is parsed.
    assert parse_headquarters("Austin, Texas") == (None, None, None)
    assert parse_headquarters("Berlin") == (None, None, None)
    assert parse_headquarters(None) == (None, None, None)
    assert parse_headquarters("A, B, C, D") == (None, None, None)


def test_validation_rejects_non_company_urls(db_session: Session) -> None:
    from app.services.imports.linkedin_company_intake import ValidationFailedError

    payload = _payload()
    payload["company"]["company_linkedin_url"] = "https://www.linkedin.com/company/unavailable"
    with pytest.raises(ValidationFailedError):
        _stage(db_session, payload)
