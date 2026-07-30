"""Finish the captures an intake request did not have time to resolve.

Automatic company-domain resolution belongs at intake conceptually — a saved person
should become a Contact without anyone pressing a button — but it cannot *all*
happen there. Each unresolved company may cost one provider lookup, and a
hundred-capture submission would spend a hundred of them inside one HTTP request.

So intake resolves what it can inside a hard, bounded share of its budget and
leaves the rest untouched. This is where the rest get finished: in the agent
worker, which already exists, already runs in its own window, and is not bounded
by a request.

Untouched matters. A capture the intake pass skipped has no decision recorded
against it, so nothing here is re-deciding or overriding anything — it is the first
evaluation that capture has had.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.services.resolution import service as resolution_service

ACTOR = "resolution-backfill"


@dataclass(frozen=True)
class BackfillResult:
    """What one pass over the pending captures achieved."""

    considered: int = 0
    promoted: int = 0
    provider_calls: int = 0
    failed: int = 0

    @property
    def did_work(self) -> bool:
        return bool(self.considered)


def _provider_access(settings: Settings) -> resolution_service.ProviderAccess:
    usable = settings.features.salesnav_domain_enrichment and settings.has_logo_dev_key()
    return resolution_service.ProviderAccess(
        api_key=settings.logo_dev_api_key if usable else None,
        search_url=settings.logo_dev_search_url,
        timeout=settings.logo_dev_timeout_seconds,
        max_candidates=settings.logo_dev_max_candidates,
    )


def pending_capture_ids(session: Session, *, limit: int = 50) -> list[uuid.UUID]:
    """Captures with no permanent Contact and no resolution decision at all.

    Deliberately excludes captures that already carry a decision, including an
    UNRESOLVED one. A recorded UNRESOLVED means the policy looked and could not
    conclude — re-running it without new evidence would reach the same answer, and
    re-running it *with* ``force`` would overwrite an operator's correction. Those
    captures belong to the operator, not to a background pass.
    """

    decided = select(CompanyDomainResolution.capture_id).where(
        CompanyDomainResolution.is_current.is_(True)
    )
    rows = session.scalars(
        select(LinkedInProfileSnapshot.id)
        .where(
            LinkedInProfileSnapshot.matched_contact_id.is_(None),
            LinkedInProfileSnapshot.id.not_in(decided),
        )
        .order_by(LinkedInProfileSnapshot.ingested_at.asc())
        .limit(limit)
    ).all()
    return list(rows)


def resolve_pending(
    session: Session,
    *,
    limit: int = 50,
    settings: Settings | None = None,
    actor: str = ACTOR,
) -> BackfillResult:
    """Resolve up to *limit* pending captures. The caller owns the commit.

    Each capture is isolated in its own SAVEPOINT, so one provider failure or one
    ambiguous company cannot cost the others. Returns counts rather than raising:
    a pass that could do nothing is a normal outcome, not an error.
    """

    settings = settings or get_settings()
    if not (
        settings.features.contact_capture_promotion
        and settings.features.automatic_company_domain_resolution
    ):
        return BackfillResult()

    access = _provider_access(settings)
    # Same rule as intake: without a provider the policy could only conclude "the
    # lookup was not run", and recording that non-decision would permanently stop
    # the capture resolving automatically later.
    if not access.available:
        return BackfillResult()

    considered = 0
    promoted = 0
    provider_calls = 0
    failed = 0

    for capture_id in pending_capture_ids(session, limit=limit):
        snapshot = session.get(LinkedInProfileSnapshot, capture_id)
        if snapshot is None:  # pragma: no cover - protected by the query
            continue
        considered += 1
        try:
            with session.begin_nested():
                outcome = resolution_service.resolve(
                    session,
                    snapshot=snapshot,
                    access=access,
                    actor=actor,
                    force=False,
                )
        except Exception:  # noqa: BLE001 - one capture must not stop the pass
            failed += 1
            continue
        if outcome.provider_call_made:
            provider_calls += 1
        if outcome.auto_promoted:
            promoted += 1

    return BackfillResult(
        considered=considered,
        promoted=promoted,
        provider_calls=provider_calls,
        failed=failed,
    )
