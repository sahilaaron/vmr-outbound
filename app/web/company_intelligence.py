"""Admin Workbench pages for Company Intelligence (CI-001).

A router of its own, mounted only when ``features.company_intelligence`` is on,
rather than more routes bolted onto ``app.web.routes``. Three reasons, in order:

* **Isolation.** This area must be able to arrive, change and leave without
  touching the Agent monitor, the Campaign screens or anything Personalization
  will later grow into. A separate module and a separate mount make that
  structural instead of a promise about discipline.
* **Gating.** The switch is default-off (FND-007). With a separate router, off
  means the paths do not exist — a 404, not a page explaining that a feature is
  disabled — and there is no code path from a disabled feature into a template.
* **Namespacing.** Everything lives under ``/admin/company-intelligence`` and
  ``/admin/companies/{id}/intelligence``, so no pattern here can shadow an
  existing workbench route and no future workbench route can shadow one of these.

Routes are thin adapters, per AGENTS.md: every rule lives in the service layer.
The pages read through :mod:`app.services.company_intelligence.read` — the same
typed model any other consumer gets — so a screen cannot show something the
contract does not expose.

**Producing is queued, never inline.** The "Run classification" button enqueues a
durable job and returns. A model call inside a request handler would block the
operator for a minute, time out behind a proxy, and leave a half-written version
if they hit stop.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.csrf import register_csrf, require_csrf
from app.core.config import get_settings
from app.models.company import Company
from app.models.company_intelligence import (
    CompanyIntelligenceBackfillRun,
    CompanyIntelligenceClassification,
    CompanyIntelligenceVersion,
)
from app.models.enums import (
    IntelligenceDecisionAction,
    IntelligenceDimension,
    IntelligenceJobStatus,
)
from app.services import identity
from app.services.company_intelligence import backfill as ci_backfill
from app.services.company_intelligence import inputs as ci_inputs
from app.services.company_intelligence import jobs as ci_jobs
from app.services.company_intelligence import read as ci_read
from app.services.company_intelligence import review as ci_review
from app.services.company_intelligence import taxonomy as ci_taxonomy
from app.services.company_intelligence.inputs import IntelligenceInputError
from app.services.company_intelligence.producer import POLICY_VERSION
from app.services.company_intelligence.runner import PRODUCER, PRODUCER_VERSION
from app.services.imports import display

# Every state-changing route on this router is refused unless the request
# carries the CSRF token bound to the caller's session. The check is declared
# once, here, rather than on ~100 individual handlers: a route added later is
# covered the moment it is registered. It is inert for safe methods and inert
# entirely when hosted authentication is disabled (local development).
router = APIRouter(prefix="/admin", include_in_schema=False, dependencies=[Depends(require_csrf)])

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# The shared spreadsheet-neutralization boundary, so every environment that
# can render imported text has it under the same name.
display.register_neutralize(templates.env)
register_csrf(templates.env)

PAGE_SIZE = 50
OPERATOR_ACTOR = "operator"


def _render(
    request: Request,
    db: Session,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    """Render with the shared shell context, same as the rest of the workbench."""

    settings = get_settings()
    try:
        open_reviews = identity.count_open_reviews(db)
    except Exception:
        open_reviews = 0
    shared: dict[str, Any] = {
        "app_env": settings.app_env,
        "dry_run": settings.dry_run,
        "features_enabled": settings.features.enabled(),
        "local_env": settings.app_env.lower() == "local",
        "database_ok": True,
        "open_reviews": open_reviews,
        "flash_ok": request.query_params.get("ok"),
        "flash_err": request.query_params.get("err"),
        "active_nav": "company-intelligence",
        "dimensions": list(IntelligenceDimension),
    }
    shared.update(context)
    return templates.TemplateResponse(
        request=request, name=template, context=shared, status_code=status_code
    )


def _redirect(path: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    """Redirect with a flash message, preserving any query the path already had.

    The naive version — append ``?ok=...`` — produced ``...?run=<id>?ok=...`` for
    every caller that redirected back to a selected record, which parses as a
    ``run`` value with the flash glued to the end of it. Splitting the existing
    query out and merging is the fix; the separator is chosen, not assumed.
    """

    base, _, existing = path.partition("?")
    params = {key: value for key, value in (("ok", ok), ("err", err)) if value}
    encoded = urlencode(params)
    query = "&".join(part for part in (existing, encoded) if part)
    return RedirectResponse(f"{base}?{query}" if query else base, status_code=303)


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


@router.get("/company-intelligence", response_class=HTMLResponse)
def company_intelligence_index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Every Company with produced intelligence, plus what is queued."""

    page = max(int(request.query_params.get("page") or 1), 1)
    state = (request.query_params.get("state") or "all").strip().lower()

    statement = (
        select(Company, CompanyIntelligenceVersion)
        .join(
            CompanyIntelligenceVersion,
            (CompanyIntelligenceVersion.company_id == Company.id)
            & CompanyIntelligenceVersion.is_current.is_(True),
            isouter=True,
        )
        .order_by(Company.name.asc(), Company.id.asc())
    )
    if state == "produced":
        statement = statement.where(CompanyIntelligenceVersion.id.is_not(None))
    elif state == "missing":
        statement = statement.where(CompanyIntelligenceVersion.id.is_(None))
    elif state == "unresolved":
        statement = statement.where(CompanyIntelligenceVersion.unresolved_count > 0)
    elif state == "conflicted":
        statement = statement.where(CompanyIntelligenceVersion.conflict_count > 0)

    total = int(db.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    rows = db.execute(statement.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()

    return _render(
        request,
        db,
        "company_intelligence/index.html",
        {
            "rows": [{"company": company, "version": version} for company, version in rows],
            "total": total,
            "page": page,
            "pages": max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1),
            "state": state,
            "queue": ci_jobs.queue_counts(db),
            "taxonomy_versions": ci_taxonomy.active_versions(db),
            "page_title": "Company Intelligence",
        },
    )


# --------------------------------------------------------------------------
# One company
# --------------------------------------------------------------------------


@router.get("/companies/{company_id}/intelligence", response_class=HTMLResponse)
def company_intelligence_detail(
    company_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    company = db.get(Company, company_id)
    if company is None:
        return _render(request, db, "not_found.html", {"page_title": "Not found"}, status_code=404)

    view = ci_read.get_company_intelligence(db, company_id=company_id)
    versions = list(
        db.scalars(
            select(CompanyIntelligenceVersion)
            .where(CompanyIntelligenceVersion.company_id == company_id)
            .order_by(CompanyIntelligenceVersion.version_number.desc())
        ).all()
    )
    # Why a company cannot be classified is worth showing on the page rather
    # than discovering when a queued job fails an hour later.
    blocked_reason: str | None = None
    try:
        ci_inputs.assemble(
            db,
            company=company,
            producer=PRODUCER,
            producer_version=PRODUCER_VERSION,
            policy_version=POLICY_VERSION,
        )
    except IntelligenceInputError as exc:
        blocked_reason = exc.message

    return _render(
        request,
        db,
        "company_intelligence/detail.html",
        {
            "company": company,
            "view": view,
            "versions": versions,
            "active_job": ci_jobs.active_job_for(db, company_id=company_id),
            "decisions": ci_review.decision_history(db, company_id=company_id),
            "blocked_reason": blocked_reason,
            "correction_terms": _correction_terms(db),
            "actions": list(IntelligenceDecisionAction),
            "page_title": f"Intelligence — {company.name}",
        },
    )


@router.get(
    "/companies/{company_id}/intelligence/versions/{version_id}",
    response_class=HTMLResponse,
)
def company_intelligence_version(
    company_id: uuid.UUID,
    version_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """One version exactly as produced, current or superseded.

    Reads the stored rows directly rather than the effective read model, on
    purpose: this page answers "what did the producer actually say", and folding
    operator decisions into it would make a superseded version look like it had
    been edited, which is precisely what never happens.
    """

    company = db.get(Company, company_id)
    version = db.get(CompanyIntelligenceVersion, version_id)
    if company is None or version is None or version.company_id != company_id:
        return _render(request, db, "not_found.html", {"page_title": "Not found"}, status_code=404)

    classifications = list(
        db.scalars(
            select(CompanyIntelligenceClassification)
            .where(CompanyIntelligenceClassification.intelligence_version_id == version_id)
            .order_by(
                CompanyIntelligenceClassification.dimension,
                CompanyIntelligenceClassification.rank,
            )
        ).all()
    )
    return _render(
        request,
        db,
        "company_intelligence/version.html",
        {
            "company": company,
            "version": version,
            "classifications": classifications,
            "page_title": f"Version {version.version_number} — {company.name}",
        },
    )


@router.post("/companies/{company_id}/intelligence/run")
def company_intelligence_run(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Queue one production job. Never produces inline."""

    path = f"/admin/companies/{company_id}/intelligence"
    company = db.get(Company, company_id)
    if company is None:
        return _redirect("/admin/company-intelligence", err="That company no longer exists.")

    try:
        source = ci_inputs.assemble(
            db,
            company=company,
            producer=PRODUCER,
            producer_version=PRODUCER_VERSION,
            policy_version=POLICY_VERSION,
        )
    except IntelligenceInputError as exc:
        return _redirect(path, err=exc.message)

    job, created = ci_jobs.enqueue(
        db,
        company=company,
        input_digest=source.digest,
        producer_version=PRODUCER_VERSION,
        policy_version=POLICY_VERSION,
        requested_by=OPERATOR_ACTOR,
    )
    db.commit()
    if created:
        return _redirect(path, ok="Queued. A worker will classify this company.")
    # "Already queued" was reported for every non-created outcome, including a
    # job that had already finished. Say which of the two this is: an operator
    # waiting for a queue that already emptied is the same wasted afternoon as
    # one waiting for a queue that never moves.
    if job.status is IntelligenceJobStatus.SUCCEEDED:
        return _redirect(path, ok=f"Already classified under this exact evidence (job {job.id}).")
    return _redirect(path, ok=f"Already queued (job {job.id}).")


@router.post("/companies/{company_id}/intelligence/decisions")
def company_intelligence_decide(
    company_id: uuid.UUID,
    dimension: str = Form(...),
    action: str = Form(...),
    target_key: str = Form(...),
    target_label: str = Form(""),
    classification_id: str = Form(""),
    corrected_term_id: str = Form(""),
    corrected_value: str = Form(""),
    set_primary: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Record one operator decision. Append-only; nothing produced is edited."""

    path = f"/admin/companies/{company_id}/intelligence"
    company = db.get(Company, company_id)
    if company is None:
        return _redirect("/admin/company-intelligence", err="That company no longer exists.")

    try:
        parsed_dimension = IntelligenceDimension(dimension)
        parsed_action = IntelligenceDecisionAction(action)
    except ValueError:
        return _redirect(path, err="Unrecognised dimension or action.")

    view = ci_read.get_company_intelligence(db, company_id=company_id)
    version = None
    if view is not None and view.current_version is not None:
        version = db.get(CompanyIntelligenceVersion, view.current_version.version_id)

    try:
        ci_review.record_decision(
            db,
            company=company,
            version=version,
            request=ci_review.DecisionRequest(
                dimension=parsed_dimension,
                action=parsed_action,
                target_key=target_key,
                target_label=target_label or None,
                classification_id=_uuid(classification_id),
                corrected_term_id=_uuid(corrected_term_id),
                corrected_value=corrected_value or None,
                set_primary=bool(set_primary),
                note=note.strip() or None,
            ),
            actor=OPERATOR_ACTOR,
        )
    except ci_review.IntelligenceReviewError as exc:
        db.rollback()
        return _redirect(path, err=str(exc))
    db.commit()
    return _redirect(path, ok=f"Recorded: {parsed_action.value.replace('_', ' ')}.")


@router.post("/companies/{company_id}/intelligence/aliases")
def company_intelligence_alias(
    company_id: uuid.UUID,
    dimension: str = Form(...),
    alias: str = Form(...),
    term_id: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Teach the active vocabulary that a written value means a canonical term."""

    path = f"/admin/companies/{company_id}/intelligence"
    try:
        parsed_dimension = IntelligenceDimension(dimension)
    except ValueError:
        return _redirect(path, err="Unrecognised dimension.")
    parsed_term = _uuid(term_id)
    if parsed_term is None:
        return _redirect(path, err="Choose a canonical value to map onto.")

    try:
        term = ci_review.map_alias(
            db,
            dimension=parsed_dimension,
            alias=alias,
            term_id=parsed_term,
            actor=OPERATOR_ACTOR,
        )
    except (ci_review.IntelligenceReviewError, ci_taxonomy.TaxonomyError) as exc:
        db.rollback()
        return _redirect(path, err=str(exc))
    db.commit()
    return _redirect(
        path,
        ok=(
            f"{alias!r} now maps to {term.canonical_label!r}. Stored versions are "
            "unchanged; the next run will use it."
        ),
    )


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


@router.get("/company-intelligence/taxonomy", response_class=HTMLResponse)
def company_intelligence_taxonomy(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Browse the active vocabularies, their terms and their aliases."""

    selected = (request.query_params.get("dimension") or "industry").strip().lower()
    try:
        dimension = IntelligenceDimension(selected)
    except ValueError:
        dimension = IntelligenceDimension.INDUSTRY

    edition = ci_taxonomy.active_taxonomy(db, dimension=dimension)
    terms = ci_taxonomy.list_terms(db, taxonomy=edition) if edition is not None else []
    aliases = {
        term.id: ci_taxonomy.list_aliases(db, term=term) for term in terms if term.depth == 0
    }
    return _render(
        request,
        db,
        "company_intelligence/taxonomy.html",
        {
            "dimension": dimension,
            "edition": edition,
            "terms": terms,
            "aliases": aliases,
            "vocabulary_dimensions": sorted(
                ci_taxonomy.NORMALIZING_DIMENSION, key=lambda item: item.value
            ),
            "page_title": "Intelligence vocabulary",
        },
    )


# --------------------------------------------------------------------------
# Backfill
# --------------------------------------------------------------------------


@router.get("/company-intelligence/backfill", response_class=HTMLResponse)
def company_intelligence_backfill(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    runs = ci_backfill.list_runs(db)
    selected_id = _uuid(request.query_params.get("run") or "")
    selected = db.get(CompanyIntelligenceBackfillRun, selected_id) if selected_id else None
    return _render(
        request,
        db,
        "company_intelligence/backfill.html",
        {
            "runs": runs,
            "selected": selected,
            "items": (
                ci_backfill.run_items(db, run_id=selected.id) if selected is not None else []
            ),
            "eligible_companies": ci_backfill.eligible_company_count(db),
            "default_batch_size": ci_backfill.DEFAULT_BATCH_SIZE,
            "page_title": "Intelligence backfill",
        },
    )


@router.post("/company-intelligence/backfill")
def company_intelligence_backfill_create(
    label: str = Form("Company Intelligence backfill"),
    mode: str = Form("preview"),
    batch_size: str = Form("25"),
    max_companies: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    path = "/admin/company-intelligence/backfill"
    try:
        run = ci_backfill.create_run(
            db,
            label=label,
            dry_run=mode != "live",
            batch_size=int(batch_size or ci_backfill.DEFAULT_BATCH_SIZE),
            max_companies=int(max_companies) if max_companies.strip() else None,
            created_by=OPERATOR_ACTOR,
        )
    except (ci_backfill.BackfillError, ValueError) as exc:
        db.rollback()
        return _redirect(path, err=str(exc))
    db.commit()
    return _redirect(
        f"{path}?run={run.id}",
        ok="Preview opened. Advance it a batch at a time."
        if run.dry_run
        else "Live run opened. Advance it a batch at a time.",
    )


@router.post("/company-intelligence/backfill/{run_id}/advance")
def company_intelligence_backfill_advance(
    run_id: uuid.UUID, db: Session = Depends(get_db)
) -> RedirectResponse:
    """Process exactly one bounded batch. One button press, one batch."""

    path = f"/admin/company-intelligence/backfill?run={run_id}"
    run = db.get(CompanyIntelligenceBackfillRun, run_id)
    if run is None:
        return _redirect("/admin/company-intelligence/backfill", err="That run no longer exists.")
    try:
        report = ci_backfill.advance(
            db,
            run=run,
            feature_enabled=get_settings().features.company_intelligence,
            actor=OPERATOR_ACTOR,
        )
    except ci_backfill.BackfillError as exc:
        db.rollback()
        return _redirect(path, err=str(exc))
    db.commit()
    return _redirect(
        path,
        ok=(
            f"{report.considered} considered, {report.enqueued} queued, "
            f"{report.skipped} skipped" + (" — run finished." if report.exhausted else ".")
        ),
    )


@router.post("/company-intelligence/backfill/{run_id}/pause")
def company_intelligence_backfill_pause(
    run_id: uuid.UUID, db: Session = Depends(get_db)
) -> RedirectResponse:
    return _run_command(db, run_id=run_id, command="pause")


@router.post("/company-intelligence/backfill/{run_id}/resume")
def company_intelligence_backfill_resume(
    run_id: uuid.UUID, db: Session = Depends(get_db)
) -> RedirectResponse:
    return _run_command(db, run_id=run_id, command="resume")


@router.post("/company-intelligence/backfill/{run_id}/cancel")
def company_intelligence_backfill_cancel(
    run_id: uuid.UUID, db: Session = Depends(get_db)
) -> RedirectResponse:
    return _run_command(db, run_id=run_id, command="cancel")


def _run_command(db: Session, *, run_id: uuid.UUID, command: str) -> RedirectResponse:
    path = f"/admin/company-intelligence/backfill?run={run_id}"
    run = db.get(CompanyIntelligenceBackfillRun, run_id)
    if run is None:
        return _redirect("/admin/company-intelligence/backfill", err="That run no longer exists.")
    try:
        if command == "pause":
            ci_backfill.pause(db, run=run, actor=OPERATOR_ACTOR)
        elif command == "resume":
            ci_backfill.resume(db, run=run, actor=OPERATOR_ACTOR)
        else:
            ci_backfill.cancel(
                db, run=run, reason="cancelled from the Admin surface", actor=OPERATOR_ACTOR
            )
    except ci_backfill.BackfillError as exc:
        db.rollback()
        return _redirect(path, err=str(exc))
    db.commit()
    return _redirect(path, ok=f"Run {command}d.")


def _correction_terms(db: Session) -> dict[str, list[Any]]:
    """Canonical values an operator may correct onto, per dimension.

    Only from the *active* edition, because a correction onto a retired term
    would put a value on screen that the next production run cannot reproduce.
    """

    out: dict[str, list[Any]] = {}
    for dimension in ci_taxonomy.NORMALIZING_DIMENSION:
        edition = ci_taxonomy.active_taxonomy(db, dimension=dimension)
        if edition is None:
            continue
        depth = 0 if dimension is IntelligenceDimension.INDUSTRY else None
        if dimension is IntelligenceDimension.SUBINDUSTRY:
            depth = 1
        out[dimension.value] = ci_taxonomy.list_terms(db, taxonomy=edition, depth=depth)
    return out
