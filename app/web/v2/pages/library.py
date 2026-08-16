"""Library: what VMR may say and offer.

Business profile, offerings, proof points, message rules (restricted claims)
and personas. Everyone on the team can read it; only an administrator can
change it — the edit forms render for administrators and every write route
lives under ``/app/admin/library``, which the auth policy withholds from
ordinary users.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.auth.context import current_operator
from app.core.config import get_settings
from app.models.enums import SellerClaimScope, SellerOfferingType
from app.services import drafts as draft_service
from app.services.campaign_access import actor_from_request
from app.services.seller import campaign_offerings as seller_campaign_offerings
from app.services.seller import profile as seller_profile
from app.services.seller import readiness as seller_readiness
from app.services.seller import records as seller_records
from app.services.seller.common import SellerKnowledgeError, parse_lines
from app.web.v2 import shell

router = shell.router

#: Local views, in order. The keys are the URL segments.
KB_SECTIONS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("company", "Business profile"),
    ("offerings", "Offerings"),
    ("proof-points", "Proof"),
    ("restricted-claims", "Message rules"),
    ("personas", "Personas"),
)

#: Readiness links point at the admin knowledge-base surface; the Library is
#: where the customer reads the same records.
_LEGACY_LINKS: dict[str, str] = {
    "/knowledge-base": "/app/library",
    "/knowledge-base/company": "/app/library/company",
    "/knowledge-base/offerings": "/app/library/offerings",
    "/knowledge-base/proof-points": "/app/library/proof-points",
    "/knowledge-base/restricted-claims": "/app/library/restricted-claims",
    "/knowledge-base/personas": "/app/library/personas",
}


def _library_link(link: str | None) -> str | None:
    if not link:
        return None
    if link.startswith("/campaigns/"):
        return "/app" + link + "/setup"
    return _LEGACY_LINKS.get(link, link)


def _actor() -> str:
    operator = current_operator()
    return operator.email if operator is not None else draft_service.OPERATOR_ACTOR


def _offering_type(
    raw: str | None, *, default: SellerOfferingType = SellerOfferingType.OTHER
) -> SellerOfferingType:
    try:
        return SellerOfferingType(str(raw))
    except ValueError:
        return default


def _claim_scope(
    raw: str | None, *, default: SellerClaimScope = SellerClaimScope.GLOBAL
) -> SellerClaimScope:
    try:
        return SellerClaimScope(str(raw))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get("/library")
@router.get("/library/{section}")
def library_page(
    request: Request,
    db: Session = Depends(get_db),
    section: str = "overview",
    offering: str | None = None,
    archived: str | None = None,
) -> HTMLResponse:
    """What we sell, what we may say about it, and who we say it to."""

    settings = get_settings()
    if not shell.kb_on(db, settings):
        return shell.render(
            request,
            db,
            "knowledge_disabled.html",
            {"active_nav": "library", "page_title": "Library"},
        )

    if section not in {key for key, _ in KB_SECTIONS}:
        section = "overview"
    show_archived = shell.checkbox(archived)

    counts = seller_records.counts(db)
    ctx: dict[str, Any] = {
        "active_nav": "library",
        "page_title": "Library",
        "kb_section": section,
        "kb_sections": KB_SECTIONS,
        "kb_counts": counts,
        "profile": seller_profile.get_profile(db),
        "show_archived": show_archived,
        "offering_types": list(SellerOfferingType),
        "claim_scopes": list(SellerClaimScope),
    }

    if section == "overview":
        report = seller_readiness.seller_report(db)
        ctx["report"] = report
        ctx["report_links"] = {item.key: _library_link(item.link) for item in report.items}
        ctx["draft_counts"] = draft_service.queue_counts(db)
    elif section == "offerings":
        offerings = seller_records.list_offerings(db, include_archived=True)
        chosen = shell.uuid_or_none(offering) if offering else None
        default = offerings[0] if offerings else None
        selected = next((o for o in offerings if o.id == chosen), default)
        ctx["offerings"] = offerings
        ctx["selected"] = selected
        if selected is not None:
            ctx["proof_points"] = seller_records.proof_points_for_offering(db, selected.id)
            ctx["claims"] = seller_records.restricted_claims_for_offering(db, selected.id)
            ctx["personas"] = seller_records.personas_for_offering(db, selected.id)
            ctx["campaigns"] = seller_campaign_offerings.campaigns_for_offering(
                db, selected.id, actor=actor_from_request(request)
            )
            linked_proof = {record.id for record in ctx["proof_points"]}
            linked_claims = {record.id for record in ctx["claims"]}
            linked_personas = {record.id for record in ctx["personas"]}
            ctx["available_proof_points"] = [
                r for r in seller_records.list_proof_points(db) if r.id not in linked_proof
            ]
            ctx["available_claims"] = [
                r
                for r in seller_records.list_restricted_claims(db)
                if r.id not in linked_claims and r.scope is SellerClaimScope.OFFERING
            ]
            ctx["available_personas"] = [
                r for r in seller_records.list_personas(db) if r.id not in linked_personas
            ]
    elif section == "proof-points":
        proof_points = seller_records.list_proof_points(db, include_archived=True)
        ctx["records"] = proof_points
        ctx["offerings_by_record"] = seller_records.offerings_by_record(
            db, kind="proof_point", record_ids=[record.id for record in proof_points]
        )
    elif section == "restricted-claims":
        claims = seller_records.list_restricted_claims(db, include_archived=True)
        ctx["records"] = claims
        ctx["offerings_by_record"] = seller_records.offerings_by_record(
            db, kind="restricted_claim", record_ids=[record.id for record in claims]
        )
    elif section == "personas":
        personas = seller_records.list_personas(db, include_archived=True)
        ctx["records"] = personas
        ctx["offerings_by_record"] = seller_records.offerings_by_record(
            db, kind="persona", record_ids=[record.id for record in personas]
        )

    return shell.render(request, db, f"kb_{section.replace('-', '_')}.html", ctx)


# ---------------------------------------------------------------------------
# Write — administrators only, under /app/admin/library
# ---------------------------------------------------------------------------


def _kb_guard(db: Session) -> Response | None:
    if not shell.kb_on(db, get_settings()):
        return shell.redirect(
            "/app/library", err="The Library is switched off in this environment."
        )
    return None


@router.post("/admin/library/company")
async def library_profile_save(request: Request, db: Session = Depends(get_db)) -> Response:
    refused = _kb_guard(db)
    if refused is not None:
        return refused
    form = await request.form()
    try:
        seller_profile.save_profile(
            db,
            name=str(form.get("name", "")),
            short_description=str(form.get("short_description", "")),
            description=str(form.get("description", "")),
            positioning=str(form.get("positioning", "")),
            communication_guidance=str(form.get("communication_guidance", "")),
            notes=str(form.get("notes", "")),
            industries_served=parse_lines(str(form.get("industries_served", ""))),
            geographies_served=parse_lines(str(form.get("geographies_served", ""))),
            capabilities=parse_lines(str(form.get("capabilities", ""))),
            differentiators=parse_lines(str(form.get("differentiators", ""))),
            updated_by=_actor(),
        )
    except SellerKnowledgeError as exc:
        db.rollback()
        return shell.redirect("/app/library/company", err=str(exc))
    db.commit()
    return shell.redirect("/app/library/company", ok="Business profile saved.")


@router.post("/admin/library/offerings")
async def library_offering_create(request: Request, db: Session = Depends(get_db)) -> Response:
    refused = _kb_guard(db)
    if refused is not None:
        return refused
    form = await request.form()
    try:
        offering = seller_records.create_offering(
            db,
            name=str(form.get("name", "")),
            offering_type=_offering_type(str(form.get("offering_type", ""))),
            short_description=str(form.get("short_description", "")),
            description=str(form.get("description", "")),
            problems_addressed=parse_lines(str(form.get("problems_addressed", ""))),
            use_cases=parse_lines(str(form.get("use_cases", ""))),
            differentiators=parse_lines(str(form.get("differentiators", ""))),
            notes=str(form.get("notes", "")),
            created_by=_actor(),
        )
    except SellerKnowledgeError as exc:
        db.rollback()
        return shell.redirect("/app/library/offerings", err=str(exc))
    db.commit()
    return shell.redirect(
        f"/app/library/offerings?offering={offering.id}", ok=f"Added “{offering.name}”."
    )


def _offering(db: Session, offering_id: str) -> Any | None:
    parsed = shell.uuid_or_none(offering_id)
    return seller_records.get_offering(db, parsed) if parsed else None


@router.post("/admin/library/offerings/{offering_id}")
async def library_offering_update(
    request: Request, offering_id: str, db: Session = Depends(get_db)
) -> Response:
    refused = _kb_guard(db)
    if refused is not None:
        return refused
    offering = _offering(db, offering_id)
    if offering is None:
        return shell.redirect("/app/library/offerings", err="That offering does not exist.")
    back = f"/app/library/offerings?offering={offering.id}"
    form = await request.form()
    try:
        seller_records.update_offering(
            db,
            offering,
            name=str(form.get("name", "")),
            offering_type=_offering_type(
                str(form.get("offering_type", "")), default=offering.offering_type
            ),
            short_description=str(form.get("short_description", "")),
            description=str(form.get("description", "")),
            problems_addressed=parse_lines(str(form.get("problems_addressed", ""))),
            use_cases=parse_lines(str(form.get("use_cases", ""))),
            differentiators=parse_lines(str(form.get("differentiators", ""))),
            notes=str(form.get("notes", "")),
            actor=_actor(),
        )
    except SellerKnowledgeError as exc:
        db.rollback()
        return shell.redirect(back, err=str(exc))
    db.commit()
    return shell.redirect(back, ok="Offering saved.")


@router.post("/admin/library/offerings/{offering_id}/state")
async def library_offering_state(
    request: Request, offering_id: str, db: Session = Depends(get_db)
) -> Response:
    refused = _kb_guard(db)
    if refused is not None:
        return refused
    offering = _offering(db, offering_id)
    if offering is None:
        return shell.redirect("/app/library/offerings", err="That offering does not exist.")
    back = f"/app/library/offerings?offering={offering.id}"
    form = await request.form()
    restore = str(form.get("action", "")) == "restore"
    try:
        if restore:
            changed = seller_records.restore_offering(db, offering, actor=_actor())
        else:
            changed = seller_records.archive_offering(db, offering, actor=_actor())
    except SellerKnowledgeError as exc:
        db.rollback()
        return shell.redirect(back, err=str(exc))
    db.commit()
    if not changed:
        return shell.redirect(back, ok="No change — it was already in that state.")
    if restore:
        return shell.redirect(back, ok=f"“{offering.name}” is active again.")
    return shell.redirect(
        back,
        ok=f"“{offering.name}” is archived. Campaigns that already name it still show it.",
    )


@router.post("/admin/library/offerings/{offering_id}/links")
async def library_offering_link(
    request: Request, offering_id: str, db: Session = Depends(get_db)
) -> Response:
    refused = _kb_guard(db)
    if refused is not None:
        return refused
    offering = _offering(db, offering_id)
    if offering is None:
        return shell.redirect("/app/library/offerings", err="That offering does not exist.")
    back = f"/app/library/offerings?offering={offering.id}"
    form = await request.form()
    kind = str(form.get("kind", ""))
    if kind not in ("proof_point", "restricted_claim", "persona"):
        return shell.redirect(back, err="Unknown association type.")
    related_id = shell.uuid_or_none(str(form.get("related_id", "")))
    if related_id is None:
        return shell.redirect(back, err="Select something to associate first.")
    remove = str(form.get("action", "")) == "remove"
    label = kind.replace("_", " ")
    try:
        if remove:
            changed = seller_records.unlink_from_offering(
                db, offering=offering, kind=kind, related_id=related_id, actor=_actor()
            )
        else:
            changed = seller_records.link_to_offering(
                db, offering=offering, kind=kind, related_id=related_id, actor=_actor()
            )
    except SellerKnowledgeError as exc:
        db.rollback()
        return shell.redirect(back, err=str(exc))
    db.commit()
    if not changed:
        return shell.redirect(back, ok=f"No change — that {label} was already as you asked.")
    return shell.redirect(back, ok=f"{'Removed' if remove else 'Added'} the {label}.")


@router.post("/admin/library/proof-points")
async def library_proof_create(request: Request, db: Session = Depends(get_db)) -> Response:
    refused = _kb_guard(db)
    if refused is not None:
        return refused
    form = await request.form()
    try:
        seller_records.create_proof_point(
            db,
            statement=str(form.get("statement", "")),
            supporting_detail=str(form.get("supporting_detail", "")),
            source_reference=str(form.get("source_reference", "")),
            created_by=_actor(),
        )
    except SellerKnowledgeError as exc:
        db.rollback()
        return shell.redirect("/app/library/proof-points", err=str(exc))
    db.commit()
    return shell.redirect("/app/library/proof-points", ok="Proof point added.")


@router.post("/admin/library/proof-points/{proof_point_id}")
async def library_proof_update(
    request: Request, proof_point_id: str, db: Session = Depends(get_db)
) -> Response:
    refused = _kb_guard(db)
    if refused is not None:
        return refused
    parsed = shell.uuid_or_none(proof_point_id)
    record = seller_records.get_proof_point(db, parsed) if parsed else None
    if record is None:
        return shell.redirect("/app/library/proof-points", err="That proof point does not exist.")
    form = await request.form()
    action = str(form.get("action", ""))
    try:
        if action in ("archive", "restore"):
            if action == "restore":
                seller_records.restore_proof_point(db, record, actor=_actor())
            else:
                seller_records.archive_proof_point(db, record, actor=_actor())
            message = "Proof point restored." if action == "restore" else "Proof point archived."
        else:
            seller_records.update_proof_point(
                db,
                record,
                statement=str(form.get("statement", "")),
                supporting_detail=str(form.get("supporting_detail", "")),
                source_reference=str(form.get("source_reference", "")),
                actor=_actor(),
            )
            message = "Proof point saved."
    except SellerKnowledgeError as exc:
        db.rollback()
        return shell.redirect("/app/library/proof-points", err=str(exc))
    db.commit()
    return shell.redirect("/app/library/proof-points", ok=message)


@router.post("/admin/library/restricted-claims")
async def library_claim_create(request: Request, db: Session = Depends(get_db)) -> Response:
    refused = _kb_guard(db)
    if refused is not None:
        return refused
    form = await request.form()
    try:
        seller_records.create_restricted_claim(
            db,
            title=str(form.get("title", "")),
            explanation=str(form.get("explanation", "")),
            examples=parse_lines(str(form.get("examples", ""))),
            scope=_claim_scope(str(form.get("scope", ""))),
            created_by=_actor(),
        )
    except SellerKnowledgeError as exc:
        db.rollback()
        return shell.redirect("/app/library/restricted-claims", err=str(exc))
    db.commit()
    return shell.redirect("/app/library/restricted-claims", ok="Message rule added.")


@router.post("/admin/library/restricted-claims/{claim_id}")
async def library_claim_update(
    request: Request, claim_id: str, db: Session = Depends(get_db)
) -> Response:
    refused = _kb_guard(db)
    if refused is not None:
        return refused
    parsed = shell.uuid_or_none(claim_id)
    record = seller_records.get_restricted_claim(db, parsed) if parsed else None
    if record is None:
        return shell.redirect("/app/library/restricted-claims", err="That rule does not exist.")
    form = await request.form()
    action = str(form.get("action", ""))
    try:
        if action in ("archive", "restore"):
            if action == "restore":
                seller_records.restore_restricted_claim(db, record, actor=_actor())
            else:
                seller_records.archive_restricted_claim(db, record, actor=_actor())
            message = "Message rule restored." if action == "restore" else "Message rule withdrawn."
        else:
            seller_records.update_restricted_claim(
                db,
                record,
                title=str(form.get("title", "")),
                explanation=str(form.get("explanation", "")),
                examples=parse_lines(str(form.get("examples", ""))),
                scope=_claim_scope(str(form.get("scope", "")), default=record.scope),
                actor=_actor(),
            )
            message = "Message rule saved."
    except SellerKnowledgeError as exc:
        db.rollback()
        return shell.redirect("/app/library/restricted-claims", err=str(exc))
    db.commit()
    return shell.redirect("/app/library/restricted-claims", ok=message)


@router.post("/admin/library/personas")
async def library_persona_create(request: Request, db: Session = Depends(get_db)) -> Response:
    refused = _kb_guard(db)
    if refused is not None:
        return refused
    form = await request.form()
    try:
        seller_records.create_persona(
            db,
            name=str(form.get("name", "")),
            role_function=str(form.get("role_function", "")),
            seniority=str(form.get("seniority", "")),
            responsibilities=parse_lines(str(form.get("responsibilities", ""))),
            challenges=parse_lines(str(form.get("challenges", ""))),
            use_cases=parse_lines(str(form.get("use_cases", ""))),
            messaging_notes=str(form.get("messaging_notes", "")),
            created_by=_actor(),
        )
    except SellerKnowledgeError as exc:
        db.rollback()
        return shell.redirect("/app/library/personas", err=str(exc))
    db.commit()
    return shell.redirect("/app/library/personas", ok="Persona added.")


@router.post("/admin/library/personas/{persona_id}")
async def library_persona_update(
    request: Request, persona_id: str, db: Session = Depends(get_db)
) -> Response:
    refused = _kb_guard(db)
    if refused is not None:
        return refused
    parsed = shell.uuid_or_none(persona_id)
    record = seller_records.get_persona(db, parsed) if parsed else None
    if record is None:
        return shell.redirect("/app/library/personas", err="That persona does not exist.")
    form = await request.form()
    action = str(form.get("action", ""))
    try:
        if action in ("archive", "restore"):
            if action == "restore":
                seller_records.restore_persona(db, record, actor=_actor())
            else:
                seller_records.archive_persona(db, record, actor=_actor())
            message = "Persona restored." if action == "restore" else "Persona archived."
        else:
            seller_records.update_persona(
                db,
                record,
                name=str(form.get("name", "")),
                role_function=str(form.get("role_function", "")),
                seniority=str(form.get("seniority", "")),
                responsibilities=parse_lines(str(form.get("responsibilities", ""))),
                challenges=parse_lines(str(form.get("challenges", ""))),
                use_cases=parse_lines(str(form.get("use_cases", ""))),
                messaging_notes=str(form.get("messaging_notes", "")),
                actor=_actor(),
            )
            message = "Persona saved."
    except SellerKnowledgeError as exc:
        db.rollback()
        return shell.redirect("/app/library/personas", err=str(exc))
    db.commit()
    return shell.redirect("/app/library/personas", ok=message)


# ---------------------------------------------------------------------------
# Legacy URLs
# ---------------------------------------------------------------------------


@router.get("/knowledge")
@router.get("/knowledge/{section}")
def knowledge_redirect(request: Request, section: str = "") -> RedirectResponse:
    query = f"?{request.url.query}" if request.url.query else ""
    suffix = f"/{section}" if section else ""
    return RedirectResponse(f"/app/library{suffix}{query}", status_code=308)


__all__ = ["router", "KB_SECTIONS"]
