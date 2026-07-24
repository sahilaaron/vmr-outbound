"""Deterministic Phase 2 verification demo (local-only, synthetic data).

Seeds a dedicated demo campaign with synthetic contacts that exercise every
verification outcome and path, then runs the real generation + verification
pipeline against the local database so an operator (or a browser walkthrough) can
observe truthful status transitions. It uses only fictional example-domain data —
never real prospects — and never makes a live network call (the simulator maps
the synthetic addresses to the documented outcomes).

Run (local only):

    FEATURES__EMAIL_GENERATION=true FEATURES__MILLIONVERIFIER=true \
      python scripts/phase2_verification_demo.py

It is safe to re-run: the demo campaign and its contacts are rebuilt each time.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.email_evidence import ExactEmailVerification
from app.models.enums import CampaignStatus, ContactWorkflowState, EmailVerificationResult
from app.services.email.candidates import generate_candidates
from app.services.verification import service as verification_service
from sqlalchemy import delete, select

DEMO_CAMPAIGN = "Phase 2 Verification Demo"

# (first, last, domain, imported_email) — synthetic, fictional, example domains.
DEMO_CONTACTS = [
    # Generated-candidate path, becomes VALID (Successful).
    ("Jane", "Doe", "acme.example", None),
    # Imported exact address, VALID (Successful).
    ("Grace", "Hopper", "compilers.example", "grace.hopper@compilers.example"),
    # INVALID mailbox (Failure).
    ("Bounce", "Invalid", "acme.example", "invalid@acme.example"),
    # Catch-all domain (Warning).
    ("Cathy", "All", "catchall.example", "cathy@catchall.example"),
    # Unknown (Warning).
    ("Uma", "Unknown", "acme.example", "unknown@acme.example"),
    # Disposable (Warning).
    ("Dis", "Poseable", "mailinator.com", "dis@mailinator.com"),
    # Role-based valid address (Warning, not Successful).
    ("Info", "Desk", "acme.example", "info@acme.example"),
    # Provider transient error, exhausts retries -> stays uncertain (Warning).
    ("Serge", "Error", "acme.example", "servererror@acme.example"),
    # Insufficient credits (Warning).
    ("Nora", "Credits", "acme.example", "nocredits@acme.example"),
    # Compound/diacritic name, generated path.
    ("José", "de la Cruz", "acme.example", None),
    # Unrenderable name + no domain -> needs review (Pending/unverified).
    ("Аня", "Иванова", "", None),
]


def _rebuild_campaign(session) -> Campaign:  # type: ignore[no-untyped-def]
    existing = session.scalars(select(Campaign).where(Campaign.name == DEMO_CAMPAIGN)).first()
    if existing is not None:
        contact_ids = [
            cc.contact_id
            for cc in session.scalars(
                select(CampaignContact).where(CampaignContact.campaign_id == existing.id)
            ).all()
        ]
        if contact_ids:
            session.execute(
                delete(EmailCandidate).where(EmailCandidate.contact_id.in_(contact_ids))
            )
        session.execute(delete(CampaignContact).where(CampaignContact.campaign_id == existing.id))
        if contact_ids:
            session.execute(delete(Contact).where(Contact.id.in_(contact_ids)))
        session.delete(existing)
        session.flush()
    campaign = Campaign(name=DEMO_CAMPAIGN, status=CampaignStatus.ACTIVE)
    session.add(campaign)
    session.flush()
    return campaign


def main() -> int:
    settings = get_settings()
    if settings.app_env.lower() != "local":
        print("Refusing to run outside APP_ENV=local.", file=sys.stderr)
        return 2

    session = SessionLocal()
    try:
        campaign = _rebuild_campaign(session)
        contacts: list[Contact] = []
        for first, last, domain, email in DEMO_CONTACTS:
            contact = Contact(
                first_name=first,
                last_name=last,
                company_name=domain.split(".")[0].title() if domain else "Unknown",
                company_domain=domain,
                email=email,
                natural_key=f"{first.casefold()}|{last.casefold()}|{domain}:{uuid.uuid4()}",
            )
            session.add(contact)
            session.flush()
            session.add(
                CampaignContact(
                    campaign_id=campaign.id,
                    contact_id=contact.id,
                    state=ContactWorkflowState.IMPORTED,
                )
            )
            contacts.append(contact)
        session.flush()

        # Seed a fresh cached VALID result for a separate address to demonstrate
        # cache reuse without a call, and a stale result to demonstrate recheck.
        session.add(
            ExactEmailVerification(
                email="grace.hopper@compilers.example",
                result=EmailVerificationResult.VALID,
                provider="millionverifier",
                policy_version="ver-1",
                checked_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        session.flush()

        provider = verification_service.get_provider(settings)
        summary: dict[str, int] = {}
        for contact in contacts:
            gen = generate_candidates(session, contact)
            if gen.needs_review:
                summary["needs_review"] = summary.get("needs_review", 0) + 1
                continue
            outcome = verification_service.prepare_and_enqueue_contact(
                session, contact, settings=settings
            )
            if outcome.reused_evidence is not None:
                summary["cache_reuse"] = summary.get("cache_reuse", 0) + 1
        # Drain the queue.
        processed = verification_service.run_worker(
            session, provider=provider, settings=settings, max_jobs=1000
        )
        summary["jobs_processed"] = len(processed)
        session.commit()

        print(f"Demo campaign ready: {DEMO_CAMPAIGN} ({len(contacts)} synthetic contacts)")
        for key, value in sorted(summary.items()):
            print(f"  {key}: {value}")
        print("Open http://127.0.0.1:8000/contacts and /verification to inspect.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
