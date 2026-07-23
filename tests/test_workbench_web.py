"""Browser-facing workbench tests: shell gating, pages, and the two-step import.

These drive the server-rendered pages over HTTP (TestClient) — upload, sheet
selection, mapping, preview (proving nothing persists), confirm, idempotent
re-confirm, row inspection, and the local-tools guards.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.contact import Contact
from app.services.campaigns import create_campaign
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

VALID_CSV = (
    b"first_name,last_name,company_name,company_domain,email\n"
    b"Ada,Lovelace,Engines,engines.example,ada@engines.example\n"
    b"Bad,,NoDomain,,broken\n"
)


def _workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "People"
    sheet.append(["First Name", "Surname", "Company", "Website"])
    sheet.append(["Grace", "Hopper", "Compilers", "compilers.example"])
    workbook.create_sheet(title="Notes")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture()
def client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    """Workbench-enabled app whose DB dependency is the rolled-back session."""

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


@pytest.fixture()
def disabled_client(db_session: Session) -> Iterator[TestClient]:
    """App with the workbench switch off (default): no UI routes exist."""

    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --- Shell & gating -------------------------------------------------------------


def test_workbench_disabled_by_default(disabled_client: TestClient) -> None:
    assert disabled_client.get("/").status_code == 404
    assert disabled_client.get("/contacts").status_code == 404


def test_functional_pages_render(client: TestClient) -> None:
    for path in ("/", "/campaigns", "/imports", "/imports/new", "/contacts", "/local-tools"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "VMR Outbound" in response.text


def test_later_phase_sections_show_one_clean_unavailable_state(client: TestClient) -> None:
    for path in (
        "/verification",
        "/scoring",
        "/research",
        "/drafts",
        "/sequences",
        "/activity",
        "/settings",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "isn't available yet" in response.text
        # No fake tables/scores/drafts on later-phase pages.
        assert "<table" not in response.text


def test_unknown_records_render_not_found(client: TestClient) -> None:
    assert client.get("/campaigns/not-a-uuid").status_code == 404
    assert client.get(f"/imports/{'0' * 32}").status_code == 404
    assert client.get("/contacts/11111111-1111-1111-1111-111111111111").status_code == 404


# --- Campaigns -------------------------------------------------------------------


def test_campaign_create_and_detail(client: TestClient) -> None:
    response = client.post(
        "/campaigns/create",
        data={"name": "Web Campaign", "description": "From the form", "status": "draft"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Web Campaign" in response.text
    # Duplicate names surface an actionable error, not a crash.
    duplicate = client.post(
        "/campaigns/create", data={"name": "Web Campaign"}, follow_redirects=True
    )
    assert "already exists" in duplicate.text


# --- CSV: upload -> mapping -> preview -> confirm ---------------------------------


def _upload_csv(client: TestClient, db_session: Session) -> str:
    campaign = create_campaign(db_session, name="CSV wizard")
    db_session.commit()
    response = client.post(
        "/imports/upload",
        data={"campaign_id": str(campaign.id), "source_name": "wizard test"},
        files={"file": ("wizard.csv", VALID_CSV, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "/mapping" in location
    return location.split("/imports/staged/")[1].split("/")[0]


def test_csv_wizard_end_to_end(client: TestClient, db_session: Session) -> None:
    staged_id = _upload_csv(client, db_session)

    # Mapping page pre-fills the exact-name mapping.
    mapping_page = client.get(f"/imports/staged/{staged_id}/mapping")
    assert mapping_page.status_code == 200
    assert "first_name" in mapping_page.text

    saved = client.post(
        f"/imports/staged/{staged_id}/mapping",
        data={
            "map__first_name": "first_name",
            "map__last_name": "last_name",
            "map__company_name": "company_name",
            "map__company_domain": "company_domain",
            "map__email": "email",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303

    # Preview shows predicted outcomes and persists NOTHING.
    before = db_session.scalar(select(func.count(Contact.id))) or 0
    preview = client.get(f"/imports/staged/{staged_id}/preview")
    assert preview.status_code == 200
    assert "Nothing has been written" in preview.text
    assert (db_session.scalar(select(func.count(Contact.id))) or 0) == before

    # Confirm commits the batch.
    confirmed = client.post(f"/imports/staged/{staged_id}/confirm", follow_redirects=False)
    assert confirmed.status_code == 303
    batch_url = confirmed.headers["location"].split("?")[0]
    detail = client.get(batch_url)
    assert detail.status_code == 200
    assert "wizard.csv" in detail.text
    assert (db_session.scalar(select(func.count(Contact.id))) or 0) == before + 1

    # Re-confirming the same staged upload cannot create a duplicate import.
    again = client.post(f"/imports/staged/{staged_id}/confirm", follow_redirects=False)
    assert again.status_code == 303
    assert again.headers["location"].split("?")[0] == batch_url
    assert (db_session.scalar(select(func.count(Contact.id))) or 0) == before + 1


def test_mapping_without_required_fields_is_blocked(
    client: TestClient, db_session: Session
) -> None:
    staged_id = _upload_csv(client, db_session)
    response = client.post(
        f"/imports/staged/{staged_id}/mapping",
        data={"map__first_name": "first_name"},
    )
    assert response.status_code == 400
    assert "required field" in response.text


def test_batch_rows_filter_and_row_inspection(client: TestClient, db_session: Session) -> None:
    staged_id = _upload_csv(client, db_session)
    client.post(
        f"/imports/staged/{staged_id}/mapping",
        data={
            "map__first_name": "first_name",
            "map__last_name": "last_name",
            "map__company_name": "company_name",
            "map__company_domain": "company_domain",
            "map__email": "email",
        },
    )
    confirmed = client.post(f"/imports/staged/{staged_id}/confirm", follow_redirects=False)
    batch_url = confirmed.headers["location"].split("?")[0]

    rejected_view = client.get(f"{batch_url}?outcome=rejected")
    assert rejected_view.status_code == 200
    assert (
        "missing_required" in rejected_view.text or "required but was empty" in rejected_view.text
    )

    # Row inspection compares original and normalized values.
    detail = client.get(batch_url)
    row_link = next(
        segment.split('"')[0]
        for segment in detail.text.split('href="')
        if "/rows/" in segment.split('"')[0]
    )
    row_page = client.get(row_link)
    assert row_page.status_code == 200
    assert "Original value" in row_page.text


# --- XLSX: sheet selection ---------------------------------------------------------


def test_xlsx_wizard_sheet_selection(client: TestClient, db_session: Session) -> None:
    campaign = create_campaign(db_session, name="XLSX wizard")
    db_session.commit()
    response = client.post(
        "/imports/upload",
        data={"campaign_id": str(campaign.id)},
        files={
            "file": (
                "book.xlsx",
                _workbook(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        follow_redirects=False,
    )
    location = response.headers["location"]
    assert "/sheets" in location
    staged_id = location.split("/imports/staged/")[1].split("/")[0]

    sheets_page = client.get(f"/imports/staged/{staged_id}/sheets")
    assert "People" in sheets_page.text and "Notes" in sheets_page.text

    selected = client.post(
        f"/imports/staged/{staged_id}/sheets", data={"sheet": "0"}, follow_redirects=False
    )
    assert selected.status_code == 303 and "/mapping" in selected.headers["location"]


def test_upload_rejects_unsupported_and_malformed_files(
    client: TestClient, db_session: Session
) -> None:
    campaign = create_campaign(db_session, name="Bad uploads")
    db_session.commit()
    xls = client.post(
        "/imports/upload",
        data={"campaign_id": str(campaign.id)},
        files={"file": ("legacy.xls", b"anything", "application/vnd.ms-excel")},
        follow_redirects=False,
    )
    assert xls.status_code == 303 and "err=" in xls.headers["location"]

    malformed = client.post(
        "/imports/upload",
        data={"campaign_id": str(campaign.id)},
        files={"file": ("fake.xlsx", b"not a workbook", "application/octet-stream")},
        follow_redirects=False,
    )
    assert malformed.status_code == 303 and "err=" in malformed.headers["location"]


# --- Upload size limit ---------------------------------------------------------


@pytest.fixture()
def small_limit_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[TestClient, Path]]:
    """Workbench client with a tiny configured upload limit (200 bytes)."""

    staged_dir = tmp_path / "staged"
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    monkeypatch.setenv("STAGED_UPLOADS_DIR", str(staged_dir))
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "200")
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client, staged_dir
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _upload_bytes(client: TestClient, db_session: Session, payload: bytes, name: str) -> object:
    campaign = create_campaign(db_session, name=f"Size limit {name}")
    db_session.commit()
    return client.post(
        "/imports/upload",
        data={"campaign_id": str(campaign.id)},
        files={"file": (name, payload, "text/csv")},
        follow_redirects=False,
    )


def _csv_of_size(size: int) -> bytes:
    base = (
        b"first_name,last_name,company_name,company_domain\nAda,Lovelace,Engines,engines.example\n"
    )
    assert size >= len(base), "test sizes must fit a parseable CSV"
    return base + b"x" * (size - len(base))


def test_upload_below_limit_is_staged(
    small_limit_client: tuple[TestClient, Path], db_session: Session
) -> None:
    client, _staged_dir = small_limit_client
    response = _upload_bytes(client, db_session, _csv_of_size(150), "below.csv")
    assert response.status_code == 303
    assert "/mapping" in response.headers["location"]


def test_upload_exactly_at_limit_is_staged(
    small_limit_client: tuple[TestClient, Path], db_session: Session
) -> None:
    client, _staged_dir = small_limit_client
    payload = _csv_of_size(200)
    assert len(payload) == 200
    response = _upload_bytes(client, db_session, payload, "at-limit.csv")
    assert response.status_code == 303
    assert "/mapping" in response.headers["location"]


def test_upload_above_limit_is_rejected_before_staging(
    small_limit_client: tuple[TestClient, Path], db_session: Session
) -> None:
    client, staged_dir = small_limit_client
    payload = _csv_of_size(201)
    assert len(payload) == 201
    response = _upload_bytes(client, db_session, payload, "too-big.csv")
    assert response.status_code == 303
    location = response.headers["location"]
    assert "err=" in location and "larger+than" in location.replace("%20", "+")
    # Nothing was staged for the rejected file: no bytes, no metadata.
    assert not staged_dir.exists() or not any(staged_dir.iterdir())


# --- Workbench local-only startup guard ------------------------------------------


def test_workbench_enabled_outside_local_refuses_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import WorkbenchConfigurationError

    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    try:
        with pytest.raises(WorkbenchConfigurationError, match="APP_ENV"):
            create_app()
    finally:
        get_settings.cache_clear()


def test_workbench_enabled_locally_mounts_ui(client: TestClient) -> None:
    # The `client` fixture is APP_ENV=local (default) + workbench enabled.
    assert client.get("/").status_code == 200
    assert client.get("/contacts").status_code == 200


def test_workbench_disabled_never_raises_regardless_of_env(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("FEATURES__WORKBENCH", raising=False)
    get_settings.cache_clear()
    try:
        app = create_app()  # no error: the guard only fires when the switch is on

        def _override() -> Iterator[Session]:
            yield db_session

        app.dependency_overrides[get_db] = _override
        with TestClient(app) as staging_client:
            assert staging_client.get("/").status_code == 404
            assert staging_client.get("/local-tools").status_code == 404
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


# --- Local tools guards -------------------------------------------------------------
# The Local Tools safeguards are independent of the startup guard and unchanged:
# route-level 404 outside APP_ENV=local (now unreachable anyway, since the whole
# workbench refuses to start outside local) plus the service-level environment
# and loopback-database refusals proven in tests/test_devtools.py.


def test_local_reset_requires_typed_confirmation(client: TestClient) -> None:
    refused = client.post("/local-tools/clear", data={"confirm": "nope"}, follow_redirects=False)
    assert refused.status_code == 303
    assert "err=" in refused.headers["location"]
