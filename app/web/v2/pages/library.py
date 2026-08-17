"""Library: what VMR may say and offer.

Business profile, offerings, proof points, restricted claims and personas.
Read-only here; the forms live on the admin knowledge-base surface until the
Library slice moves them.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.services import drafts as draft_service
from app.services.campaign_access import actor_from_request
from app.services.seller import campaign_offerings as seller_campaign_offerings
from app.services.seller import profile as seller_profile
from app.services.seller import readiness as seller_readiness
from app.services.seller import records as seller_records
from app.web.v2 import shell

router = shell.router

KB_SECTIONS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("company", "Business profile"),
    ("offerings", "Offerings"),
    ("proof-points", "Proof points"),
    ("restricted-claims", "Restricted claims"),
    ("personas", "Personas"),
)


@router.get("/library")
@router.get("/library/{section}")
def knowledge_page(
    request: Request,
    db: Session = Depends(get_db),
    section: str = "overview",
    offering: str | None = None,
) -> HTMLResponse:
    """What we sell, what we may say about it, and who we say it to.

    Read-only here. Every one of these records is the authorisation for a claim in a
    draft, so editing stays on the admin surface where the create/update/archive
    forms and their validation already live — a second set of forms would be a
    second place for the rules to drift.
    """

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

    counts = seller_records.counts(db)
    ctx: dict[str, Any] = {
        "active_nav": "library",
        "page_title": "Library",
        "kb_section": section,
        "kb_sections": KB_SECTIONS,
        "kb_counts": counts,
        "profile": seller_profile.get_profile(db),
    }

    if section == "overview":
        ctx["report"] = seller_readiness.seller_report(db)
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


@router.get("/knowledge")
@router.get("/knowledge/{section}")
def knowledge_redirect(request: Request, section: str = "") -> RedirectResponse:
    query = f"?{request.url.query}" if request.url.query else ""
    suffix = f"/{section}" if section else ""
    return RedirectResponse(f"/app/library{suffix}{query}", status_code=308)


__all__ = ["router"]
