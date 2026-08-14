"""Global Agent Studio projection over the existing registry, controls and queue."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.campaign import Campaign
from app.services.agent_studio.extensions import (
    AgentStudioModule,
    StudioCapabilityModule,
    enabled_capability_modules,
    module_for,
)
from app.services.agents.registry import PIPELINE_ORDER
from app.services.operations import settings as operational
from app.services.workbench_agents.reader import PhaseTwoWorkbenchReader
from app.services.workbench_agents.views import AgentCardView, ControlView, JobView


@dataclass(frozen=True)
class AgentStudioCard:
    card: AgentCardView
    effective_control: ControlView
    module: AgentStudioModule
    recent_runs: tuple[JobView, ...]
    recent_failures: tuple[JobView, ...]

    @property
    def latest_run(self) -> JobView | None:
        return self.recent_runs[0] if self.recent_runs else None

    @property
    def health(self) -> str:
        if not self.card.control.implemented:
            return "unavailable"
        if not self.effective_control.accepting_work:
            return "controlled stop"
        if self.card.queue.terminal_failures:
            return "failing"
        if self.card.queue.retrying:
            return "degraded"
        if self.card.queue.running:
            return "running"
        return "healthy"


@dataclass(frozen=True)
class AgentStudioView:
    agents: tuple[AgentStudioCard, ...]
    campaigns: tuple[Campaign, ...]
    selected_campaign_id: uuid.UUID | None
    #: Operator modules that are not pipeline Agents. Separate from ``agents`` on
    #: purpose: these have no AgentIdentifier, no position in PIPELINE_ORDER, no
    #: Agent control and no Campaign Contact job, so folding them into the same
    #: tuple would misrepresent all four.
    capability_modules: tuple[StudioCapabilityModule, ...] = ()


def load_studio(session: Session, *, campaign_id: uuid.UUID | None = None) -> AgentStudioView:
    reader = PhaseTwoWorkbenchReader(session)
    overview = reader.overview()
    selected_campaign = session.get(Campaign, campaign_id) if campaign_id else None
    cards_by_id = {card.agent_id: card for card in overview.agents}
    cards: list[AgentStudioCard] = []
    for agent_id in PIPELINE_ORDER:
        card = cards_by_id[agent_id]
        detail = reader.agent_detail(
            agent_id,
            campaign_id=selected_campaign.id if selected_campaign else None,
        )
        assert detail is not None
        recent = reader.jobs(
            agent_id=agent_id,
            campaign_id=selected_campaign.id if selected_campaign else None,
            limit=5,
        ).jobs
        failures = reader.jobs(
            agent_id=agent_id,
            campaign_id=selected_campaign.id if selected_campaign else None,
            status="failed",
            limit=3,
        ).jobs
        cards.append(
            AgentStudioCard(
                card=card,
                effective_control=detail.effective_control,
                module=module_for(agent_id),
                recent_runs=recent,
                recent_failures=failures,
            )
        )
    campaigns = tuple(session.scalars(select(Campaign).order_by(Campaign.name, Campaign.id)).all())
    # Reads the effective flag list only — the administrator's settings, not the
    # environment's defaults, so a module turned on from the Admin Configuration
    # screen is listed here without a restart. No query is issued against any
    # non-Agent module's tables, so listing a module cannot load, lease or touch
    # its state.
    modules = enabled_capability_modules(
        operational.effective_flags(session, get_settings()).enabled()
    )
    return AgentStudioView(
        agents=tuple(cards),
        campaigns=campaigns,
        selected_campaign_id=selected_campaign.id if selected_campaign else None,
        capability_modules=modules,
    )
