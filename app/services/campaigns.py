"""Campaign service (CMP-001).

Creates and updates the draft campaign settings an authorized import targets —
name, offer, structured audience rules, structured exclusions, the minimum
Initial Fit Score threshold, copy tone, owner, source, and a sending-
configuration reference — and provides the read paths the workbench lists and
detail pages use.

All validation lives here (AGENTS.md: "put authoritative rules in backend
services"); the JSON API (``app/api/routes.py``) and the operator workbench
(``app/web/routes.py``) are both thin adapters over these functions. Sequence
generation, sending, scheduling, and provider integrations are explicitly out
of scope for this service — ``sending_reference`` is stored opaquely and never
resolved against a sending provider here.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.campaign import DEFAULT_MIN_SCORE_THRESHOLD, Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import ALLOWED_CAMPAIGN_TRANSITIONS, CampaignStatus, ContactWorkflowState
from app.models.import_batch import ImportBatch
from app.services.audit import record_audit_event

# --- Field limits (defense in depth; also checked by the database where a
# column or constraint exists). Kept as module constants so the API layer and
# any future caller can reuse the exact same bounds instead of re-deriving them.
MAX_NAME_LEN = 255
MAX_DESCRIPTION_LEN = 4000
MAX_OFFER_LEN = 4000
MAX_TONE_LEN = 100
MAX_OWNER_LEN = 255
MAX_SOURCE_LEN = 255
MAX_SENDING_REFERENCE_LEN = 255
# Serialized-size cap for the structured JSON settings, to keep a draft
# campaign from becoming a dumping ground for unrelated data (AGENTS.md:
# "uploaded files must have explicit type and size limits" — the same
# principle applied to free-shaped campaign JSON).
MAX_JSON_BYTES = 20_000
MIN_SCORE_THRESHOLD_MIN = 0
MIN_SCORE_THRESHOLD_MAX = 100

_SETTINGS_FIELDS: Final[tuple[str, ...]] = (
    "offer",
    "audience_rules",
    "exclusions",
    "tone",
    "owner",
    "source",
    "sending_reference",
)


class CampaignError(Exception):
    """Raised when a campaign cannot be created or updated as requested.

    The message is always safe to show an operator: it names the offending
    field and the rule it broke, never a database error, a stack trace, or any
    internal identifier.
    """


class CampaignNotFound(Exception):
    """Raised when the target campaign does not exist."""


class _UnsetType:
    """Sentinel distinguishing an omitted update keyword from an explicit ``None``.

    ``update_campaign_settings`` uses this as every keyword's default so it can
    tell "the caller did not mention this field, leave it alone" apart from
    "the caller explicitly wants this nullable field cleared" (``None``). This
    is the update contract CMP-001 requires: a partial update must never
    silently erase a setting the caller simply did not mention.
    """

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return "UNSET"


UNSET: Final = _UnsetType()


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise CampaignError("campaign name is required")
    if len(cleaned) > MAX_NAME_LEN:
        raise CampaignError(f"campaign name must be {MAX_NAME_LEN} characters or fewer")
    return cleaned


def _clean_optional_text(value: str | None, *, field_name: str, max_len: int) -> str | None:
    """Trim optional text; a blank/whitespace-only value normalizes to ``None``."""

    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        raise CampaignError(f"{field_name} must be {max_len} characters or fewer")
    return cleaned


def _validate_json_object(
    value: dict[str, Any] | None, *, field_name: str
) -> dict[str, Any] | None:
    """Require a JSON object (or ``None``); reject unserializable or oversized input.

    Only the top-level shape is enforced (a JSON object, not a list/string/
    number, and not free text) so the field stays structured and readable
    without CMP-001 fixing a rule vocabulary — see ``app/models/campaign.py``.
    """

    if value is None:
        return None
    if not isinstance(value, dict):
        raise CampaignError(f"{field_name} must be a JSON object")
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise CampaignError(f"{field_name} must contain only JSON-serializable values") from exc
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise CampaignError(f"{field_name} is too large (max {MAX_JSON_BYTES} bytes)")
    return value


def _validate_min_score_threshold(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CampaignError("min_score_threshold must be a whole number between 0 and 100")
    if value < MIN_SCORE_THRESHOLD_MIN or value > MIN_SCORE_THRESHOLD_MAX:
        raise CampaignError(
            f"min_score_threshold must be between {MIN_SCORE_THRESHOLD_MIN} "
            f"and {MIN_SCORE_THRESHOLD_MAX}"
        )
    return value


def is_campaign_transition_allowed(current: CampaignStatus, target: CampaignStatus) -> bool:
    """Return True if moving from *current* to *target* is a permitted status change."""

    return target in ALLOWED_CAMPAIGN_TRANSITIONS.get(current, frozenset())


def create_campaign(
    session: Session,
    *,
    name: str,
    description: str | None = None,
    offer: str | None = None,
    audience_rules: dict[str, Any] | None = None,
    exclusions: dict[str, Any] | None = None,
    min_score_threshold: int = DEFAULT_MIN_SCORE_THRESHOLD,
    tone: str | None = None,
    owner: str | None = None,
    source: str | None = None,
    sending_reference: str | None = None,
    status: CampaignStatus = CampaignStatus.DRAFT,
    actor: str = "operator",
) -> Campaign:
    """Create and persist a draft campaign, recording an audit event.

    Only ``name`` is required; every other setting is optional and safe to
    omit (it is simply left unset), so a minimal campaign can still be created
    before the rest of its settings are known. Malformed or oversized values
    are rejected with a :class:`CampaignError` before anything is persisted.

    The campaign is added and flushed (so it receives its id) but not
    committed — the caller owns the transaction boundary.
    """

    cleaned_name = _clean_name(name)
    cleaned_description = _clean_optional_text(
        description, field_name="description", max_len=MAX_DESCRIPTION_LEN
    )
    cleaned_offer = _clean_optional_text(offer, field_name="offer", max_len=MAX_OFFER_LEN)
    cleaned_audience_rules = _validate_json_object(audience_rules, field_name="audience_rules")
    cleaned_exclusions = _validate_json_object(exclusions, field_name="exclusions")
    cleaned_threshold = _validate_min_score_threshold(min_score_threshold)
    cleaned_tone = _clean_optional_text(tone, field_name="tone", max_len=MAX_TONE_LEN)
    cleaned_owner = _clean_optional_text(owner, field_name="owner", max_len=MAX_OWNER_LEN)
    cleaned_source = _clean_optional_text(source, field_name="source", max_len=MAX_SOURCE_LEN)
    cleaned_sending_reference = _clean_optional_text(
        sending_reference, field_name="sending_reference", max_len=MAX_SENDING_REFERENCE_LEN
    )

    campaign = Campaign(
        name=cleaned_name,
        description=cleaned_description,
        offer=cleaned_offer,
        audience_rules=cleaned_audience_rules,
        exclusions=cleaned_exclusions,
        min_score_threshold=cleaned_threshold,
        tone=cleaned_tone,
        owner=cleaned_owner,
        source=cleaned_source,
        sending_reference=cleaned_sending_reference,
        status=status,
    )
    session.add(campaign)
    session.flush()

    present = sorted(f for f in _SETTINGS_FIELDS if getattr(campaign, f) is not None)
    record_audit_event(
        session,
        actor=actor,
        action="campaign.created",
        entity_type="campaign",
        entity_id=str(campaign.id),
        new_state=campaign.status.value,
        reason="campaign created",
        context={"settings_present": present},
    )
    return campaign


def update_campaign_settings(
    session: Session,
    campaign_id: uuid.UUID,
    *,
    name: str | _UnsetType = UNSET,
    description: str | None | _UnsetType = UNSET,
    offer: str | None | _UnsetType = UNSET,
    audience_rules: dict[str, Any] | None | _UnsetType = UNSET,
    exclusions: dict[str, Any] | None | _UnsetType = UNSET,
    min_score_threshold: int | _UnsetType = UNSET,
    tone: str | None | _UnsetType = UNSET,
    owner: str | None | _UnsetType = UNSET,
    source: str | None | _UnsetType = UNSET,
    sending_reference: str | None | _UnsetType = UNSET,
    status: CampaignStatus | _UnsetType = UNSET,
    actor: str = "operator",
    reason: str | None = None,
) -> Campaign:
    """Apply a partial update to a draft campaign's settings.

    Only fields explicitly passed are changed; an omitted keyword leaves the
    existing stored value untouched. For nullable fields, passing ``None``
    explicitly clears the stored value — this is intentionally different from
    omitting the keyword. A ``status`` change is validated against
    :data:`app.models.enums.ALLOWED_CAMPAIGN_TRANSITIONS`; requesting the
    campaign's current status is always a no-op, never an illegal transition.
    Malformed values are rejected with :class:`CampaignError` before anything
    is changed, so a rejected update never partially applies.

    Raises :class:`CampaignNotFound` if no campaign has ``campaign_id``. If
    nothing actually changes (every explicit value equals the stored value),
    the campaign is returned unchanged and no audit event is recorded.
    """

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFound(f"campaign {campaign_id} does not exist")

    changed: dict[str, Any] = {}

    if not isinstance(name, _UnsetType):
        cleaned_name = _clean_name(name)
        if cleaned_name != campaign.name:
            changed["name"] = cleaned_name

    if not isinstance(description, _UnsetType):
        cleaned_description = _clean_optional_text(
            description, field_name="description", max_len=MAX_DESCRIPTION_LEN
        )
        if cleaned_description != campaign.description:
            changed["description"] = cleaned_description

    if not isinstance(offer, _UnsetType):
        cleaned_offer = _clean_optional_text(offer, field_name="offer", max_len=MAX_OFFER_LEN)
        if cleaned_offer != campaign.offer:
            changed["offer"] = cleaned_offer

    if not isinstance(audience_rules, _UnsetType):
        cleaned_audience_rules = _validate_json_object(audience_rules, field_name="audience_rules")
        if cleaned_audience_rules != campaign.audience_rules:
            changed["audience_rules"] = cleaned_audience_rules

    if not isinstance(exclusions, _UnsetType):
        cleaned_exclusions = _validate_json_object(exclusions, field_name="exclusions")
        if cleaned_exclusions != campaign.exclusions:
            changed["exclusions"] = cleaned_exclusions

    if not isinstance(min_score_threshold, _UnsetType):
        cleaned_threshold = _validate_min_score_threshold(min_score_threshold)
        if cleaned_threshold != campaign.min_score_threshold:
            changed["min_score_threshold"] = cleaned_threshold

    if not isinstance(tone, _UnsetType):
        cleaned_tone = _clean_optional_text(tone, field_name="tone", max_len=MAX_TONE_LEN)
        if cleaned_tone != campaign.tone:
            changed["tone"] = cleaned_tone

    if not isinstance(owner, _UnsetType):
        cleaned_owner = _clean_optional_text(owner, field_name="owner", max_len=MAX_OWNER_LEN)
        if cleaned_owner != campaign.owner:
            changed["owner"] = cleaned_owner

    if not isinstance(source, _UnsetType):
        cleaned_source = _clean_optional_text(source, field_name="source", max_len=MAX_SOURCE_LEN)
        if cleaned_source != campaign.source:
            changed["source"] = cleaned_source

    if not isinstance(sending_reference, _UnsetType):
        cleaned_sending_reference = _clean_optional_text(
            sending_reference,
            field_name="sending_reference",
            max_len=MAX_SENDING_REFERENCE_LEN,
        )
        if cleaned_sending_reference != campaign.sending_reference:
            changed["sending_reference"] = cleaned_sending_reference

    previous_status = campaign.status
    status_changing = False
    if not isinstance(status, _UnsetType) and status != previous_status:
        if not is_campaign_transition_allowed(previous_status, status):
            raise CampaignError(
                f"cannot change campaign status from {previous_status.value!r} to {status.value!r}"
            )
        changed["status"] = status
        status_changing = True

    if not changed:
        return campaign

    for field_name, new_value in changed.items():
        setattr(campaign, field_name, new_value)
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="campaign.updated",
        entity_type="campaign",
        entity_id=str(campaign.id),
        previous_state=previous_status.value if status_changing else None,
        new_state=campaign.status.value if status_changing else None,
        reason=reason or "campaign settings updated",
        context={"fields_changed": sorted(changed.keys())},
    )
    return campaign


def get_campaign(session: Session, campaign_id: uuid.UUID) -> Campaign | None:
    """Fetch one campaign by id, or ``None`` if it does not exist."""

    return session.get(Campaign, campaign_id)


@dataclass
class CampaignOverview:
    """A campaign with the aggregate counts the workbench list shows."""

    campaign: Campaign
    contact_count: int = 0
    import_count: int = 0
    state_counts: dict[str, int] = field(default_factory=dict)


def list_campaigns(session: Session) -> list[CampaignOverview]:
    """All campaigns, newest first, with membership and import counts."""

    campaigns = session.scalars(select(Campaign).order_by(Campaign.created_at.desc())).all()

    member_counts: dict[uuid.UUID, int] = {
        campaign_id: count
        for campaign_id, count in session.execute(
            select(CampaignContact.campaign_id, func.count(CampaignContact.id)).group_by(
                CampaignContact.campaign_id
            )
        ).all()
    }
    import_counts: dict[uuid.UUID, int] = {
        campaign_id: count
        for campaign_id, count in session.execute(
            select(ImportBatch.campaign_id, func.count(ImportBatch.id)).group_by(
                ImportBatch.campaign_id
            )
        ).all()
    }
    return [
        CampaignOverview(
            campaign=c,
            contact_count=member_counts.get(c.id, 0),
            import_count=import_counts.get(c.id, 0),
        )
        for c in campaigns
    ]


def get_campaign_overview(session: Session, campaign_id: uuid.UUID) -> CampaignOverview | None:
    """One campaign with its per-state membership counts, or None."""

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        return None

    state_rows = session.execute(
        select(CampaignContact.state, func.count(CampaignContact.id))
        .where(CampaignContact.campaign_id == campaign_id)
        .group_by(CampaignContact.state)
    ).all()
    state_counts = {state.value: count for state, count in state_rows}
    return CampaignOverview(
        campaign=campaign,
        contact_count=sum(state_counts.values()),
        import_count=session.scalar(
            select(func.count(ImportBatch.id)).where(ImportBatch.campaign_id == campaign_id)
        )
        or 0,
        state_counts=state_counts,
    )


def campaign_imports(session: Session, campaign_id: uuid.UUID) -> list[ImportBatch]:
    """Import batches linked to one campaign, newest first."""

    return list(
        session.scalars(
            select(ImportBatch)
            .where(ImportBatch.campaign_id == campaign_id)
            .order_by(ImportBatch.created_at.desc())
        ).all()
    )


def campaign_members(
    session: Session,
    campaign_id: uuid.UUID,
    *,
    state: ContactWorkflowState | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[CampaignContact, Contact]], int]:
    """Paginated memberships (with contacts) for one campaign, newest first."""

    query = (
        select(CampaignContact, Contact)
        .join(Contact, Contact.id == CampaignContact.contact_id)
        .where(CampaignContact.campaign_id == campaign_id)
    )
    count_query = select(func.count(CampaignContact.id)).where(
        CampaignContact.campaign_id == campaign_id
    )
    if state is not None:
        query = query.where(CampaignContact.state == state)
        count_query = count_query.where(CampaignContact.state == state)
    total = session.scalar(count_query) or 0
    rows = session.execute(
        query.order_by(CampaignContact.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return [(membership, contact) for membership, contact in rows], total
