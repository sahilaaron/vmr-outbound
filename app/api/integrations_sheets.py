"""The three HTTP endpoints the Google Sheets add-on calls, and nothing else.

Three routes, all under ``/integrations/sheets``, all authenticated by the same
router-level dependency, all refusing outright when the feature switch is off.

* ``GET  /integrations/sheets/campaigns`` — which Campaigns this account may use,
  plus the limits the add-on must respect. The add-on calls it once per sidebar
  open and never guesses a Campaign id.
* ``POST /integrations/sheets/batches`` — submit rows. Returns quickly with one
  identifier per row; it does not wait for the pipeline.
* ``POST /integrations/sheets/results`` — read current state for a bounded list
  of identifiers the add-on already holds.

Why the results endpoint is a POST that takes a list
----------------------------------------------------

A ``GET /batches/{id}`` needs a batch row to look the id up in, and a batch table
is schema this integration does not otherwise need. The add-on already stores one
identifier per row — it has to, in order to write a result back to the right row
after the sheet has been sorted — so asking about exactly those identifiers is
both the smallest server and the more precise question. It also refreshes a
partially-completed sheet without re-reading rows that are already finished. The
list is bounded by configuration and an oversized list is refused whole.

Why the router is not under ``/api``
------------------------------------

``/api`` is administrator-only by policy (``app/core/auth/policy.py``). This
surface is for ordinary accounts acting on their own Campaigns, so it is mounted
on its own top-level prefix and classified separately rather than carving an
exception into a rule whose whole value is that it has none.

Where the guard actually is
---------------------------

``require_account`` is declared on the **router**, so it covers every route
mounted here now and later. Handlers that need the account declare it again to
receive the value; FastAPI resolves a dependency once per request, so that is the
same single verification, not a second one.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.sheets_assertion import (
    AssertionVerifier,
    IdentityAssertionError,
    bearer_token,
)
from app.core.config import Settings, get_settings
from app.models.user import User
from app.services import campaign_access
from app.services.integrations.sheets import identity as sheet_identity
from app.services.integrations.sheets import results as sheet_results
from app.services.integrations.sheets import submit as sheet_submit
from app.services.integrations.sheets.contract import (
    SCHEMA_VERSION,
    BatchContractError,
    RejectedRow,
    RowStatus,
    SheetLocation,
    SubmittedRow,
    batch_id,
    normalize_client_id,
    parse_rows,
)
from app.services.operations import settings as operational

DbSession = Annotated[Session, Depends(get_db)]

#: The maximum submission generation the add-on may declare. A generation is how
#: an operator deliberately asks for the same row to be prepared again — it
#: changes every derived key — and bounding it keeps the value a small integer
#: the add-on increments rather than an arbitrary string it invents.
MAX_GENERATION = 1000


def _settings(request: Request) -> Settings:
    configured = getattr(request.app.state, "settings", None)
    return configured if isinstance(configured, Settings) else get_settings()


def _require_enabled(request: Request, db: Session) -> Settings:
    """404 when the integration is switched off, before any credential is read.

    The order matters. Reading the credential first and then refusing would make
    a disabled deployment distinguishable from an enabled one by how it rejects a
    bad token, and would fetch Google's key set for a surface that is not in use.

    Read through ``operations.settings.enabled`` rather than off the environment,
    so the administrator's own switch — and the capability gate behind it — is
    what decides. An operator turning this off in the Admin screen must actually
    turn it off, not toggle a control the routes ignore.
    """

    settings = _settings(request)
    if not operational.enabled(db, "google_sheets_integration", settings):
        raise HTTPException(status_code=404, detail="not found")
    return settings


def _verifier(request: Request) -> AssertionVerifier:
    verifier: AssertionVerifier | None = getattr(
        request.app.state, "sheets_assertion_verifier", None
    )
    if verifier is None:  # pragma: no cover - wired unconditionally in create_app
        raise HTTPException(status_code=503, detail="integration_unavailable")
    return verifier


async def require_account(request: Request, db: DbSession) -> User:
    """Resolve the add-on's Google assertion to an active VMR account, or refuse.

    One refusal shape for every cause. A missing header, a wrong scheme, an
    expired or forged token, a token minted for another application, an unknown
    Google identity and a disabled account all produce ``401`` with the same
    body. Distinguishing them would tell an unauthenticated caller which accounts
    exist and which deployments accept which clients.
    """

    _require_enabled(request, db)
    try:
        token = bearer_token(request.headers.get("authorization"))
        assertion = await _verifier(request).verify(token)
    except IdentityAssertionError as exc:
        raise HTTPException(status_code=401, detail="unauthorized") from exc
    try:
        user = sheet_identity.resolve_account(db, assertion)
    except sheet_identity.IntegrationAccountError as exc:
        raise HTTPException(status_code=401, detail="unauthorized") from exc
    # Recorded for the access log and for any later authorization decision that
    # reads the request scope. It is not a session and creates none.
    request.scope.setdefault("state", {})["sheets_account_id"] = str(user.id)
    return user


AuthenticatedAccount = Annotated[User, Depends(require_account)]

router = APIRouter(
    prefix="/integrations/sheets",
    tags=["integrations"],
    include_in_schema=False,
    dependencies=[Depends(require_account)],
)


@router.get("/campaigns")
def list_campaigns(
    request: Request, db: DbSession, account: AuthenticatedAccount
) -> dict[str, Any]:
    """The Campaigns this account may submit into, and the request limits."""

    settings = _settings(request)
    actor = sheet_identity.actor_for(account)
    campaigns = campaign_access.visible_campaigns(db, actor)
    return {
        "schema_version": SCHEMA_VERSION,
        "account": {"email": account.email_normalized, "display_name": account.display_name},
        "limits": {
            "max_batch_rows": settings.sheets.max_batch_rows,
            "max_result_ids": settings.sheets.max_result_ids,
            "max_context_chars": settings.sheets.max_context_chars,
        },
        "campaigns": [
            {
                "id": str(campaign.id),
                "name": campaign.name,
                "status": campaign.status.value,
                "execution_enabled": campaign.execution_enabled,
            }
            for campaign in campaigns
        ],
    }


@router.post("/batches")
def create_batch(
    request: Request, db: DbSession, account: AuthenticatedAccount, payload: dict[str, Any]
) -> dict[str, Any]:
    """Accept one batch of prospect rows into one Campaign."""

    settings = _settings(request)
    actor = sheet_identity.actor_for(account)
    try:
        campaign_id = _campaign_id(payload)
        location = _location(payload)
        generation = _generation(payload)
        parsed = _rows(payload, settings=settings)
    except BatchContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    campaign = sheet_submit.require_campaign(db, campaign_id=campaign_id, actor=actor)
    reference = batch_id(location, campaign_id=str(campaign.id), generation=generation)
    submission = sheet_submit.submit_rows(
        db,
        campaign=campaign,
        location=location,
        rows=[item for item in parsed if isinstance(item, SubmittedRow)],
        generation=generation,
        batch_reference=reference,
        actor=sheet_identity.actor_label(account),
    )

    # Rebuilt in the order the sheet sent, with the rows the contract refused
    # folded back into place. The add-on writes results by identifier, but an
    # operator reading the response should still see their own row order.
    accepted = {row.client_row_id: row for row in submission.rows}
    entries: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, RejectedRow):
            entries.append(
                {
                    "client_row_id": item.client_row_id,
                    "status": RowStatus.COULD_NOT_PREPARE.value,
                    "submission_id": None,
                    "contact_id": None,
                    "already_submitted": False,
                    "safe_failure_reason": item.reason,
                    "failure_code": item.code,
                }
            )
            continue
        row = accepted[item.client_row_id]
        entries.append(
            {
                "client_row_id": row.client_row_id,
                "status": row.status.value,
                "submission_id": str(row.submission_id) if row.submission_id else None,
                "contact_id": str(row.contact_id) if row.contact_id else None,
                "already_submitted": row.already_submitted,
                "safe_failure_reason": row.safe_failure_reason,
                "failure_code": row.failure_code,
            }
        )
    refused = sum(1 for entry in entries if entry["status"] == RowStatus.COULD_NOT_PREPARE.value)
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": submission.batch_id,
        "campaign_id": str(campaign.id),
        "counts": {
            "submitted": len(entries),
            "accepted": len(entries) - refused,
            "could_not_prepare": refused,
        },
        "rows": entries,
    }


@router.post("/results")
def read_results(
    request: Request, db: DbSession, account: AuthenticatedAccount, payload: dict[str, Any]
) -> dict[str, Any]:
    """Current state for a bounded list of submission identifiers."""

    settings = _settings(request)
    actor = sheet_identity.actor_for(account)
    try:
        submission_ids = _submission_ids(payload, limit=settings.sheets.max_result_ids)
    except BatchContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    results = sheet_results.results_for(db, submission_ids=submission_ids, actor=actor)
    return {
        "schema_version": SCHEMA_VERSION,
        "rows": [_result_json(result) for result in results],
    }


def _result_json(result: sheet_results.RowResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "submission_id": str(result.submission_id),
        "status": result.status.value,
        "email_address": result.email_address,
        "updated_at": result.updated_at.isoformat() if result.updated_at else None,
    }
    if result.status is RowStatus.READY:
        payload["messages"] = [
            {
                "sequence_index": message.sequence_index,
                "elapsed_day": message.elapsed_day,
                "subject": message.subject,
                "body": message.body,
            }
            for message in result.messages
        ]
    if result.safe_failure_reason:
        payload["safe_failure_reason"] = result.safe_failure_reason
    if result.note:
        payload["note"] = result.note
    return payload


# --- request parsing ---------------------------------------------------------


def _campaign_id(payload: dict[str, Any]) -> uuid.UUID:
    raw = payload.get("campaign_id")
    if not isinstance(raw, str) or not raw:
        raise BatchContractError("campaign_id is required", code="missing_campaign")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise BatchContractError(
            "campaign_id is not a valid identifier", code="bad_campaign"
        ) from exc


def _location(payload: dict[str, Any]) -> SheetLocation:
    return SheetLocation(
        installation_id=normalize_client_id(
            payload.get("installation_id"), field="installation_id"
        ),
        spreadsheet_id=normalize_client_id(payload.get("spreadsheet_id"), field="spreadsheet_id"),
        sheet_id=normalize_client_id(payload.get("sheet_id"), field="sheet_id"),
    )


def _generation(payload: dict[str, Any]) -> int:
    raw = payload.get("generation", 1)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise BatchContractError("generation must be a whole number", code="bad_generation")
    if raw < 1 or raw > MAX_GENERATION:
        raise BatchContractError(
            f"generation must be between 1 and {MAX_GENERATION}", code="bad_generation"
        )
    return raw


def _rows(payload: dict[str, Any], *, settings: Settings) -> list[SubmittedRow | RejectedRow]:
    raw = payload.get("rows")
    if not isinstance(raw, list) or not raw:
        raise BatchContractError("rows must be a non-empty list", code="no_rows")
    ceiling = settings.sheets.max_batch_rows
    if len(raw) > ceiling:
        # Refused whole, with the number stated. Silently processing a prefix
        # would look like success and leave the operator to discover the missing
        # rows themselves.
        raise BatchContractError(
            f"this request carries {len(raw)} rows; the maximum is {ceiling}",
            code="batch_too_large",
        )
    return parse_rows(raw, max_context_chars=settings.sheets.max_context_chars)


def _submission_ids(payload: dict[str, Any], *, limit: int) -> list[uuid.UUID]:
    raw = payload.get("submission_ids")
    if not isinstance(raw, list) or not raw:
        raise BatchContractError(
            "submission_ids must be a non-empty list", code="no_submission_ids"
        )
    if len(raw) > limit:
        raise BatchContractError(
            f"this request asks about {len(raw)} rows; the maximum is {limit}",
            code="too_many_submission_ids",
        )
    identifiers: list[uuid.UUID] = []
    for item in raw:
        if not isinstance(item, str):
            raise BatchContractError(
                "every submission identifier must be a string", code="bad_submission_id"
            )
        try:
            identifiers.append(uuid.UUID(item))
        except ValueError as exc:
            raise BatchContractError(
                "one of the submission identifiers is not valid", code="bad_submission_id"
            ) from exc
    return identifiers


__all__ = ["require_account", "router"]
