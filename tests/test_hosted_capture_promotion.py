"""Hosted capture promotion: the staging boundary, and the captures behind it.

Two halves, and they answer two different questions.

**The boundary.** ``contact_capture_promotion`` was a local-only feature switch,
refused outright in staging and production. The hosted Beta needs it, and the
cheap way to get it — deleting the name from ``_LOCAL_ONLY_FEATURES`` — would
have permitted promotion in every hosted environment in every state of
configuration. The tests here describe the boundary that replaced it instead:
staging only, and only with every dependency automatic promotion actually needs.
Each refusal is paired with the positive case that proves it is not vacuous.

**The captures.** A boundary that starts a process is worth nothing if the work
already sitting in the database cannot move. Staging accepted captures for weeks
while promotion was unavailable — immutable snapshots, explicit campaign filing
requests, Capture jobs recorded as succeeded, and no Contact anywhere. Those
captures must become promotable the moment the boundary is satisfied, through
the existing pending worker, with nobody editing a database row. The second half
of this file proves that end to end: one capture staged with promotion
unavailable, then resolved, promoted, filed into its Campaign exactly once, and
handed to the Identity stage — including when the Campaign is paused, where the
membership must exist while everything after Capture stays held.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.core.auth.config import AuthSettings
from app.core.config import Settings, get_settings
from app.core.runtime import RuntimeConfigurationError, validate_runtime_settings
from app.main import create_app
from app.models.campaign import Campaign, CampaignContact
from app.models.company import Company
from app.models.company_domain_resolution import CompanyDomainResolution
from app.models.contact import Contact
from app.models.enums import (
    AgentControlStatus,
    AgentIdentifier,
    CampaignStatus,
    CaptureCampaignFilingStatus,
    ContactPromotionOutcome,
    DomainResolutionState,
    PipelineStageStatus,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.models.pipeline import CampaignContactAgentState, CaptureCampaignFiling
from app.services.captures import campaign_filing
from app.services.captures import intake as capture_intake
from app.services.enrichment import logodev
from app.services.resolution import pending
from sqlalchemy import func, select
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "extensions" / "salesnav-capture" / "docs" / "fixtures"
PROFILE_SUBMISSION = json.loads(
    (FIXTURES / "contact-capture.profile.example.json").read_text("utf-8")
)
PROVIDER_SAMPLES = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "logodev_brand_search_sanitized.json").read_text("utf-8")
)

STAGING_HOST = "srv1885453.hstgr.cloud"
STAGING_ORIGIN = f"https://{STAGING_HOST}"
STAGING_DATABASE_URL = "postgresql+psycopg://vmr:secret@db.internal.example:5432/vmr_staging"
SESSION_SECRET = "test-session-secret-value-at-least-32-chars"
APPROVED_EMAIL = "operator@vmr.example"

# Never a real key. It exists only so `has_logo_dev_key()` is true and the
# provider transport below is the thing that actually answers.
PROVIDER_KEY = "logo-dev-key-never-real"
DOMAIN = "meridianworks.example"


# --- Part one: the configuration boundary -------------------------------------


def _staging(**overrides: Any) -> Settings:
    """A staging configuration that is complete by the *whole* startup contract.

    Built with an explicit ``auth`` block for the same reason
    ``tests/test_production_hardening.py`` does: ``create_app`` refuses any
    hosted environment without a complete hosted-authentication boundary, so a
    fixture that omitted it would fail on the auth contract before reaching the
    promotion rule each test is actually about.
    """

    values: dict[str, Any] = {
        "_env_file": None,
        "app_env": "staging",
        "database_url": STAGING_DATABASE_URL,
        "trusted_hosts": (STAGING_HOST,),
        "trusted_proxy_cidrs": ("10.20.0.0/24",),
        "dry_run": True,
        "auth": AuthSettings(
            enabled=True,
            session_secret=SESSION_SECRET,
            google_client_id="staging-client-id",
            google_client_secret="staging-client-secret",
            allowed_operator_emails=(APPROVED_EMAIL,),
            public_base_url=STAGING_ORIGIN,
        ),
    }
    values.update(overrides)
    return Settings(**values)


def _features(**flags: bool) -> dict[str, Any]:
    """The four promotion prerequisites, all satisfied unless overridden."""

    enabled = {
        "contact_capture_promotion": True,
        "automatic_company_domain_resolution": True,
        "salesnav_domain_enrichment": True,
    }
    enabled.update(flags)
    return {"features": enabled}


def test_staging_with_promotion_off_starts_exactly_as_it_did() -> None:
    """1. The Beta is opt-in; a staging deployment that says nothing is unchanged."""

    validate_runtime_settings(_staging())


def test_staging_refuses_promotion_without_automatic_domain_resolution() -> None:
    """2. Promotion with nothing to resolve a domain promotes nothing, silently."""

    with pytest.raises(RuntimeConfigurationError) as caught:
        validate_runtime_settings(
            _staging(
                logo_dev_api_key=PROVIDER_KEY,
                **_features(automatic_company_domain_resolution=False),
            )
        )
    assert "FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION" in str(caught.value)


def test_staging_refuses_promotion_without_the_provider_switch() -> None:
    """3. The resolution policy is never asked anything without this one."""

    with pytest.raises(RuntimeConfigurationError) as caught:
        validate_runtime_settings(
            _staging(
                logo_dev_api_key=PROVIDER_KEY,
                **_features(salesnav_domain_enrichment=False),
            )
        )
    assert "FEATURES__SALESNAV_DOMAIN_ENRICHMENT" in str(caught.value)


def test_staging_refuses_promotion_without_a_provider_key() -> None:
    """4. Three switches on and no key is the exact state staging was in."""

    with pytest.raises(RuntimeConfigurationError) as caught:
        validate_runtime_settings(_staging(**_features()))
    assert "LOGO_DEV_API_KEY" in str(caught.value)


def test_staging_reports_every_missing_prerequisite_at_once() -> None:
    """One restart should teach an operator all of it, not one item per restart."""

    with pytest.raises(RuntimeConfigurationError) as caught:
        validate_runtime_settings(
            _staging(
                **_features(
                    automatic_company_domain_resolution=False,
                    salesnav_domain_enrichment=False,
                )
            )
        )
    message = str(caught.value)
    assert "FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION" in message
    assert "FEATURES__SALESNAV_DOMAIN_ENRICHMENT" in message
    assert "LOGO_DEV_API_KEY" in message


def test_staging_with_every_prerequisite_starts() -> None:
    """5. The refusals above are refusals, not a feature that cannot be enabled."""

    validate_runtime_settings(_staging(logo_dev_api_key=PROVIDER_KEY, **_features()))


def test_a_complete_staging_promotion_configuration_builds_the_application() -> None:
    """The same configuration through the real startup path, not the rule alone."""

    class _Ready:
        def __call__(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
            raise AssertionError("readiness is never probed in this test")

        def check(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"status": "ready"}

    settings = _staging(
        logo_dev_api_key=PROVIDER_KEY,
        **_features(workbench=True),
    )
    assert create_app(settings, readiness_probe=_Ready()) is not None


def test_production_refuses_the_same_beta_promotion_configuration() -> None:
    """6. Production does not inherit the staging exception by being 'hosted'."""

    with pytest.raises(RuntimeConfigurationError) as caught:
        validate_runtime_settings(
            _staging(
                app_env="production",
                database_url="postgresql+psycopg://service:pw@db.example.com/vmr_prod",
                trusted_hosts=("outbound.example.com",),
                logo_dev_api_key=PROVIDER_KEY,
                **_features(),
            )
        )
    message = str(caught.value)
    assert "FEATURES__CONTACT_CAPTURE_PROMOTION" in message
    assert "staging only" in message
    # The dependency findings are deliberately absent: they read as a checklist
    # under a refusal that no prerequisite can lift.
    assert "FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION" not in message


def test_production_refuses_promotion_even_with_nothing_else_configured() -> None:
    """The switch alone is the refusal; it does not need the rest to be wrong."""

    with pytest.raises(RuntimeConfigurationError) as caught:
        validate_runtime_settings(
            _staging(
                app_env="production",
                database_url="postgresql+psycopg://service:pw@db.example.com/vmr_prod",
                trusted_hosts=("outbound.example.com",),
                features={"contact_capture_promotion": True},
            )
        )
    assert "may not be enabled in production" in str(caught.value)


@pytest.mark.parametrize("environment", ["local", "development", "test", "ci"])
def test_local_behaviour_is_unchanged(environment: str) -> None:
    """7. Promotion in development needs no provider, no key and no ceremony.

    This is the property the boundary must not have broken. A developer enabling
    promotion alone, against the local database, with nothing else configured,
    is exactly the DAT-014 operator flow — the one route that needs no provider
    key at all, because the operator types the domain.
    """

    validate_runtime_settings(
        Settings(
            _env_file=None,
            app_env=environment,
            features={"contact_capture_promotion": True},
        )
    )


def test_the_other_intakes_did_not_move_with_promotion() -> None:
    """Only promotion gained a hosted exception; the local-only list still bites."""

    with pytest.raises(RuntimeConfigurationError) as caught:
        validate_runtime_settings(_staging(features={"linkedin_company_intake": True}))
    assert "linkedin_company_intake" in str(caught.value)


def test_no_secret_ever_appears_in_a_refusal_or_a_settings_dump() -> None:
    """12. The boundary reads a provider key; nothing may echo one."""

    settings = _staging(
        logo_dev_api_key=PROVIDER_KEY,
        millionverifier_api_key="mv-key-never-real",
        database_url="postgresql+psycopg://vmr:TOP-SECRET@db.internal.example:5432/vmr_dev",
        **_features(),
    )
    with pytest.raises(RuntimeConfigurationError) as caught:
        validate_runtime_settings(settings)

    message = str(caught.value)
    assert PROVIDER_KEY not in message
    assert "TOP-SECRET" not in message
    assert "mv-key-never-real" not in message

    dumped = json.dumps(settings.model_dump(mode="json"))
    assert PROVIDER_KEY not in dumped
    assert "mv-key-never-real" not in dumped
    assert PROVIDER_KEY not in repr(settings)
    # The key is still readable by the code that needs it, which is what makes
    # the exclusions above a redaction rather than an absence.
    assert settings.has_logo_dev_key() is True


# --- Part two: the captures already waiting ------------------------------------


def _transport(sample: str) -> logodev.Transport:
    body = PROVIDER_SAMPLES[sample]["body"]

    def _call(url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        return logodev.RawResponse(status_code=200, body=json.dumps(body))

    return _call


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """The staging promotion environment, applied to the process settings.

    Environment variables rather than a constructed ``Settings`` because the code
    under test here — ``resolve_pending`` and the intake pass — reads
    ``get_settings()``, and the point is that the deployment's own configuration
    drives them.
    """

    env = {
        "APP_ENV": "staging",
        "FEATURES__CONTACT_CAPTURE_INTAKE": "true",
        "FEATURES__CONTACT_CAPTURE_PROMOTION": "true",
        "FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION": "true",
        "FEATURES__SALESNAV_DOMAIN_ENRICHMENT": "true",
        "LOGO_DEV_API_KEY": PROVIDER_KEY,
    }
    env.update(overrides)
    for key, value in env.items():
        if value == "":
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """One clean provider answer, substituted at the transport, not above it.

    Patching ``_urllib_transport`` rather than ``pending._provider_access`` is
    deliberate: the settings-driven wiring — the enrichment switch and the key —
    stays in the path, so a test that promotes is also evidence that the
    configured prerequisites are what carried the call.
    """

    monkeypatch.setattr(logodev, "_urllib_transport", _transport("clean_single_match"))


def _campaign(db: Session, *, name: str, execution_enabled: bool = True) -> Campaign:
    campaign = Campaign(
        name=name,
        status=CampaignStatus.ACTIVE,
        execution_enabled=execution_enabled,
    )
    db.add(campaign)
    db.flush()
    return campaign


def _submit(db: Session, *, campaign: Campaign | None = None) -> LinkedInProfileSnapshot:
    """One reviewed contact-first submission, exactly as the extension sends it."""

    payload = copy.deepcopy(PROFILE_SUBMISSION)
    payload["client_submission_id"] = str(uuid.uuid4())
    payload["campaign_id"] = str(campaign.id) if campaign is not None else None
    for capture in payload["contacts"]:
        capture["client_capture_id"] = str(uuid.uuid4())
    result = capture_intake.stage_contact_captures(
        db, payload=payload, operator_base_url=STAGING_ORIGIN
    )
    snapshot = db.get(LinkedInProfileSnapshot, uuid.UUID(str(result.results[0].capture_id)))
    assert snapshot is not None
    return snapshot


def _stage_capture(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    campaign: Campaign | None = None,
) -> LinkedInProfileSnapshot:
    """A capture accepted while promotion was unavailable, then left pending.

    This is the state the 44 hosted captures are in, and reproducing it is the
    point: intake resolves what it can inside its own request budget, so a
    submission accepted with the prerequisites already satisfied would be
    promoted before ``resolve_pending`` ever saw it, and a test that called the
    worker afterwards would be asserting against a no-op.

    The environment is restored to the complete staging configuration on the way
    out, so what follows is the recovery path and nothing else.
    """

    _env(monkeypatch, FEATURES__CONTACT_CAPTURE_PROMOTION="", LOGO_DEV_API_KEY="")
    try:
        return _submit(db, campaign=campaign)
    finally:
        _env(monkeypatch)


def _counts(db: Session) -> tuple[int, int, int]:
    return (
        db.scalar(select(func.count()).select_from(Contact)) or 0,
        db.scalar(select(func.count()).select_from(Company)) or 0,
        db.scalar(select(func.count()).select_from(CampaignContact)) or 0,
    )


def test_a_capture_accepted_while_promotion_was_unavailable_promotes_later(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """8. The exact staging situation: captures stored, nothing promoted, then enabled.

    The first half reproduces the blocker rather than assuming it — a capture is
    accepted with promotion switched off, and the assertions record that it left
    behind no Contact, no Company and, critically, **no resolution decision**. A
    recorded decision is what would have made this unrecoverable: the pending
    query skips anything already decided, so a capture that had been "decided"
    while the provider was unreachable would never be looked at again.
    """

    campaign = _campaign(db_session, name="Hosted capture recovery")
    snapshot = _stage_capture(db_session, monkeypatch, campaign=campaign)

    assert _counts(db_session) == (0, 0, 0)
    assert snapshot.matched_contact_id is None
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CompanyDomainResolution)
            .where(CompanyDomainResolution.capture_id == snapshot.id)
        )
        == 0
    )
    filing = campaign_filing.get_filing(db_session, capture_id=snapshot.id)
    assert filing is not None
    assert filing.status is CaptureCampaignFilingStatus.PENDING
    assert filing.requested_campaign_id == campaign.id
    assert filing.campaign_contact_id is None

    # Nothing is edited. The prerequisites are supplied — already restored by the
    # helper above — and the worker's own pass runs. That is the whole recovery
    # procedure.
    monkeypatch.setattr(logodev, "_urllib_transport", _transport("clean_single_match"))
    assert snapshot.id in pending.pending_capture_ids(db_session)

    result = pending.resolve_pending(db_session, limit=10)

    assert result.considered == 1
    assert result.promoted == 1
    assert result.failed == 0
    db_session.flush()

    contacts, companies, memberships = _counts(db_session)
    assert (contacts, companies, memberships) == (1, 1, 1)
    contact = db_session.scalars(select(Contact)).one()
    assert contact.company_domain == DOMAIN
    db_session.refresh(snapshot)
    decision = db_session.scalars(
        select(CompanyDomainResolution).where(CompanyDomainResolution.capture_id == snapshot.id)
    ).one()
    assert decision.state in {DomainResolutionState.PROVISIONAL, DomainResolutionState.CONFIRMED}


def test_the_pending_worker_does_nothing_while_a_prerequisite_is_missing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed at the point of use, not only at startup.

    The startup boundary makes this state unreachable in a running staging
    deployment, which is exactly why it is worth pinning here: the service must
    not depend on that for its safety, and it must leave the capture *untouched*
    rather than recording a non-decision.
    """

    snapshot = _stage_capture(db_session, monkeypatch)

    for missing in (
        {"FEATURES__CONTACT_CAPTURE_PROMOTION": ""},
        {"FEATURES__AUTOMATIC_COMPANY_DOMAIN_RESOLUTION": ""},
        {"FEATURES__SALESNAV_DOMAIN_ENRICHMENT": ""},
        {"LOGO_DEV_API_KEY": ""},
    ):
        _env(monkeypatch, **missing)

        def _never(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("resolution must not run with a missing prerequisite")

        from app.services.resolution import service as resolution_service

        monkeypatch.setattr(resolution_service, "resolve", _never)
        assert pending.resolve_pending(db_session).did_work is False

    assert _counts(db_session) == (0, 0, 0)
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CompanyDomainResolution)
            .where(CompanyDomainResolution.capture_id == snapshot.id)
        )
        == 0
    )
    # Untouched, so still recoverable once the boundary is satisfied.
    _env(monkeypatch)
    assert snapshot.id in pending.pending_capture_ids(db_session)


def test_an_explicit_campaign_request_becomes_exactly_one_campaign_contact(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, provider: None
) -> None:
    """9. The filing the capture already carried, applied once, against that Campaign."""

    campaign = _campaign(db_session, name="Hosted filing exactly once")
    other = _campaign(db_session, name="A campaign nobody filed into")
    snapshot = _stage_capture(db_session, monkeypatch, campaign=campaign)

    pending.resolve_pending(db_session, limit=10)
    db_session.flush()

    membership = db_session.scalars(select(CampaignContact)).one()
    assert membership.campaign_id == campaign.id
    assert membership.campaign_id != other.id
    assert membership.source_capture_id == snapshot.id

    filing = campaign_filing.get_filing(db_session, capture_id=snapshot.id)
    assert filing is not None
    assert filing.status is CaptureCampaignFilingStatus.APPLIED
    assert filing.campaign_contact_id == membership.id
    assert filing.applied_at is not None
    assert filing.error_code is None


def test_a_fresh_hosted_capture_promotes_and_files_inside_the_request(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, provider: None
) -> None:
    """The steady state once the boundary is satisfied, not only the backlog.

    Every other test here deliberately stages its capture while promotion is
    unavailable, so the worker has something to recover. This is the other case:
    a capture submitted into a fully configured staging deployment is resolved,
    promoted and filed inside the intake request itself, and the worker finds
    nothing left to do.
    """

    _env(monkeypatch)
    campaign = _campaign(db_session, name="Hosted fresh capture")
    snapshot = _submit(db_session, campaign=campaign)
    db_session.flush()

    contacts, companies, memberships = _counts(db_session)
    assert (contacts, companies, memberships) == (1, 1, 1)

    filing = campaign_filing.get_filing(db_session, capture_id=snapshot.id)
    assert filing is not None
    assert filing.status is CaptureCampaignFilingStatus.APPLIED
    assert filing.campaign_contact_id == db_session.scalars(select(CampaignContact)).one().id
    assert snapshot.id not in pending.pending_capture_ids(db_session)
    assert pending.resolve_pending(db_session, limit=10).considered == 0


def test_no_campaign_membership_is_invented_for_a_capture_that_requested_none(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, provider: None
) -> None:
    """A Contact is permanent; a Campaign is a separate, explicit request."""

    snapshot = _stage_capture(db_session, monkeypatch)

    pending.resolve_pending(db_session, limit=10)
    db_session.flush()

    contacts, _, memberships = _counts(db_session)
    assert contacts == 1
    assert memberships == 0
    assert campaign_filing.get_filing(db_session, capture_id=snapshot.id) is None
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CaptureCampaignFiling)
            .where(CaptureCampaignFiling.capture_id == snapshot.id)
        )
        == 0
    )


def test_a_second_pass_creates_no_second_contact_and_no_second_membership(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, provider: None
) -> None:
    """10. Retry is the normal case for a worker, so it has to be free of cost."""

    campaign = _campaign(db_session, name="Hosted idempotent retry")
    snapshot = _stage_capture(db_session, monkeypatch, campaign=campaign)

    first = pending.resolve_pending(db_session, limit=10)
    db_session.flush()
    assert first.promoted == 1
    before = _counts(db_session)
    membership_id = db_session.scalars(select(CampaignContact)).one().id

    # The pending query alone already refuses it, and the promotion service is
    # asked directly as well — the guarantee is the service's, not the query's.
    assert snapshot.id not in pending.pending_capture_ids(db_session)
    assert pending.resolve_pending(db_session, limit=10).considered == 0

    from app.services.captures import promotion as capture_promotion

    repeat = capture_promotion.promote(db_session, snapshot=snapshot, actor="test-retry")
    db_session.flush()

    assert repeat.contact_outcome is ContactPromotionOutcome.ALREADY_PROMOTED
    assert _counts(db_session) == before
    assert db_session.scalars(select(CampaignContact)).one().id == membership_id
    filing = campaign_filing.get_filing(db_session, capture_id=snapshot.id)
    assert filing is not None
    assert filing.campaign_contact_id == membership_id


def test_an_unresolved_domain_creates_no_contact_at_all(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """11. Never fabricate a domain, and never promote without one.

    Two plausible candidates is the case that matters most: the provider
    answered, and the answer is precisely why an operator is still needed. The
    capture keeps its pending campaign filing, so nothing about the operator's
    request is lost while it waits.
    """

    campaign = _campaign(db_session, name="Hosted unresolved company")
    snapshot = _stage_capture(db_session, monkeypatch, campaign=campaign)
    monkeypatch.setattr(logodev, "_urllib_transport", _transport("two_plausible_matches"))

    result = pending.resolve_pending(db_session, limit=10)
    db_session.flush()

    assert result.considered == 1
    assert result.promoted == 0
    assert _counts(db_session) == (0, 0, 0)

    decision = db_session.scalars(
        select(CompanyDomainResolution).where(CompanyDomainResolution.capture_id == snapshot.id)
    ).one()
    assert decision.state is DomainResolutionState.UNRESOLVED
    filing = campaign_filing.get_filing(db_session, capture_id=snapshot.id)
    assert filing is not None
    assert filing.status is CaptureCampaignFilingStatus.PENDING
    assert filing.campaign_contact_id is None


# --- Pipeline handoff ----------------------------------------------------------


def _stage_state(
    db: Session, *, membership: CampaignContact, agent_id: AgentIdentifier
) -> CampaignContactAgentState | None:
    return db.scalars(
        select(CampaignContactAgentState).where(
            CampaignContactAgentState.campaign_contact_id == membership.id,
            CampaignContactAgentState.agent_id == agent_id,
        )
    ).one_or_none()


def test_enrollment_completes_capture_and_queues_identity_and_nothing_further(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, provider: None
) -> None:
    """The handoff: Capture complete, Identity next, and no later stage touched."""

    campaign = _campaign(db_session, name="Hosted pipeline handoff")
    _stage_capture(db_session, monkeypatch, campaign=campaign)

    pending.resolve_pending(db_session, limit=10)
    db_session.flush()

    membership = db_session.scalars(select(CampaignContact)).one()
    assert membership.latest_completed_stage is AgentIdentifier.CAPTURE
    assert membership.next_stage is AgentIdentifier.IDENTITY

    capture_state = _stage_state(
        db_session, membership=membership, agent_id=AgentIdentifier.CAPTURE
    )
    assert capture_state is not None
    assert capture_state.status is PipelineStageStatus.COMPLETED

    identity_state = _stage_state(
        db_session, membership=membership, agent_id=AgentIdentifier.IDENTITY
    )
    assert identity_state is not None
    assert identity_state.status is not PipelineStageStatus.COMPLETED

    # No stage after Identity is created, let alone advanced. A capture that
    # reached Research or Verification state here would mean the pipeline had
    # been jumped rather than handed over.
    for later in (
        AgentIdentifier.COMPANY,
        AgentIdentifier.RESEARCH,
        AgentIdentifier.EMAIL,
        AgentIdentifier.VERIFICATION,
        AgentIdentifier.INSIGHTS,
        AgentIdentifier.PERSONALIZATION,
        AgentIdentifier.SENDING,
    ):
        assert _stage_state(db_session, membership=membership, agent_id=later) is None


def test_a_paused_campaign_still_enrolls_but_holds_every_stage_after_capture(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, provider: None
) -> None:
    """The Pause/Resume gate survives automatic promotion.

    Enrollment is a statement about membership, not permission to work: the
    Campaign Contact exists and Capture is complete, while Identity is held by
    the Campaign's own execution switch rather than queued or — much worse —
    auto-skipped, which is terminal and would lose the work a resume expects to
    find.
    """

    campaign = _campaign(db_session, name="Hosted paused campaign", execution_enabled=False)
    _stage_capture(db_session, monkeypatch, campaign=campaign)

    pending.resolve_pending(db_session, limit=10)
    db_session.flush()

    membership = db_session.scalars(select(CampaignContact)).one()
    assert membership.campaign_id == campaign.id
    assert membership.latest_completed_stage is AgentIdentifier.CAPTURE

    identity_state = _stage_state(
        db_session, membership=membership, agent_id=AgentIdentifier.IDENTITY
    )
    assert identity_state is not None
    assert identity_state.status is not PipelineStageStatus.SKIPPED
    assert identity_state.status is not PipelineStageStatus.COMPLETED

    from app.models.verification_job import AgentJob

    queued = db_session.scalars(
        select(AgentJob).where(AgentJob.campaign_contact_id == membership.id)
    ).all()
    assert [job for job in queued if job.agent_id is AgentIdentifier.IDENTITY] == []

    from app.services.agents.controls import effective_control

    control = effective_control(db_session, campaign=campaign, agent_id=AgentIdentifier.IDENTITY)
    assert control.status is AgentControlStatus.DISABLED
