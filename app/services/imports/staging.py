"""Short-lived staged uploads backing the preview -> confirm flow.

An uploaded file is held on local disk (never in the database) while the
operator inspects sheets, confirms the column mapping, and reviews the preview.
Nothing about a staged upload creates contacts, memberships, suppressions, or
import outcomes — persistence happens only at the explicit confirm step, which
hands the staged bytes to the committing importer.

Lifecycle and cleanup:

* Each staged upload lives in its own directory under the configured staging
  root (``var/staged_uploads`` by default) as the original bytes plus a JSON
  sidecar (filename, campaign, sheet selection, mapping, timestamps).
* A staged upload expires ``STAGED_UPLOAD_TTL_HOURS`` (24) after upload.
  Expired entries are removed opportunistically whenever the staging area is
  listed or read — there is no background job to operate.
* Confirming records the resulting batch id on the sidecar, so re-submitting
  the same confirmation returns the existing batch instead of importing twice
  (the importer's content-hash idempotency backs this up independently).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

STAGED_UPLOAD_TTL_HOURS = 24

_META_NAME = "meta.json"
_CONTENT_NAME = "content.bin"
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class StagedUploadNotFound(Exception):
    """Raised when a staged upload does not exist or has expired."""


class UploadTooLargeError(Exception):
    """Raised when an upload exceeds the configured maximum size.

    Enforced before any parsing or staging happens, so an oversized file never
    leaves bytes or metadata behind in the staging area.
    """

    def __init__(self, size_bytes: int, limit_bytes: int, filename: str | None = None) -> None:
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        name = f"“{filename}” " if filename else ""
        super().__init__(
            f"The file {name}is larger than the configured upload limit "
            f"({_format_size(limit_bytes)}). Split the spreadsheet into smaller "
            "files and import them one at a time."
        )


def _format_size(size_bytes: int) -> str:
    if size_bytes % (1024 * 1024) == 0:
        return f"{size_bytes // (1024 * 1024)} MB"
    return f"{size_bytes} bytes"


def enforce_upload_size(size_bytes: int, limit_bytes: int, *, filename: str | None = None) -> None:
    """Reject an upload larger than *limit_bytes* (a file AT the limit passes)."""

    if size_bytes > limit_bytes:
        raise UploadTooLargeError(size_bytes, limit_bytes, filename)


@dataclass
class StagedUpload:
    """One staged upload awaiting inspection, mapping, preview, or confirm."""

    id: str
    filename: str
    campaign_id: str
    uploaded_at: datetime
    expires_at: datetime
    source_format: str
    size_bytes: int
    sheet_selection: list[int] | None = None
    column_mapping: dict[str, str] | None = None
    confirmed_batch_id: str | None = None
    provenance: dict[str, str | None] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at


def _root(staging_dir: str | Path) -> Path:
    root = Path(staging_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _meta_to_dict(staged: StagedUpload) -> dict[str, object]:
    return {
        "id": staged.id,
        "filename": staged.filename,
        "campaign_id": staged.campaign_id,
        "uploaded_at": staged.uploaded_at.isoformat(),
        "expires_at": staged.expires_at.isoformat(),
        "source_format": staged.source_format,
        "size_bytes": staged.size_bytes,
        "sheet_selection": staged.sheet_selection,
        "column_mapping": staged.column_mapping,
        "confirmed_batch_id": staged.confirmed_batch_id,
        "provenance": staged.provenance,
    }


def _meta_from_dict(data: dict[str, Any]) -> StagedUpload:
    sheet_selection = data.get("sheet_selection")
    column_mapping = data.get("column_mapping")
    provenance = data.get("provenance") or {}
    return StagedUpload(
        id=str(data["id"]),
        filename=str(data["filename"]),
        campaign_id=str(data["campaign_id"]),
        uploaded_at=datetime.fromisoformat(str(data["uploaded_at"])),
        expires_at=datetime.fromisoformat(str(data["expires_at"])),
        source_format=str(data["source_format"]),
        size_bytes=int(data["size_bytes"]),
        sheet_selection=[int(v) for v in sheet_selection] if sheet_selection else None,
        column_mapping=(
            {str(k): str(v) for k, v in column_mapping.items()} if column_mapping else None
        ),
        confirmed_batch_id=(
            str(data["confirmed_batch_id"]) if data.get("confirmed_batch_id") else None
        ),
        provenance={str(k): (str(v) if v is not None else None) for k, v in provenance.items()},
    )


def _write_meta(directory: Path, staged: StagedUpload) -> None:
    (directory / _META_NAME).write_text(
        json.dumps(_meta_to_dict(staged), indent=2), encoding="utf-8"
    )


def _remove(directory: Path) -> None:
    for child in directory.iterdir():
        child.unlink(missing_ok=True)
    directory.rmdir()


def purge_expired(staging_dir: str | Path) -> int:
    """Remove expired or unreadable staged uploads. Returns how many were removed."""

    removed = 0
    root = _root(staging_dir)
    for directory in root.iterdir():
        if not directory.is_dir() or not _ID_RE.match(directory.name):
            continue
        meta_path = directory / _META_NAME
        try:
            staged = _meta_from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
            expired = staged.is_expired
        except (OSError, ValueError, KeyError):
            expired = True  # unreadable sidecar: treat as garbage and clean up
        if expired:
            _remove(directory)
            removed += 1
    return removed


def create_staged_upload(
    staging_dir: str | Path,
    *,
    filename: str,
    campaign_id: str,
    content: bytes,
    source_format: str,
    provenance: dict[str, str | None] | None = None,
) -> StagedUpload:
    """Persist an upload to the staging area and return its record."""

    purge_expired(staging_dir)
    staged_id = uuid.uuid4().hex
    now = datetime.now(UTC)
    staged = StagedUpload(
        id=staged_id,
        filename=filename,
        campaign_id=campaign_id,
        uploaded_at=now,
        expires_at=now + timedelta(hours=STAGED_UPLOAD_TTL_HOURS),
        source_format=source_format,
        size_bytes=len(content),
        provenance=provenance or {},
    )
    directory = _root(staging_dir) / staged_id
    directory.mkdir()
    (directory / _CONTENT_NAME).write_bytes(content)
    _write_meta(directory, staged)
    return staged


def load_staged_upload(staging_dir: str | Path, staged_id: str) -> StagedUpload:
    """Load one staged upload; expired or missing entries raise."""

    if not _ID_RE.match(staged_id):
        raise StagedUploadNotFound("staged upload id is not valid")
    directory = _root(staging_dir) / staged_id
    meta_path = directory / _META_NAME
    if not meta_path.is_file():
        raise StagedUploadNotFound("staged upload not found (it may have expired)")
    try:
        staged = _meta_from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError) as exc:
        raise StagedUploadNotFound("staged upload metadata is unreadable") from exc
    if staged.is_expired:
        _remove(directory)
        raise StagedUploadNotFound("staged upload has expired; upload the file again")
    return staged


def read_staged_content(staging_dir: str | Path, staged_id: str) -> bytes:
    """Return the original uploaded bytes for a staged upload."""

    load_staged_upload(staging_dir, staged_id)  # existence + expiry check
    return (Path(staging_dir) / staged_id / _CONTENT_NAME).read_bytes()


def update_staged_upload(staging_dir: str | Path, staged: StagedUpload) -> None:
    """Persist updated selection/mapping/confirmation state for a staged upload."""

    directory = _root(staging_dir) / staged.id
    if not (directory / _META_NAME).is_file():
        raise StagedUploadNotFound("staged upload not found (it may have expired)")
    _write_meta(directory, staged)


def list_staged_uploads(staging_dir: str | Path) -> list[StagedUpload]:
    """List current (unexpired, unconfirmed) staged uploads, newest first."""

    purge_expired(staging_dir)
    root = _root(staging_dir)
    found: list[StagedUpload] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not _ID_RE.match(directory.name):
            continue
        try:
            staged = _meta_from_dict(
                json.loads((directory / _META_NAME).read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, KeyError):
            continue
        if staged.confirmed_batch_id is None:
            found.append(staged)
    found.sort(key=lambda s: s.uploaded_at, reverse=True)
    return found


def delete_staged_upload(staging_dir: str | Path, staged_id: str) -> None:
    """Discard a staged upload (operator cancelled the flow)."""

    if not _ID_RE.match(staged_id):
        raise StagedUploadNotFound("staged upload id is not valid")
    directory = _root(staging_dir) / staged_id
    if directory.is_dir():
        _remove(directory)
