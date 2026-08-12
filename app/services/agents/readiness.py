"""Whether a Campaign can be resumed without losing work to a terminal skip.

The trap this exists to close
----------------------------
A disabled *skippable* Agent is not held — it is stepped over, permanently. The
walk in ``orchestrator.schedule_next`` moves the stage to ``SKIPPED``
(``reason_code="control_disabled_autoskip"``), and ``SKIPPED`` is an absorbing
state: ``app/services/pipeline.py`` gives it an empty outgoing transition set,
``schedule_next`` returns early for it, and the operator re-run path does not
list it as a stopped stage. Enabling the Agent an hour later therefore does
nothing at all for the contacts that already went past it.

That auto-skip is deliberate and worth keeping — without it an operator would
have to skip a disabled stage by hand for every contact in a campaign. What was
missing is any check that the operator had finished configuring *before* the
walk starts, and the walk starts for every contact in the campaign at once when
execution is switched on.

The distinction this module draws
---------------------------------
Two different failures, and only one of them is worth refusing over:

*Blocking* — a skippable Agent that is disabled. Resuming burns it terminally
for every contact that reaches it, and nothing recovers it. Research, Insights
and Personalization are the three skippable Agents, which is exactly the set the
seven-message workflow depends on: skip Personalization and the campaign
produces no messages at all, silently, while reporting every contact complete.

*Holding* — a non-skippable Agent that is disabled or paused. Work waits for it
and resumes when it is enabled. Nothing is lost, so this is reported rather than
refused; an operator running a deliberately partial pipeline is doing something
legitimate.

Why the check is scoped to sequence campaigns
---------------------------------------------
Refusing on the blocking condition changes behaviour that campaigns not opted in
to sequences have relied on, so it applies only where the brief asked for it:
a Campaign that has opted in to the seven-message workflow. Everything else
keeps the behaviour it had.

Why controls are re-resolved here
---------------------------------
``effective_control`` reports every Agent as disabled with
``source="campaign_execution"`` while execution is off — which is the state this
check runs in. Asking it directly would therefore say "everything is disabled"
and refuse every resume. The question that matters is what the controls will be
*once execution is on*, so the campaign-execution override is the one layer this
module skips. Every other layer — registry default, global control, campaign
override, unimplemented adapter — is resolved exactly as ``effective_control``
resolves it, and the two must not drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent import AgentControl, CampaignAgentOverride
from app.models.campaign import Campaign, CampaignContact
from app.models.enums import AgentControlStatus, AgentIdentifier
from app.services.agents.registry import AGENT_SPECS, PIPELINE_ORDER, get_agent_spec


@dataclass(frozen=True)
class AgentReadiness:
    """One Agent's resolved state, as it would be with execution switched on."""

    agent_id: AgentIdentifier
    display_name: str
    status: AgentControlStatus
    skippable: bool


@dataclass(frozen=True)
class ExecutionReadiness:
    """What resuming this Campaign would do to its pipeline right now."""

    blocking: tuple[AgentReadiness, ...]
    holding: tuple[AgentReadiness, ...]

    @property
    def runnable(self) -> bool:
        """Whether a resume can proceed without terminally skipping a stage."""

        return not self.blocking

    def refusal_message(self) -> str:
        """One sentence naming exactly what must be enabled, and why.

        Written for the operator who pressed Resume, so it says what to do and
        what would otherwise be lost rather than reporting a status code. The
        Agent names come from the registry rather than being spelled out here,
        so this cannot drift from what the pipeline actually runs.
        """

        names = ", ".join(entry.display_name for entry in self.blocking)
        plural = "s are" if len(self.blocking) > 1 else " is"
        return (
            f"This campaign generates a seven-message sequence, and the {names} "
            f"Agent{plural} switched off. Starting now would step past that stage "
            "permanently for every contact that reaches it — a skipped stage cannot "
            "be re-run once the campaign has moved past it, so enabling the Agent "
            "afterwards would not recover those contacts. Ask a platform "
            "administrator to enable it, then start the campaign."
        )


def _resolved_status(
    session: Session,
    *,
    campaign: Campaign,
    agent_id: AgentIdentifier,
) -> AgentControlStatus:
    """The status this Agent would carry once campaign execution is on.

    Mirrors ``controls.effective_control`` with the campaign-execution override
    deliberately omitted — see the module docstring.
    """

    spec = get_agent_spec(agent_id)
    global_control = session.get(AgentControl, agent_id)
    override = session.scalars(
        select(CampaignAgentOverride).where(
            CampaignAgentOverride.campaign_id == campaign.id,
            CampaignAgentOverride.agent_id == agent_id,
        )
    ).one_or_none()

    status = global_control.status if global_control else spec.default_status
    if override is not None:
        status = override.status
    if not spec.implemented and status is AgentControlStatus.ENABLED:
        return AgentControlStatus.DISABLED
    return status


def _furthest_desired_stage(session: Session, campaign: Campaign) -> AgentIdentifier | None:
    """The last stage any contact in this Campaign is meant to reach.

    A stage no contact is enrolled through cannot be skipped, so complaining
    about it would be a refusal the operator cannot act on and does not need to.
    ``None`` means the Campaign has no contacts yet — there is nothing to lose,
    so nothing to refuse.
    """

    stages = set(
        session.scalars(
            select(CampaignContact.desired_stage).where(CampaignContact.campaign_id == campaign.id)
        ).all()
    )
    if not stages:
        return None
    return max(stages, key=lambda stage: PIPELINE_ORDER.index(stage))


def execution_readiness(session: Session, *, campaign: Campaign) -> ExecutionReadiness:
    """Classify every in-scope Agent as blocking, holding, or fine.

    Capture is excluded: it is never auto-skipped (``schedule_next`` names it
    explicitly) and it is what makes a person exist at all.
    """

    furthest = _furthest_desired_stage(session, campaign)
    if furthest is None:
        return ExecutionReadiness(blocking=(), holding=())
    limit = PIPELINE_ORDER.index(furthest)

    blocking: list[AgentReadiness] = []
    holding: list[AgentReadiness] = []
    for agent_id in PIPELINE_ORDER:
        spec = AGENT_SPECS[agent_id]
        if agent_id is AgentIdentifier.CAPTURE or spec.position > limit:
            continue
        # An Agent with no adapter can never be enabled, so refusing on it would
        # be a permanent refusal with no operator remedy. Sending is the one
        # that matters here, and it must stay disabled.
        if not spec.implemented:
            continue
        status = _resolved_status(session, campaign=campaign, agent_id=agent_id)
        if status is AgentControlStatus.ENABLED:
            continue
        entry = AgentReadiness(
            agent_id=agent_id,
            display_name=spec.display_name,
            status=status,
            skippable=spec.skippable,
        )
        # Only a *disabled* skippable stage is stepped over. A paused one waits
        # for a human, which is recoverable and therefore not a refusal.
        if spec.skippable and status is AgentControlStatus.DISABLED:
            blocking.append(entry)
        else:
            holding.append(entry)

    return ExecutionReadiness(blocking=tuple(blocking), holding=tuple(holding))
