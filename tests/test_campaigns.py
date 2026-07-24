"""Campaign-creation and settings tests (CMP-001)."""

from __future__ import annotations

import uuid

import pytest
from app.models.audit_event import AuditEvent
from app.models.campaign import DEFAULT_MIN_SCORE_THRESHOLD, Campaign
from app.models.enums import CampaignStatus
from app.services.campaigns import (
    CampaignError,
    CampaignNotFound,
    create_campaign,
    update_campaign_settings,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

# A message an operator-facing CampaignError must never contain — leaking any
# of these would violate AGENTS.md ("keep secrets ... out of ... error
# messages") and the CMP-001 requirement that errors never leak internals.
_FORBIDDEN_ERROR_SUBSTRINGS = (
    "Traceback",
    "sqlalchemy",
    "psycopg",
    "IntegrityError",
    "postgresql://",
    "postgresql+psycopg://",
    "SELECT ",
    "INSERT ",
)


def _assert_safe_error_message(message: str) -> None:
    lowered = message.lower()
    for forbidden in _FORBIDDEN_ERROR_SUBSTRINGS:
        assert forbidden.lower() not in lowered, f"error leaked internal detail: {message!r}"


def test_create_campaign_persists_with_defaults(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="  Pilot 100  ")
    assert campaign.name == "Pilot 100"  # trimmed
    assert campaign.status is CampaignStatus.DRAFT
    assert campaign.min_score_threshold == DEFAULT_MIN_SCORE_THRESHOLD
    assert campaign.offer is None
    assert campaign.audience_rules is None
    assert campaign.exclusions is None
    assert campaign.tone is None
    assert campaign.owner is None
    assert campaign.source is None
    assert campaign.sending_reference is None

    fetched = db_session.get(Campaign, campaign.id)
    assert fetched is not None
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


def test_create_campaign_records_audit_event(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Audited Campaign")
    events = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "campaign.created",
            AuditEvent.entity_id == str(campaign.id),
        )
    ).all()
    assert len(events) == 1
    assert events[0].dry_run is True  # default dry-run stamping preserved


def test_create_campaign_rejects_blank_name(db_session: Session) -> None:
    with pytest.raises(CampaignError):
        create_campaign(db_session, name="   ")


def test_create_campaign_persists_full_settings_round_trip(db_session: Session) -> None:
    audience_rules = {"titles": ["VP Sales", "Head of Growth"], "min_company_size": 50}
    exclusions = {"excluded_domains": ["competitor.example"], "excluded_titles": ["Intern"]}

    campaign = create_campaign(
        db_session,
        name="Full Settings Campaign",
        description="A representative pilot batch.",
        offer="Free 30-minute audit",
        audience_rules=audience_rules,
        exclusions=exclusions,
        min_score_threshold=70,
        tone="direct",
        owner="sahil@example.com",
        source="sales_navigator",
        sending_reference="saleshandy-seq-42",
    )
    db_session.flush()
    db_session.expire_all()

    fetched = db_session.get(Campaign, campaign.id)
    assert fetched is not None
    assert fetched.name == "Full Settings Campaign"
    assert fetched.description == "A representative pilot batch."
    assert fetched.offer == "Free 30-minute audit"
    assert fetched.audience_rules == audience_rules
    assert fetched.exclusions == exclusions
    assert fetched.min_score_threshold == 70
    assert fetched.tone == "direct"
    assert fetched.owner == "sahil@example.com"
    assert fetched.source == "sales_navigator"
    assert fetched.sending_reference == "saleshandy-seq-42"
    assert fetched.status is CampaignStatus.DRAFT


def test_create_campaign_normalizes_blank_optional_text_to_none(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="Blank Optionals", offer="   ", tone="\t")
    assert campaign.offer is None
    assert campaign.tone is None


@pytest.mark.parametrize("bad_value", [["not", "a", "dict"], "a string", 42, True])
def test_create_campaign_rejects_non_object_audience_rules(
    db_session: Session, bad_value: object
) -> None:
    with pytest.raises(CampaignError) as excinfo:
        create_campaign(db_session, name="Bad Audience Rules", audience_rules=bad_value)  # type: ignore[arg-type]
    _assert_safe_error_message(str(excinfo.value))


def test_create_campaign_rejects_non_object_exclusions(db_session: Session) -> None:
    with pytest.raises(CampaignError):
        create_campaign(db_session, name="Bad Exclusions", exclusions=["a", "b"])  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_threshold", [-1, 101, 1000])
def test_create_campaign_rejects_out_of_range_min_score_threshold(
    db_session: Session, bad_threshold: int
) -> None:
    with pytest.raises(CampaignError) as excinfo:
        create_campaign(db_session, name="Bad Threshold", min_score_threshold=bad_threshold)
    _assert_safe_error_message(str(excinfo.value))


def test_create_campaign_rejects_oversized_offer(db_session: Session) -> None:
    with pytest.raises(CampaignError):
        create_campaign(db_session, name="Oversized Offer", offer="x" * 5000)


def test_create_campaign_rejects_oversized_json_settings(db_session: Session) -> None:
    huge_rules = {"notes": "x" * 25_000}
    with pytest.raises(CampaignError):
        create_campaign(db_session, name="Oversized Rules", audience_rules=huge_rules)


# --- Updates -------------------------------------------------------------


def _create_full(db_session: Session, **overrides: object) -> Campaign:
    defaults: dict[str, object] = dict(
        name="Base Campaign",
        description="Base description",
        offer="Base offer",
        audience_rules={"titles": ["CTO"]},
        exclusions={"excluded_titles": ["Intern"]},
        min_score_threshold=85,
        tone="warm",
        owner="owner@example.com",
        source="manual",
        sending_reference="seq-1",
    )
    defaults.update(overrides)
    return create_campaign(db_session, **defaults)  # type: ignore[arg-type]


def test_update_campaign_partial_update_preserves_omitted_fields(db_session: Session) -> None:
    campaign = _create_full(db_session)

    updated = update_campaign_settings(db_session, campaign.id, tone="urgent")

    assert updated.tone == "urgent"
    # Everything else the caller did not mention is untouched.
    assert updated.name == "Base Campaign"
    assert updated.description == "Base description"
    assert updated.offer == "Base offer"
    assert updated.audience_rules == {"titles": ["CTO"]}
    assert updated.exclusions == {"excluded_titles": ["Intern"]}
    assert updated.min_score_threshold == 85
    assert updated.owner == "owner@example.com"
    assert updated.source == "manual"
    assert updated.sending_reference == "seq-1"


def test_update_campaign_explicit_none_clears_nullable_field(db_session: Session) -> None:
    campaign = _create_full(db_session)

    updated = update_campaign_settings(db_session, campaign.id, description=None)

    assert updated.description is None
    # Confirms it is a real, persisted clear, not just an in-memory default.
    db_session.flush()
    db_session.expire_all()
    fetched = db_session.get(Campaign, campaign.id)
    assert fetched is not None
    assert fetched.description is None


def test_update_campaign_omission_differs_from_explicit_clear(db_session: Session) -> None:
    """Omitting a keyword must behave differently from passing it as ``None``."""

    left_alone = _create_full(db_session, name="Left Alone", offer="Keep me")
    cleared = _create_full(db_session, name="Explicitly Cleared", offer="Clear me")

    # Omit `offer` entirely: unchanged.
    result_untouched = update_campaign_settings(db_session, left_alone.id, tone="calm")
    assert result_untouched.offer == "Keep me"

    # Pass `offer=None` explicitly: cleared.
    result_cleared = update_campaign_settings(db_session, cleared.id, offer=None)
    assert result_cleared.offer is None


def test_update_campaign_json_settings_round_trip(db_session: Session) -> None:
    campaign = _create_full(db_session)
    new_rules = {"titles": ["VP Marketing"], "industries": ["saas"]}

    updated = update_campaign_settings(db_session, campaign.id, audience_rules=new_rules)

    assert updated.audience_rules == new_rules
    # exclusions untouched
    assert updated.exclusions == {"excluded_titles": ["Intern"]}


def test_update_campaign_rejects_invalid_settings_without_partial_apply(
    db_session: Session,
) -> None:
    campaign = _create_full(db_session, tone="warm")

    with pytest.raises(CampaignError) as excinfo:
        update_campaign_settings(db_session, campaign.id, tone="urgent", min_score_threshold=999)
    _assert_safe_error_message(str(excinfo.value))

    db_session.flush()
    db_session.expire_all()
    fetched = db_session.get(Campaign, campaign.id)
    assert fetched is not None
    # Neither field was applied — the whole update was rejected atomically.
    assert fetched.tone == "warm"
    assert fetched.min_score_threshold == 85


def test_update_campaign_rejects_null_for_non_nullable_name(db_session: Session) -> None:
    campaign = _create_full(db_session)
    with pytest.raises(CampaignError):
        update_campaign_settings(db_session, campaign.id, name="   ")
    db_session.flush()
    db_session.expire_all()
    fetched = db_session.get(Campaign, campaign.id)
    assert fetched is not None
    assert fetched.name == "Base Campaign"


def test_update_campaign_allows_valid_status_transitions(db_session: Session) -> None:
    campaign = _create_full(db_session)
    assert campaign.status is CampaignStatus.DRAFT

    activated = update_campaign_settings(db_session, campaign.id, status=CampaignStatus.ACTIVE)
    assert activated.status is CampaignStatus.ACTIVE

    archived = update_campaign_settings(db_session, campaign.id, status=CampaignStatus.ARCHIVED)
    assert archived.status is CampaignStatus.ARCHIVED


def test_update_campaign_rejects_illegal_status_transition(db_session: Session) -> None:
    campaign = _create_full(db_session)
    update_campaign_settings(db_session, campaign.id, status=CampaignStatus.ARCHIVED)

    with pytest.raises(CampaignError) as excinfo:
        update_campaign_settings(db_session, campaign.id, status=CampaignStatus.DRAFT)
    _assert_safe_error_message(str(excinfo.value))

    db_session.flush()
    db_session.expire_all()
    fetched = db_session.get(Campaign, campaign.id)
    assert fetched is not None
    assert fetched.status is CampaignStatus.ARCHIVED  # unchanged


def test_update_campaign_same_status_is_a_noop(db_session: Session) -> None:
    campaign = _create_full(db_session)
    result = update_campaign_settings(db_session, campaign.id, status=CampaignStatus.DRAFT)
    assert result.status is CampaignStatus.DRAFT

    events_before = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "campaign.updated",
            AuditEvent.entity_id == str(campaign.id),
        )
    ).all()
    assert len(events_before) == 0  # a true no-op records no audit event


def test_update_campaign_records_audit_event_with_changed_fields(db_session: Session) -> None:
    campaign = _create_full(db_session)
    update_campaign_settings(db_session, campaign.id, tone="urgent", owner="new-owner@example.com")

    events = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "campaign.updated",
            AuditEvent.entity_id == str(campaign.id),
        )
    ).all()
    assert len(events) == 1
    assert events[0].context is not None
    assert set(events[0].context["fields_changed"]) == {"tone", "owner"}


def test_update_campaign_not_found_raises(db_session: Session) -> None:
    with pytest.raises(CampaignNotFound):
        update_campaign_settings(db_session, uuid.uuid4(), tone="urgent")


def test_update_campaign_no_changes_returns_unchanged_without_audit(db_session: Session) -> None:
    campaign = _create_full(db_session)
    result = update_campaign_settings(db_session, campaign.id, tone="warm")  # same as stored
    assert result.tone == "warm"

    events = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "campaign.updated",
            AuditEvent.entity_id == str(campaign.id),
        )
    ).all()
    assert len(events) == 0
