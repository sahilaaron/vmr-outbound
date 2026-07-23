"""Staged-upload lifecycle tests (preview -> confirm flow storage)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.services.imports import staging


def _create(tmp_path: Path, **overrides: object) -> staging.StagedUpload:
    defaults: dict[str, object] = {
        "filename": "contacts.csv",
        "campaign_id": "11111111-1111-1111-1111-111111111111",
        "content": b"first_name\nAda\n",
        "source_format": "csv",
    }
    defaults.update(overrides)
    return staging.create_staged_upload(tmp_path, **defaults)  # type: ignore[arg-type]


def test_create_load_roundtrip(tmp_path: Path) -> None:
    staged = _create(tmp_path, provenance={"source_name": "unit test"})
    loaded = staging.load_staged_upload(tmp_path, staged.id)
    assert loaded.filename == "contacts.csv"
    assert loaded.provenance["source_name"] == "unit test"
    assert staging.read_staged_content(tmp_path, staged.id) == b"first_name\nAda\n"


def test_update_persists_selection_mapping_and_confirmation(tmp_path: Path) -> None:
    staged = _create(tmp_path)
    staged.sheet_selection = [0, 2]
    staged.column_mapping = {"First": "first_name"}
    staged.confirmed_batch_id = "22222222-2222-2222-2222-222222222222"
    staging.update_staged_upload(tmp_path, staged)
    loaded = staging.load_staged_upload(tmp_path, staged.id)
    assert loaded.sheet_selection == [0, 2]
    assert loaded.column_mapping == {"First": "first_name"}
    assert loaded.confirmed_batch_id == "22222222-2222-2222-2222-222222222222"


def test_expired_upload_is_purged_and_unloadable(tmp_path: Path) -> None:
    staged = _create(tmp_path)
    staged.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    staging.update_staged_upload(tmp_path, staged)
    with pytest.raises(staging.StagedUploadNotFound, match="expired"):
        staging.load_staged_upload(tmp_path, staged.id)
    assert staging.list_staged_uploads(tmp_path) == []
    assert not (tmp_path / staged.id).exists()  # cleaned from disk


def test_confirmed_uploads_leave_the_pending_list(tmp_path: Path) -> None:
    staged = _create(tmp_path)
    assert len(staging.list_staged_uploads(tmp_path)) == 1
    staged.confirmed_batch_id = "33333333-3333-3333-3333-333333333333"
    staging.update_staged_upload(tmp_path, staged)
    assert staging.list_staged_uploads(tmp_path) == []


def test_delete_discards_upload(tmp_path: Path) -> None:
    staged = _create(tmp_path)
    staging.delete_staged_upload(tmp_path, staged.id)
    with pytest.raises(staging.StagedUploadNotFound):
        staging.load_staged_upload(tmp_path, staged.id)


def test_invalid_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(staging.StagedUploadNotFound):
        staging.load_staged_upload(tmp_path, "../escape")
