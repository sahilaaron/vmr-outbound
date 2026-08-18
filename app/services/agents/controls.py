"""Global defaults and Campaign-level Agent override precedence."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.agent import AgentControl, CampaignAgentOverride
from app.models.campaign import Campaign
from app.models.enums import AgentControlStatus, AgentIdentifier
from app.services.agents.registry import AGENT_SPECS, get_agent_spec
from app.services.audit import record_audit_event
from app.services.campaign_offering import consistency as offering_consistency

MAX_CONFIG_BYTES = 25_000

#: ``EffectiveAgentControl.source`` when the Campaign's master execution switch —
#: the operator-facing "Pause campaign" — is what turned the Agent off. It is
#: named because callers must be able to tell that disable apart from a
#: configured one: it is temporary and reversible, and nothing may treat it as a
#: standing decision that this Campaign does not use the stage.
CAMPAIGN_EXECUTION_SOURCE: Final = "campaign_execution"


class AgentControlError(Exception):
    """Safe operator-facing control validation error."""


def _config(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AgentControlError("Agent configuration must be a JSON object")
    try:
        encoded = json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise AgentControlError("Agent configuration must contain JSON values") from exc
    if len(encoded.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise AgentControlError(f"Agent configuration is too large (max {MAX_CONFIG_BYTES} bytes)")
    return value


def _ensure_implemented(agent_id: AgentIdentifier, status: AgentControlStatus) -> None:
    spec = get_agent_spec(agent_id)
    if agent_id is AgentIdentifier.CAPTURE and status is not AgentControlStatus.ENABLED:
        raise AgentControlError(
            "Capture cannot be paused or disabled because accepted intake must "
            "always preserve the permanent Contact and capture evidence"
        )
    if status is AgentControlStatus.ENABLED and not spec.implemented:
        raise AgentControlError(
            f"{spec.display_name} has no executable Phase 2 adapter and cannot be enabled"
        )


@dataclass(frozen=True)
class EffectiveAgentControl:
    agent_id: AgentIdentifier
    status: AgentControlStatus
    config: dict[str, Any]
    source: str
    reason: str | None
    global_version: int | None
    campaign_version: int | None

    def to_dict(self) -> dict[str, Any]:
        spec = get_agent_spec(self.agent_id)
        return {
            "agent_id": self.agent_id.value,
            "display_name": spec.display_name,
            "position": spec.position,
            "status": self.status.value,
            "implemented": spec.implemented,
            "source": self.source,
            "reason": self.reason,
            "config": self.config,
            "global_version": self.global_version,
            "campaign_version": self.campaign_version,
        }


# Distinguishes "leave the stored config alone" from "set it to empty".
# The Workbench changes an Agent's status without knowing anything about its
# config; before this existed, every pause or resume from the UI silently reset
# the config to {} — which for the Verification and language-model Agents meant
# quietly dropping ``{"live": true}`` and falling back to a refusal mid-Campaign.
_KEEP_CONFIG: Final = object()


def set_global_control(
    session: Session,
    *,
    agent_id: AgentIdentifier,
    status: AgentControlStatus,
    config: dict[str, Any] | None | Any = _KEEP_CONFIG,
    actor: str = "operator",
    reason: str | None = None,
) -> AgentControl:
    _ensure_implemented(agent_id, status)
    control = session.get(AgentControl, agent_id)
    clean_config = (
        dict(control.config or {})
        if config is _KEEP_CONFIG and control is not None
        else _config(None if config is _KEEP_CONFIG else config)
    )
    previous = None
    created = False
    if control is None:
        control = AgentControl(
            agent_id=agent_id,
            status=status,
            config=clean_config,
            version=1,
            reason=reason,
            updated_by=actor,
        )
        try:
            with session.begin_nested():
                session.add(control)
                session.flush()
        except IntegrityError:
            control = session.get(AgentControl, agent_id)
            if control is None:  # pragma: no cover - defensive
                raise
        else:
            created = True
    if not created:
        previous = control.status
        if control.status is status and control.config == clean_config and control.reason == reason:
            return control
        control.status = status
        control.config = clean_config
        control.reason = reason
        control.version += 1
        control.updated_by = actor
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="agent.global_control_updated",
        entity_type="agent_control",
        entity_id=agent_id.value,
        previous_state=previous.value if previous else None,
        new_state=status.value,
        reason=reason or "global Agent control updated",
        context={"version": control.version},
    )
    return control


def set_campaign_override(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    agent_id: AgentIdentifier,
    status: AgentControlStatus,
    config: dict[str, Any] | None | Any = _KEEP_CONFIG,
    actor: str = "operator",
    reason: str | None = None,
) -> CampaignAgentOverride:
    _ensure_implemented(agent_id, status)
    if session.get(Campaign, campaign_id) is None:
        raise AgentControlError(f"campaign {campaign_id} does not exist")
    override = session.scalars(
        select(CampaignAgentOverride).where(
            CampaignAgentOverride.campaign_id == campaign_id,
            CampaignAgentOverride.agent_id == agent_id,
        )
    ).one_or_none()
    clean_config = (
        dict(override.config or {})
        if config is _KEEP_CONFIG and override is not None
        else _config(None if config is _KEEP_CONFIG else config)
    )
    previous = None
    created = False
    if override is None:
        override = CampaignAgentOverride(
            campaign_id=campaign_id,
            agent_id=agent_id,
            status=status,
            config=clean_config,
            version=1,
            reason=reason,
            updated_by=actor,
        )
        try:
            with session.begin_nested():
                session.add(override)
                session.flush()
        except IntegrityError:
            override = session.scalars(
                select(CampaignAgentOverride).where(
                    CampaignAgentOverride.campaign_id == campaign_id,
                    CampaignAgentOverride.agent_id == agent_id,
                )
            ).one_or_none()
            if override is None:  # pragma: no cover - defensive
                raise
        else:
            created = True
    if not created:
        previous = override.status
        if (
            override.status is status
            and override.config == clean_config
            and override.reason == reason
        ):
            return override
        override.status = status
        override.config = clean_config
        override.reason = reason
        override.version += 1
        override.updated_by = actor
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="agent.campaign_override_updated",
        entity_type="campaign_agent_override",
        entity_id=str(override.id),
        previous_state=previous.value if previous else None,
        new_state=status.value,
        reason=reason or "Campaign Agent override updated",
        context={
            "campaign_id": str(campaign_id),
            "agent_id": agent_id.value,
            "version": override.version,
        },
    )
    return override


#: The one configuration key an Agent adapter reads as "this Campaign has asked
#: for real work". Research, Verification, Insights and Personalization each
#: refuse to execute until the *effective* config carries it — see
#: ``AgentSpec.requires_live_opt_in``.
LIVE_CONFIG_KEY: Final = "live"


def campaign_live_opt_in(
    session: Session,
    *,
    campaign: Campaign,
    agent_id: AgentIdentifier,
) -> bool:
    """Whether this Campaign's effective Agent config permits live execution."""

    control = effective_control(session, campaign=campaign, agent_id=agent_id)
    return control.config.get(LIVE_CONFIG_KEY) is True


def set_campaign_live_opt_in(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    agent_id: AgentIdentifier,
    live: bool,
    actor: str = "operator",
    reason: str | None = None,
) -> CampaignAgentOverride:
    """Grant or withdraw one Campaign's live opt-in for one Agent.

    Deliberately *not* a second control surface. It writes the same
    ``CampaignAgentOverride`` row :func:`set_campaign_override` writes, through
    the same function, with the same version, audit event and validation — the
    only thing it decides is one key in ``config``.

    Three properties are the whole design:

    * **Status is carried, never chosen.** An Agent's status and its
      configuration are separate decisions, so an opt-in must not double as a
      pause or a resume. The status written is the one this Campaign already had:
      the override's if it has one, otherwise the status it inherits from the
      global control or the registry default. The temporary DISABLED that
      ``effective_control`` reports while a Campaign's execution switch is off is
      deliberately *not* read here — persisting it would turn a reversible pause
      into a stored decision.
    * **Nothing else in the config is touched.** Research's ``workers``,
      ``worker_options`` and ``claude_fallback`` keys survive an opt-in being
      turned on and off again.
    * **Turning it off writes ``false`` rather than deleting the key.** Removing
      it would silently re-inherit a global ``live`` if one is ever set, which is
      the opposite of what an operator who switched this Campaign off asked for.
      It also discards nothing: jobs, evidence and stage history are untouched,
      and the Agent simply refuses the next execution as it did before.
    """

    spec = get_agent_spec(agent_id)
    if not spec.requires_live_opt_in:
        raise AgentControlError(
            f"{spec.display_name} has no live opt-in; it does not read that configuration."
        )
    if session.get(Campaign, campaign_id) is None:
        raise AgentControlError(f"campaign {campaign_id} does not exist")

    override = session.scalars(
        select(CampaignAgentOverride).where(
            CampaignAgentOverride.campaign_id == campaign_id,
            CampaignAgentOverride.agent_id == agent_id,
        )
    ).one_or_none()
    if override is not None:
        status = override.status
        config = dict(override.config or {})
    else:
        global_control = session.get(AgentControl, agent_id)
        status = global_control.status if global_control else spec.default_status
        config = {}
    config[LIVE_CONFIG_KEY] = live

    return set_campaign_override(
        session,
        campaign_id=campaign_id,
        agent_id=agent_id,
        status=status,
        config=config,
        actor=actor,
        reason=reason,
    )


def clear_campaign_override(
    session: Session,
    *,
    campaign_id: uuid.UUID,
    agent_id: AgentIdentifier,
    actor: str = "operator",
) -> bool:
    override = session.scalars(
        select(CampaignAgentOverride).where(
            CampaignAgentOverride.campaign_id == campaign_id,
            CampaignAgentOverride.agent_id == agent_id,
        )
    ).one_or_none()
    if override is None:
        return False
    override_id = override.id
    previous = override.status
    session.delete(override)
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="agent.campaign_override_cleared",
        entity_type="campaign_agent_override",
        entity_id=str(override_id),
        previous_state=previous.value,
        reason="Campaign Agent override cleared; global/default control now applies",
        context={"campaign_id": str(campaign_id), "agent_id": agent_id.value},
    )
    return True


def effective_control(
    session: Session,
    *,
    campaign: Campaign,
    agent_id: AgentIdentifier,
) -> EffectiveAgentControl:
    spec = get_agent_spec(agent_id)
    global_control = session.get(AgentControl, agent_id)
    override = session.scalars(
        select(CampaignAgentOverride).where(
            CampaignAgentOverride.campaign_id == campaign.id,
            CampaignAgentOverride.agent_id == agent_id,
        )
    ).one_or_none()

    base_status = global_control.status if global_control else spec.default_status
    config = dict(global_control.config or {}) if global_control else {}
    source = "global" if global_control else "registry_default"
    reason = global_control.reason if global_control else None
    if override is not None:
        base_status = override.status
        config.update(override.config or {})
        source = "campaign_override"
        reason = override.reason

    if not campaign.execution_enabled and agent_id is not AgentIdentifier.CAPTURE:
        base_status = AgentControlStatus.DISABLED
        source = CAMPAIGN_EXECUTION_SOURCE
        reason = campaign.disabled_reason or "Campaign execution is disabled"
    elif base_status is AgentControlStatus.ENABLED and (
        hold := offering_consistency.holds_agent(session, campaign=campaign, agent_id=agent_id)
    ):
        # A Campaign preparing its offering from a URL holds the two stages whose
        # output depends on what it is selling, so it cannot pitch the Library
        # offering to the first fifty contacts and the researched one to the rest.
        #
        # PAUSED rather than DISABLED, and the difference is the whole point: a
        # pause projects onto the stage reversibly and is released by the ordinary
        # control reconciliation the moment a version becomes current. DISABLED
        # would invite ``schedule_next`` to auto-skip a *skippable* stage —
        # Insights is one — and SKIPPED is absorbing, so a Campaign would come
        # back from a two-minute wait having permanently discarded the stage it
        # was waiting for.
        #
        # It is also checked only when the Campaign has not otherwise turned the
        # Agent off: an operator's own pause or disable is a decision, and a
        # temporary hold must not overwrite the reason an operator sees.
        base_status = AgentControlStatus.PAUSED
        source = offering_consistency.OFFERING_RESEARCH_SOURCE
        reason = hold
    elif not spec.implemented and base_status is AgentControlStatus.ENABLED:
        base_status = AgentControlStatus.DISABLED
        source = "registry"
        reason = "No executable adapter is registered"

    return EffectiveAgentControl(
        agent_id=agent_id,
        status=base_status,
        config=config,
        source=source,
        reason=reason,
        global_version=global_control.version if global_control else None,
        campaign_version=override.version if override else None,
    )


def all_effective_controls(session: Session, campaign: Campaign) -> list[EffectiveAgentControl]:
    return [
        effective_control(session, campaign=campaign, agent_id=agent_id) for agent_id in AGENT_SPECS
    ]
