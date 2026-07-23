"""Browser-facing tests for the ambiguity review & resolution workbench pages.

These drive the server-rendered review queue, the resolution detail page, the
consequence-preview confirmation step, and the apply step over HTTP (TestClient),
covering the required browser-level review and confirmation flow for both a
row assignment and a destructive merge.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import ImportRowOutcome
from app.models.import_batch import ImportRowValidation
from app.services.campaigns import create_campaign
from app.services.imports.importer import run_import
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session


@pytest.fixture()
def client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    monkeypatch.setenv("STAGED_UPLOADS_DIR", str(tmp_path / "staged"))
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _emailless_duplicates(session: Session, domain: str = "casterly.example") -> list[Contact]:
    """Two email-less contacts sharing a natural key (mergeable and assignable)."""

    a = Contact(
        first_name="Tywin",
        last_name="Lannister",
        company_name="Casterly",
        company_domain=domain,
        natural_key=f"tywin|lannister|{domain}",
    )
    b = Contact(
        first_name="Tywin",
        last_name="Lannister",
        company_name="Casterly",
        company_domain=domain,
        natural_key=f"tywin|lannister|{domain}",
    )
    session.add_all([a, b])
    session.flush()
    return [a, b]


def _import_ambiguous(session: Session, campaign: Campaign, domain: str) -> uuid.UUID:
    csv = (
        b"first_name,last_name,company_name,company_domain\n"
        + f"Tywin,Lannister,Casterly,{domain}\n".encode()
    )
    summary = run_import(session, campaign_id=campaign.id, content=csv, filename="ambig.csv")
    assert summary.ambiguous_rows == 1
    validation = session.scalars(
        select(ImportRowValidation).where(ImportRowValidation.outcome == ImportRowOutcome.AMBIGUOUS)
    ).first()
    return validation.import_row_id


@pytest.fixture()
def ambiguous(client: TestClient, db_session: Session) -> tuple[uuid.UUID, list[Contact], Campaign]:
    candidates = _emailless_duplicates(db_session)
    campaign = create_campaign(db_session, name="Review Campaign")
    row_id = _import_ambiguous(db_session, campaign, "casterly.example")
    return row_id, candidates, campaign


# --- Gating ------------------------------------------------------------------


def test_review_pages_gated_off_by_default(db_session: Session) -> None:
    get_settings.cache_clear()
    app = create_app()  # workbench switch off

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        assert c.get("/review").status_code == 404
    app.dependency_overrides.clear()


# --- Queue & detail ----------------------------------------------------------


def test_queue_lists_ambiguous_row_and_nav_badge(
    client: TestClient, ambiguous: tuple[uuid.UUID, list[Contact], Campaign]
) -> None:
    row_id, _candidates, _campaign = ambiguous
    resp = client.get("/review")
    assert resp.status_code == 200
    assert "Tywin" in resp.text
    assert f"/review/rows/{row_id}" in resp.text
    # The rail badge advertises one open review.
    assert "Review" in resp.text


def test_detail_shows_raw_normalized_and_candidates(
    client: TestClient, ambiguous: tuple[uuid.UUID, list[Contact], Campaign]
) -> None:
    row_id, candidates, _campaign = ambiguous
    resp = client.get(f"/review/rows/{row_id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Imported row" in body and "Possible existing matches" in body
    # Both candidate contacts are offered.
    for c in candidates:
        assert str(c.id) in body


def test_detail_unknown_row_is_not_found(client: TestClient) -> None:
    assert client.get(f"/review/rows/{uuid.uuid4()}").status_code == 404
    assert client.get("/review/rows/not-a-uuid").status_code == 404


# --- Assign flow: preview -> confirm -----------------------------------------


def test_assign_preview_then_confirm_resolves(
    client: TestClient, ambiguous: tuple[uuid.UUID, list[Contact], Campaign], db_session: Session
) -> None:
    row_id, candidates, campaign = ambiguous
    target = candidates[0]

    # Step 1: preview shows the consequences, mutates nothing.
    preview = client.post(
        f"/review/rows/{row_id}/preview",
        data={"action": "assign_existing", "target_contact_id": str(target.id)},
    )
    assert preview.status_code == 200
    assert "Consequences" in preview.text
    assert (
        db_session.scalar(
            select(func.count(CampaignContact.id)).where(
                CampaignContact.campaign_id == campaign.id,
                CampaignContact.contact_id == target.id,
            )
        )
        == 0
    )  # nothing written yet

    # Step 2: confirm applies and redirects to the resolved contact.
    confirm = client.post(
        f"/review/rows/{row_id}/resolve",
        data={"action": "assign_existing", "target_contact_id": str(target.id), "reason": "same"},
        follow_redirects=False,
    )
    assert confirm.status_code == 303
    assert confirm.headers["location"].startswith(f"/contacts/{target.id}")
    assert (
        db_session.scalar(
            select(func.count(CampaignContact.id)).where(
                CampaignContact.campaign_id == campaign.id,
                CampaignContact.contact_id == target.id,
            )
        )
        == 1
    )
    # The row has left the queue.
    assert "Tywin" not in client.get("/review").text


def test_confirm_is_idempotent_over_http(
    client: TestClient, ambiguous: tuple[uuid.UUID, list[Contact], Campaign], db_session: Session
) -> None:
    row_id, candidates, _campaign = ambiguous
    target = candidates[0]
    data = {"action": "assign_existing", "target_contact_id": str(target.id)}
    first = client.post(f"/review/rows/{row_id}/resolve", data=data, follow_redirects=False)
    second = client.post(f"/review/rows/{row_id}/resolve", data=data, follow_redirects=False)
    assert first.status_code == 303 and second.status_code == 303
    # Exactly one membership; the second submission changed nothing.
    assert (
        db_session.scalar(
            select(func.count(CampaignContact.id)).where(CampaignContact.contact_id == target.id)
        )
        == 1
    )


# --- Merge flow: destructive, needs confirmation -----------------------------


def test_merge_preview_then_confirm(
    client: TestClient, ambiguous: tuple[uuid.UUID, list[Contact], Campaign], db_session: Session
) -> None:
    row_id, candidates, _campaign = ambiguous
    survivor, loser = candidates

    preview = client.post(
        f"/review/rows/{row_id}/preview",
        data={
            "action": "merge",
            "target_contact_id": str(survivor.id),
            "merged_contact_id": str(loser.id),
        },
    )
    assert preview.status_code == 200
    assert "destructive merge" in preview.text.lower()

    confirm = client.post(
        f"/review/rows/{row_id}/resolve",
        data={
            "action": "merge",
            "target_contact_id": str(survivor.id),
            "merged_contact_id": str(loser.id),
            "reason": "same person",
        },
        follow_redirects=False,
    )
    assert confirm.status_code == 303
    db_session.refresh(loser)
    assert loser.merged_into_id == survivor.id  # tombstoned, not deleted
    assert db_session.get(Contact, loser.id) is not None


# --- Malformed / unauthorized ------------------------------------------------


def test_assign_without_target_redirects_with_error(
    client: TestClient, ambiguous: tuple[uuid.UUID, list[Contact], Campaign]
) -> None:
    row_id, _candidates, _campaign = ambiguous
    resp = client.post(
        f"/review/rows/{row_id}/preview",
        data={"action": "assign_existing"},  # no target
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]


def test_resolve_unknown_action_redirects_with_error(
    client: TestClient, ambiguous: tuple[uuid.UUID, list[Contact], Campaign]
) -> None:
    row_id, _candidates, _campaign = ambiguous
    resp = client.post(
        f"/review/rows/{row_id}/resolve",
        data={"action": "nonsense"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]


# --- Review-verdict corrections (PR #122): HTTP negative tests ----------------


def test_http_merge_rejects_non_candidate(
    client: TestClient, ambiguous: tuple[uuid.UUID, list[Contact], Campaign], db_session: Session
) -> None:
    """Finding 1: a forged merge POST naming a non-candidate contact is refused."""

    row_id, candidates, _campaign = ambiguous
    stranger = Contact(
        first_name="Varys",
        last_name="Spider",
        company_name="Kings Landing",
        company_domain="kingslanding.example",
        natural_key="varys|spider|kingslanding.example",
    )
    db_session.add(stranger)
    db_session.flush()

    resp = client.post(
        f"/review/rows/{row_id}/resolve",
        data={
            "action": "merge",
            "target_contact_id": str(candidates[0].id),
            "merged_contact_id": str(stranger.id),
            "reason": "forged",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]
    db_session.refresh(stranger)
    assert stranger.merged_into_id is None


def test_http_merge_requires_reason(
    client: TestClient, ambiguous: tuple[uuid.UUID, list[Contact], Campaign], db_session: Session
) -> None:
    """Finding 4: a merge POST with an empty reason is rejected at the route."""

    row_id, candidates, _campaign = ambiguous
    survivor, loser = candidates
    resp = client.post(
        f"/review/rows/{row_id}/resolve",
        data={
            "action": "merge",
            "target_contact_id": str(survivor.id),
            "merged_contact_id": str(loser.id),
            "reason": "   ",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]
    db_session.refresh(loser)
    assert loser.merged_into_id is None  # nothing merged
