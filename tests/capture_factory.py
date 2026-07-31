"""Captures shaped the way the Chrome extension actually sends them.

Shared because the shape is the substance of several bugs. A Sales Navigator
search-results capture carries **no experience rows at all** — a results row shows
a person's current title and company but no employment history — so the employer
arrives in ``payload["current_employment_hint"]`` instead. Code that reads the
``experiences`` relationship directly sees an empty list and concludes no employer
was captured, which is how one page came to show a company name while another
showed a dash for the same person.

A factory that builds the realistic payload makes that class of bug reproducible
in a test rather than only in production.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.models.linkedin_profile import LinkedInProfileSnapshot
from sqlalchemy.orm import Session

SALESNAV_SCHEMA = "linkedin-contact-capture/2.1.0"
SALESNAV_MODE = "salesnav_people_search"


def salesnav_capture(
    session: Session,
    *,
    company_name: str | None = "QuantHealth",
    location: str | None = "Tel Aviv, Israel",
    full_name: str = "Dana Whitfield",
    first_name: str = "Dana",
    last_name: str = "Whitfield",
    title: str = "Head of Operations",
    member_id: str | None = None,
    with_alias: bool = True,
    **kwargs: Any,
) -> LinkedInProfileSnapshot:
    """One saved person from a Sales Navigator results page.

    Deliberately built with ``experience_observations: []`` and the employer only
    in the hint, because that is what the extension sends and what the bugs
    depended on. ``normalized_profile_url`` is left null for the same reason: a
    results row exposes no public profile URL, only a member id from which the
    alias is derived.
    """

    member = member_id or uuid.uuid4().hex[:12]
    hint: dict[str, Any] = {
        "company_name": company_name,
        "company_linkedin_url": None,
        "company_linkedin_id": None,
        "title": title,
        "role_location": None,
    }
    payload: dict[str, Any] = {
        "person": {
            "linkedin_profile_url": None,
            "salesnav_lead_url": f"https://www.linkedin.com/sales/lead/{member}",
            "salesnav_member_id": member,
            "salesnav_alias_url": (f"https://www.linkedin.com/in/{member}" if with_alias else None),
            "full_name": full_name,
            "location": location,
        },
        "current_employment_hint": hint,
        "experience_observations": [],
    }
    snapshot = LinkedInProfileSnapshot(
        client_capture_id=f"cap-{uuid.uuid4()}",
        content_hash=str(uuid.uuid4()),
        schema_version=SALESNAV_SCHEMA,
        capture_mode=SALESNAV_MODE,
        source="test",
        extraction_status="partial",
        payload=payload,
        profile_fields={
            "full_name": full_name,
            "first_name": first_name,
            "last_name": last_name,
            "headline": title,
            "displayed_location": location,
        },
        salesnav_lead_url=f"https://www.linkedin.com/sales/lead/{member}",
        salesnav_member_id=member,
        salesnav_alias_url=(f"https://www.linkedin.com/in/{member}" if with_alias else None),
        **kwargs,
    )
    session.add(snapshot)
    session.flush()
    return snapshot
