"""Campaign operating-context service.

Campaigns hold context and controls. Permanent Contact, Company, offering, and
evidence records remain reusable references outside the Campaign. Every write
here is transactional, validated, and audited; callers own the commit.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, TypeVar

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignContact
from app.models.contact import Contact
from app.models.enums import (
    CampaignContactEligibility,
    CampaignMembershipStatus,
    CampaignStatus,
    ContactWorkflowState,
    PipelineStageStatus,
)
from app.models.import_batch import ImportBatch
from app.models.seller_knowledge import CampaignOffering
from app.services.audit import record_audit_event

MAX_NAME_LEN = 255
MAX_DESCRIPTION_LEN = 4_000
MAX_DIRECTION_LEN = 8_000
MAX_CTA_LEN = 2_000
MAX_JSON_BYTES = 50_000
CAMPAIGN_RECONCILE_BATCH_SIZE = 100
DEADLOCK_RETRY_ATTEMPTS = 3
POSTGRES_DEADLOCK_SQLSTATE = "40P01"

T = TypeVar("T")

_JSON_FIELDS: Final = frozenset(
    {
        "sender_context",
        "target_audience",
        "template_config",
        "cadence_config",
        "sending_settings",
    }
)
_TEXT_LIMITS: Final = {
    "description": MAX_DESCRIPTION_LEN,
    "messaging_direction": MAX_DIRECTION_LEN,
    "primary_cta": MAX_CTA_LEN,
}
_SETTING_FIELDS: Final = tuple(sorted(_JSON_FIELDS | set(_TEXT_LIMITS)))


class CampaignError(Exception):
    """Safe operator-facing Campaign validation error."""


class CampaignNotFound(CampaignError):
    """The requested Campaign does not exist."""


class CampaignConcurrencyError(CampaignError):
    """A Campaign control could not finish after bounded deadlock recovery."""


class CampaignPersistenceError(CampaignError):
    """A database failure that is safe to expose without its raw traceback."""


class _Unset:
    pass


UNSET: Final = _Unset()


def _name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise CampaignError("campaign name is required")
    if len(cleaned) > MAX_NAME_LEN:
        raise CampaignError(f"campaign name must be {MAX_NAME_LEN} characters or fewer")
    return cleaned


def _optional_text(value: str | None, *, field_name: str, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > limit:
        raise CampaignError(f"{field_name} must be {limit} characters or fewer")
    return cleaned


def _json_object(value: dict[str, Any] | None, *, field_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CampaignError(f"{field_name} must be a JSON object")
    try:
        encoded = json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise CampaignError(f"{field_name} must contain JSON-serializable values") from exc
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise CampaignError(f"{field_name} is too large (max {MAX_JSON_BYTES} bytes)")
    return value


def _validate_status_change(current: CampaignStatus, target: CampaignStatus) -> None:
    if current is target:
        return
    allowed = {
        CampaignStatus.DRAFT: {CampaignStatus.ACTIVE, CampaignStatus.ARCHIVED},
        CampaignStatus.ACTIVE: {CampaignStatus.DRAFT, CampaignStatus.ARCHIVED},
        CampaignStatus.ARCHIVED: set(),
    }
    if target not in allowed[current]:
        raise CampaignError(
            f"cannot change campaign status from {current.value!r} to {target.value!r}"
        )


def create_campaign(
    session: Session,
    *,
    name: str,
    description: str | None = None,
    status: CampaignStatus = CampaignStatus.DRAFT,
    sender_context: dict[str, Any] | None = None,
    target_audience: dict[str, Any] | None = None,
    messaging_direction: str | None = None,
    primary_cta: str | None = None,
    template_config: dict[str, Any] | None = None,
    cadence_config: dict[str, Any] | None = None,
    sending_settings: dict[str, Any] | None = None,
    actor: str = "operator",
) -> Campaign:
    """Create a Campaign shell and validated operating context."""

    campaign = Campaign(
        name=_name(name),
        description=_optional_text(
            description, field_name="description", limit=MAX_DESCRIPTION_LEN
        ),
        status=status,
        sender_context=_json_object(sender_context, field_name="sender_context"),
        target_audience=_json_object(target_audience, field_name="target_audience"),
        messaging_direction=_optional_text(
            messaging_direction,
            field_name="messaging_direction",
            limit=MAX_DIRECTION_LEN,
        ),
        primary_cta=_optional_text(primary_cta, field_name="primary_cta", limit=MAX_CTA_LEN),
        template_config=_json_object(template_config, field_name="template_config"),
        cadence_config=_json_object(cadence_config, field_name="cadence_config"),
        sending_settings=_json_object(sending_settings, field_name="sending_settings"),
        execution_enabled=False,
        settings_version=1,
    )
    try:
        with session.begin_nested():
            session.add(campaign)
            session.flush()
    except IntegrityError as exc:
        raise CampaignError(f"a campaign named {campaign.name!r} already exists") from exc
    record_audit_event(
        session,
        actor=actor,
        action="campaign.created",
        entity_type="campaign",
        entity_id=str(campaign.id),
        new_state=campaign.status.value,
        reason="campaign created",
        context={
            "settings_present": [
                name for name in _SETTING_FIELDS if getattr(campaign, name) is not None
            ],
            "execution_enabled": False,
        },
    )
    return campaign


def update_campaign(
    session: Session,
    campaign_id: uuid.UUID,
    *,
    name: str | _Unset = UNSET,
    description: str | None | _Unset = UNSET,
    status: CampaignStatus | _Unset = UNSET,
    sender_context: dict[str, Any] | None | _Unset = UNSET,
    target_audience: dict[str, Any] | None | _Unset = UNSET,
    messaging_direction: str | None | _Unset = UNSET,
    primary_cta: str | None | _Unset = UNSET,
    template_config: dict[str, Any] | None | _Unset = UNSET,
    cadence_config: dict[str, Any] | None | _Unset = UNSET,
    sending_settings: dict[str, Any] | None | _Unset = UNSET,
    allow_provisional_domains: bool | _Unset = UNSET,
    actor: str = "operator",
    reason: str | None = None,
) -> Campaign:
    """Apply an explicit partial update without erasing omitted settings."""

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFound(f"campaign {campaign_id} does not exist")

    proposed: dict[str, Any] = {}
    if not isinstance(name, _Unset):
        proposed["name"] = _name(name)
    if not isinstance(description, _Unset):
        proposed["description"] = _optional_text(
            description, field_name="description", limit=MAX_DESCRIPTION_LEN
        )
    if not isinstance(messaging_direction, _Unset):
        proposed["messaging_direction"] = _optional_text(
            messaging_direction,
            field_name="messaging_direction",
            limit=MAX_DIRECTION_LEN,
        )
    if not isinstance(primary_cta, _Unset):
        proposed["primary_cta"] = _optional_text(
            primary_cta, field_name="primary_cta", limit=MAX_CTA_LEN
        )
    if not isinstance(allow_provisional_domains, _Unset):
        proposed["allow_provisional_domains"] = bool(allow_provisional_domains)
    if not isinstance(sender_context, _Unset):
        proposed["sender_context"] = _json_object(sender_context, field_name="sender_context")
    if not isinstance(target_audience, _Unset):
        proposed["target_audience"] = _json_object(target_audience, field_name="target_audience")
    if not isinstance(template_config, _Unset):
        proposed["template_config"] = _json_object(template_config, field_name="template_config")
    if not isinstance(cadence_config, _Unset):
        proposed["cadence_config"] = _json_object(cadence_config, field_name="cadence_config")
    if not isinstance(sending_settings, _Unset):
        proposed["sending_settings"] = _json_object(sending_settings, field_name="sending_settings")

    previous_status = campaign.status
    if not isinstance(status, _Unset):
        _validate_status_change(previous_status, status)
        proposed["status"] = status
        if status in {CampaignStatus.DRAFT, CampaignStatus.ARCHIVED} and (
            campaign.execution_enabled or status is CampaignStatus.ARCHIVED
        ):
            proposed["execution_enabled"] = False
            proposed["disabled_at"] = datetime.now(UTC)
            proposed["disabled_reason"] = reason or (
                "campaign archived"
                if status is CampaignStatus.ARCHIVED
                else "campaign returned to draft"
            )

    changed = {key: value for key, value in proposed.items() if getattr(campaign, key) != value}
    if not changed:
        return campaign
    try:
        with session.begin_nested():
            for key, value in changed.items():
                setattr(campaign, key, value)
            campaign.settings_version += 1
            session.flush()
    except IntegrityError as exc:
        if "name" in changed:
            raise CampaignError(f"a campaign named {changed['name']!r} already exists") from exc
        raise CampaignError("campaign update conflicts with existing data") from exc
    record_audit_event(
        session,
        actor=actor,
        action="campaign.updated",
        entity_type="campaign",
        entity_id=str(campaign.id),
        previous_state=previous_status.value if "status" in changed else None,
        new_state=campaign.status.value if "status" in changed else None,
        reason=reason or "campaign settings updated",
        context={
            "fields_changed": sorted(changed),
            "settings_version": campaign.settings_version,
        },
    )
    if campaign.status is CampaignStatus.ARCHIVED or "execution_enabled" in changed:
        _reconcile_campaign_controls(session, campaign.id, actor=actor)
    return campaign


# Historical branch/API name retained for callers that already adopted it.
update_campaign_settings = update_campaign


def _reconcile_campaign_controls(
    session: Session,
    campaign_id: uuid.UUID,
    *,
    actor: str,
    campaign_contact_ids: tuple[uuid.UUID, ...] = (),
) -> None:
    """Project the Campaign master switch onto durable Agent work."""

    # Local imports avoid the campaigns <-> orchestrator module cycle.
    from app.models.enums import AgentIdentifier
    from app.services.agents.orchestrator import reconcile_agent_control

    for agent_id in AgentIdentifier:
        if agent_id is AgentIdentifier.CAPTURE:
            continue
        reconcile_agent_control(
            session,
            campaign_id=campaign_id,
            campaign_contact_ids=campaign_contact_ids or None,
            agent_id=agent_id,
            actor=actor,
        )


def set_campaign_execution(
    session: Session,
    campaign_id: uuid.UUID,
    *,
    enabled: bool,
    actor: str = "operator",
    reason: str | None = None,
    reconcile: bool = True,
) -> Campaign:
    """Enable or disable new Agent execution without deleting queued history.

    Callers that own a request transaction may pass ``reconcile=False`` and use
    :func:`apply_campaign_execution` to commit the authoritative switch first,
    then project it in bounded transactions.
    """

    campaign = session.scalars(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update()
    ).one_or_none()
    if campaign is None:
        raise CampaignNotFound(f"campaign {campaign_id} does not exist")
    if enabled and campaign.status is CampaignStatus.ARCHIVED:
        raise CampaignError("an archived campaign cannot be enabled")
    if campaign.execution_enabled is enabled:
        if reconcile:
            _reconcile_campaign_controls(session, campaign.id, actor=actor)
        return campaign

    now = datetime.now(UTC)
    previous = campaign.execution_enabled
    campaign.execution_enabled = enabled
    if enabled:
        if campaign.status is CampaignStatus.DRAFT:
            campaign.status = CampaignStatus.ACTIVE
        campaign.enabled_at = now
        campaign.disabled_at = None
        campaign.disabled_reason = None
    else:
        campaign.disabled_at = now
        campaign.disabled_reason = reason or "disabled by operator"
    campaign.settings_version += 1
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="campaign.execution_enabled" if enabled else "campaign.execution_disabled",
        entity_type="campaign",
        entity_id=str(campaign.id),
        previous_state=str(previous).lower(),
        new_state=str(enabled).lower(),
        reason=reason or ("campaign enabled" if enabled else "campaign disabled"),
    )
    if reconcile:
        _reconcile_campaign_controls(session, campaign.id, actor=actor)
    return campaign


def is_postgresql_deadlock(error: BaseException) -> bool:
    """Return true only for PostgreSQL SQLSTATE ``40P01``.

    SQLAlchemy, psycopg, and test doubles expose the state on slightly different
    objects.  Walking only the explicit DBAPI/cause chain keeps classification
    narrow; message text is intentionally ignored.
    """

    pending: list[BaseException | object] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        sqlstate = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        diagnostic = getattr(current, "diag", None)
        if (
            sqlstate == POSTGRES_DEADLOCK_SQLSTATE
            or getattr(diagnostic, "sqlstate", None) == POSTGRES_DEADLOCK_SQLSTATE
        ):
            return True
        for attribute in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attribute, None)
            if nested is not None:
                pending.append(nested)
    return False


def _commit_with_deadlock_retry(
    session: Session,
    operation: Callable[[], T],
    *,
    attempts: int = DEADLOCK_RETRY_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> T:
    """Run and commit one bounded transaction with narrow deadlock recovery."""

    if attempts < 1:
        raise ValueError("deadlock retry attempts must be positive")
    for attempt in range(1, attempts + 1):
        try:
            result = operation()
            session.commit()
            return result
        except OperationalError as exc:
            session.rollback()
            if not is_postgresql_deadlock(exc):
                raise CampaignPersistenceError(
                    "The Campaign control could not be saved because the database operation "
                    "failed. Nothing was retried automatically."
                ) from exc
            if attempt >= attempts:
                raise CampaignConcurrencyError(
                    "The Campaign control could not finish because concurrent database work "
                    "kept colliding. Try the operation again."
                ) from exc
            # Tens of milliseconds are enough to stop the same requests from
            # immediately choosing the same victim again without making the button
            # feel unresponsive.
            sleep(jitter(0.02 * attempt, 0.05 * attempt))
        except Exception:
            session.rollback()
            raise
    raise AssertionError("deadlock retry loop did not return or raise")


@dataclass(frozen=True)
class _ReconcileBatch:
    cursor: uuid.UUID | None
    done: bool
    superseded: bool = False


def apply_campaign_execution(
    session: Session,
    campaign_id: uuid.UUID,
    *,
    enabled: bool,
    actor: str = "operator",
    reason: str | None = None,
    batch_size: int = CAMPAIGN_RECONCILE_BATCH_SIZE,
    retry_attempts: int = DEADLOCK_RETRY_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> Campaign:
    """Commit the master switch, then reconcile bounded Contact batches.

    Once the first transaction commits, worker lease eligibility reads the master
    switch directly.  Projection can therefore use short transactions without a
    window in which a worker may lease prohibited work.  A newer Pause/Resume
    version supersedes older remaining batches; the newer request converges every
    row to the latest effective control.
    """

    safe_batch_size = max(1, min(batch_size, 500))

    def switch() -> int:
        campaign = set_campaign_execution(
            session,
            campaign_id,
            enabled=enabled,
            actor=actor,
            reason=reason,
            reconcile=False,
        )
        return campaign.settings_version

    version = _commit_with_deadlock_retry(
        session,
        switch,
        attempts=retry_attempts,
        sleep=sleep,
        jitter=jitter,
    )
    cursor: uuid.UUID | None = None
    while True:

        def reconcile_batch(batch_cursor: uuid.UUID | None = cursor) -> _ReconcileBatch:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise CampaignNotFound(f"campaign {campaign_id} does not exist")
            if campaign.settings_version != version or campaign.execution_enabled is not enabled:
                return _ReconcileBatch(cursor=batch_cursor, done=True, superseded=True)
            statement = select(CampaignContact.id).where(CampaignContact.campaign_id == campaign_id)
            if batch_cursor is not None:
                statement = statement.where(CampaignContact.id > batch_cursor)
            identifiers = tuple(
                session.scalars(statement.order_by(CampaignContact.id).limit(safe_batch_size)).all()
            )
            if not identifiers:
                return _ReconcileBatch(cursor=batch_cursor, done=True)
            _reconcile_campaign_controls(
                session,
                campaign_id,
                actor=actor,
                campaign_contact_ids=identifiers,
            )
            return _ReconcileBatch(
                cursor=identifiers[-1],
                done=len(identifiers) < safe_batch_size,
            )

        batch = _commit_with_deadlock_retry(
            session,
            reconcile_batch,
            attempts=retry_attempts,
            sleep=sleep,
            jitter=jitter,
        )
        cursor = batch.cursor
        if batch.done:
            break

    session.expire_all()
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:  # pragma: no cover - protected by transaction/FK
        raise CampaignNotFound(f"campaign {campaign_id} does not exist")
    return campaign


def get_campaign(session: Session, campaign_id: uuid.UUID) -> Campaign | None:
    return session.get(Campaign, campaign_id)


@dataclass
class CampaignOverview:
    campaign: Campaign
    contact_count: int = 0
    import_count: int = 0
    state_counts: dict[str, int] = field(default_factory=dict)
    pipeline_counts: dict[str, int] = field(default_factory=dict)


def list_campaigns(session: Session) -> list[CampaignOverview]:
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
            campaign=campaign,
            contact_count=member_counts.get(campaign.id, 0),
            import_count=import_counts.get(campaign.id, 0),
        )
        for campaign in campaigns
    ]


def get_campaign_overview(session: Session, campaign_id: uuid.UUID) -> CampaignOverview | None:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        return None
    legacy_rows = session.execute(
        select(CampaignContact.state, func.count(CampaignContact.id))
        .where(CampaignContact.campaign_id == campaign_id)
        .group_by(CampaignContact.state)
    ).all()
    state_counts = {state.value: count for state, count in legacy_rows}
    pipeline_rows = session.execute(
        select(CampaignContact.pipeline_status, func.count(CampaignContact.id))
        .where(CampaignContact.campaign_id == campaign_id)
        .group_by(CampaignContact.pipeline_status)
    ).all()
    return CampaignOverview(
        campaign=campaign,
        contact_count=sum(state_counts.values()),
        import_count=session.scalar(
            select(func.count(ImportBatch.id)).where(ImportBatch.campaign_id == campaign_id)
        )
        or 0,
        state_counts=state_counts,
        pipeline_counts={state.value: count for state, count in pipeline_rows},
    )


@dataclass(frozen=True)
class CampaignOperatingState:
    campaign: Campaign
    offering_ids: tuple[uuid.UUID, ...]
    agent_controls: tuple[dict[str, Any], ...]


def campaign_operating_state(
    session: Session, campaign_id: uuid.UUID
) -> CampaignOperatingState | None:
    """Read settings plus fully resolved effective Agent controls."""

    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        return None
    offering_ids = tuple(
        session.scalars(
            select(CampaignOffering.offering_id)
            .where(CampaignOffering.campaign_id == campaign_id)
            .order_by(CampaignOffering.created_at)
        ).all()
    )
    # Local import avoids a model/service cycle.
    from app.services.agents.controls import all_effective_controls

    controls = tuple(control.to_dict() for control in all_effective_controls(session, campaign))
    return CampaignOperatingState(
        campaign=campaign,
        offering_ids=offering_ids,
        agent_controls=controls,
    )


def campaign_imports(session: Session, campaign_id: uuid.UUID) -> list[ImportBatch]:
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
    membership_status: CampaignMembershipStatus | None = None,
    pipeline_status: PipelineStageStatus | None = None,
    eligibility_status: CampaignContactEligibility | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[CampaignContact, Contact]], int]:
    """Efficient, filtered Campaign audience read path."""

    query = (
        select(CampaignContact, Contact)
        .join(Contact, Contact.id == CampaignContact.contact_id)
        .where(CampaignContact.campaign_id == campaign_id)
    )
    count_query = select(func.count(CampaignContact.id)).where(
        CampaignContact.campaign_id == campaign_id
    )
    filters = []
    if state is not None:
        filters.append(CampaignContact.state == state)
    if membership_status is not None:
        filters.append(CampaignContact.membership_status == membership_status)
    if pipeline_status is not None:
        filters.append(CampaignContact.pipeline_status == pipeline_status)
    if eligibility_status is not None:
        filters.append(CampaignContact.eligibility_status == eligibility_status)
    if filters:
        query = query.where(*filters)
        count_query = count_query.where(*filters)
    total = session.scalar(count_query) or 0
    rows = session.execute(
        query.order_by(CampaignContact.enrolled_at.desc()).limit(limit).offset(offset)
    ).all()
    return [(membership, contact) for membership, contact in rows], total
