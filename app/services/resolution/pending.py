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
from app.services.operations import settings as operational
from app.services.resolution import service as resolution_service
from app.services.thinking.claude_cli import ClaudeCliThinker

ACTOR = "resolution-backfill"


@dataclass(frozen=True)
class BackfillResult:
    """What one pass over the pending captures achieved."""

    considered: int = 0
    promoted: int = 0
    provider_calls: int = 0
    #: Model fallback calls spent. Counted apart from ``provider_calls`` because
    #: they cost an order of magnitude more time, so a pass that looks slow is
    #: explained by this number rather than being a mystery.
    model_calls: int = 0
    failed: int = 0

    @property
    def did_work(self) -> bool:
        return bool(self.considered)


def _provider_access(session: Session, settings: Settings) -> resolution_service.ProviderAccess:
    """How logo.dev may be reached on this pass, or an access with no key.

    ``session`` is here because whether the provider may be called is an
    administrator's durable setting rather than an environment variable, so
    answering the question means reading the database.
    """

    usable = (
        operational.enabled(session, "salesnav_domain_enrichment", settings)
        and settings.has_logo_dev_key()
    )
    return resolution_service.ProviderAccess(
        api_key=settings.logo_dev_api_key if usable else None,
        search_url=settings.logo_dev_search_url,
        timeout=settings.logo_dev_timeout_seconds,
        max_candidates=settings.logo_dev_max_candidates,
    )


def _model_access(session: Session, settings: Settings) -> resolution_service.ModelAccess:
    """The model fallback, if it is switched on.

    Available here and deliberately *not* at intake. A model call with web search
    takes tens of seconds; intake already learned the hard way that optional work
    inside an HTTP request can cost a whole submission, and it bounds even the
    fast provider call to a share of its budget. This pass has no request to
    overrun, which is the entire reason it exists.

    ``session`` is here because the switch is an administrator's durable setting
    rather than an environment variable, so reading it needs the database.
    """

    if not operational.enabled(session, "model_company_domain_lookup", settings):
        return resolution_service.ModelAccess()
    return resolution_service.ModelAccess(
        thinker_factory=lambda: ClaudeCliThinker(settings=settings),
        timeout=settings.model_domain_lookup_timeout_seconds,
    )


@dataclass(frozen=True)
class LookupBlocker:
    """One unmet precondition: what it means, and where to go and fix it.

    The setting is a separate field rather than a phrase inside the sentence so a
    page can render it as the one thing to act on. It used to be an ``.env`` line
    because that was genuinely the only way to change any of these; every switch
    among them is an operator control now, so it names the control and the screen
    it lives on instead of telling an administrator to open a shell. The one
    exception is the logo.dev credential, which really is environment-only, and
    there the variable name is still the actionable thing.
    """

    #: Why this stops resolution, in the operator's terms.
    message: str
    #: What to change, and where: a control on the Admin Configuration screen, or
    #: for a deployment credential the environment variable itself.
    setting: str


@dataclass(frozen=True)
class LookupReadiness:
    """Whether automatic domain resolution can run at all, and what is stopping it.

    This exists because of a specific, avoidable failure. A capture page can show
    ``lookup: not_started · 0 attempt(s)`` for four unrelated reasons — two feature
    flags, a third flag, and a missing API key — and none of them was visible
    anywhere. The status was true and useless: it reported a state the system had
    supposedly arrived at, when in fact nothing had ever been attempted, and no page
    said which switch to reach for.

    Worse, that reading is easy to mistake for a broken pipeline, because a stream
    of captures piles up behind it with no error to search for. The resolution path
    is fine; it was never invited to run.

    So the preconditions get named, in the order they have to be fixed, in the
    operator's own vocabulary — including where each one is changed, because "the
    promotion flag" is not something anyone can act on without knowing what it is
    called on the screen that owns it.
    """

    #: True when a logo.dev lookup would actually be attempted for a new capture.
    provider_ready: bool
    #: True when the model fallback would be attempted after the provider fails.
    model_ready: bool
    #: One entry per unmet precondition, ordered by what to fix first. Empty means
    #: resolution is fully configured.
    blockers: tuple[LookupBlocker, ...] = ()

    @property
    def ready(self) -> bool:
        return self.provider_ready


def _control_location(key: str) -> str:
    """Where an operator turns this control on, under the name the screen uses.

    Read from the control registry rather than written out here so the two cannot
    drift: an operator told to look for a label that no longer exists is back to
    guessing.
    """

    return f"{operational.CONTROLS_BY_KEY[key].label} — Admin → Configuration"


def lookup_readiness(session: Session, settings: Settings | None = None) -> LookupReadiness:
    """Why automatic domain resolution would or would not run right now.

    ``session`` is here because three of the four preconditions are now an
    administrator's durable settings rather than environment variables, and
    reading what is actually in force needs the database.
    """

    settings = settings or get_settings()
    features = operational.effective_flags(session, settings)
    blockers: list[LookupBlocker] = []

    if not features.contact_capture_promotion:
        blockers.append(
            LookupBlocker(
                message=(
                    "Capture promotion is switched off, so no capture is resolved automatically."
                ),
                setting=_control_location("contact_capture_promotion"),
            )
        )
    if not features.automatic_company_domain_resolution:
        blockers.append(
            LookupBlocker(
                message=(
                    "Automatic company-domain resolution is switched off, so a capture "
                    "waits for you to press resolve."
                ),
                setting=_control_location("automatic_company_domain_resolution"),
            )
        )
    if not features.salesnav_domain_enrichment:
        blockers.append(
            LookupBlocker(
                message="The logo.dev lookup is switched off, so no provider is ever asked.",
                setting=_control_location("salesnav_domain_enrichment"),
            )
        )
    if not settings.has_logo_dev_key():
        blockers.append(
            LookupBlocker(
                message=(
                    "No logo.dev API key is configured, so the lookup is skipped rather "
                    "than recorded as a failure — deliberately, so the capture stays "
                    "resolvable once a key exists rather than being frozen at a decision "
                    "nobody made."
                ),
                # Genuinely environment-only: a provider credential is a
                # deployment secret and no screen can set it.
                setting="LOGO_DEV_API_KEY=...",
            )
        )

    provider_ready = not blockers
    model_ready = provider_ready and features.model_company_domain_lookup
    if provider_ready and not features.model_company_domain_lookup:
        blockers.append(
            LookupBlocker(
                message=(
                    "The model fallback is switched off, so companies logo.dev cannot "
                    "match stay unresolved rather than being searched for."
                ),
                setting=_control_location("model_company_domain_lookup"),
            )
        )
    return LookupReadiness(
        provider_ready=provider_ready,
        model_ready=model_ready,
        blockers=tuple(blockers),
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
    features = operational.effective_flags(session, settings)
    if not (features.contact_capture_promotion and features.automatic_company_domain_resolution):
        return BackfillResult()

    access = _provider_access(session, settings)
    model = _model_access(session, settings)
    # Same rule as intake: without a provider the policy could only conclude "the
    # lookup was not run", and recording that non-decision would permanently stop
    # the capture resolving automatically later.
    #
    # The model fallback does not lift this. It runs *behind* the provider — on the
    # captures the provider looked at and could not resolve — so a pass with the
    # fallback on and no logo.dev key would be asking a model to stand in for a
    # lookup that never happened, and would record its answer as though the
    # deterministic path had been exhausted. It had not been tried.
    if not access.available:
        return BackfillResult()

    considered = 0
    promoted = 0
    provider_calls = 0
    model_calls = 0
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
                    model=model,
                    actor=actor,
                    force=False,
                )
        except Exception:  # noqa: BLE001 - one capture must not stop the pass
            failed += 1
            continue
        if outcome.provider_call_made:
            provider_calls += 1
        if outcome.model_call_made:
            model_calls += 1
        if outcome.auto_promoted:
            promoted += 1

    return BackfillResult(
        considered=considered,
        promoted=promoted,
        provider_calls=provider_calls,
        model_calls=model_calls,
        failed=failed,
    )
