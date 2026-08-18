"""When Campaign preparation must wait for the offering to be settled.

The defect this exists to prevent is mixed messaging inside one Campaign: an
operator elects "research this URL", presses Start, and the first fifty contacts
get seven emails pitching the Library offering while the last fifty get the
researched one. Both halves are individually correct and the Campaign as a whole
is incoherent, which is worse than either.

The contract is deliberately the smallest one that removes that, and it is
stated as a question about *the Campaign's current version*, not about whether a
job is running:

* **Library mode** — never holds. Nothing changed for a Campaign that did not ask
  for this.
* **URL mode with a current READY version** — never holds, including while a
  re-analysis is in flight. Every contact prepared in that window uses the same
  version, which is exactly the property being protected. Blocking here would
  stop a working Campaign to wait for an improvement it already has a
  substitute for.
* **URL mode with no current version and a run still going** — holds. This is
  the real case: the answer is coming, and starting without it is what produces
  the split.
* **URL mode with no current version and nothing running** — does not hold. Every
  run failed or was cancelled, the Campaign falls back to its Library offering,
  and it does so for every contact equally. A hold here would be a Campaign that
  can never run, which is the failure mode ``docs/GOAL.md`` calls a broken
  Campaign.

**What holds, and what does not.** Only the two stages that read offering context
to produce copy — Insights and Personalization — are held, through
``effective_control`` in :mod:`app.services.agents.controls`. Capture, Identity,
Company, Research, Email and Verification are untouched: they are about the
recipient, not about what we are selling, and stopping them would delay work the
offering cannot change. Reading a Campaign, listing its people and every other
read-only behaviour is untouched by construction — this is consulted only where a
stage is about to be queued.

The hold is a *pause*, never a disable. It projects onto the stage reversibly,
survives a restart, and is released by the ordinary control reconciliation the
moment a version becomes current — see :func:`reconcile_offering_hold`.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.enums import AgentIdentifier, CampaignOfferingSource
from app.services.campaign_offering import jobs
from app.services.operations import settings as operational

#: ``EffectiveAgentControl.source`` when this hold is what paused the Agent.
#: Named so the Workbench, diagnostics and reconciliation can tell it apart from
#: an operator's pause and from the Campaign's own execution switch.
OFFERING_RESEARCH_SOURCE = "campaign_offering_research"

#: The administrator control this whole feature sits behind.
FEATURE_KEY = "campaign_offering_research"

#: The stages whose output depends on what the Campaign is selling. Insights
#: chooses which recipient facts matter *to this offering*; Personalization
#: writes the copy. Everything else in the pipeline is about the recipient.
OFFERING_DEPENDENT_AGENTS: tuple[AgentIdentifier, ...] = (
    AgentIdentifier.INSIGHTS,
    AgentIdentifier.PERSONALIZATION,
)

HOLD_REASON = (
    "This Campaign is preparing its offering from a web address. "
    "Emails wait until that is ready, so everyone in the Campaign is pitched the same thing."
)


def offering_context_hold(session: Session, campaign: Campaign) -> str | None:
    """The reason offering-dependent preparation must wait, or ``None``.

    Cheap and read-only: at most two indexed single-row lookups, and it returns
    on the first one for every Campaign that is not in URL mode — which is every
    Campaign that existed before this feature.
    """

    if campaign.offering_source is not CampaignOfferingSource.URL_RESEARCH:
        return None
    if jobs.current_version(session, campaign_id=campaign.id) is not None:
        return None
    if not operational.enabled(session, FEATURE_KEY):
        # The control was switched off while this Campaign was waiting. Nothing
        # is coming — the runner refuses a claimed run for the same reason — so
        # holding would be a Campaign that can never run and whose only control
        # for it is no longer on the screen. It falls back to its Library
        # offering, consistently, exactly as a failed read does.
        return None
    if jobs.active_run(session, campaign_id=campaign.id) is None:
        # Nothing is coming. The Campaign falls back to its Library offering,
        # consistently, rather than waiting for an answer that will not arrive.
        return None
    return HOLD_REASON


def holds_agent(session: Session, *, campaign: Campaign, agent_id: AgentIdentifier) -> str | None:
    """The hold reason for one Agent, or ``None`` when this stage is unaffected."""

    if agent_id not in OFFERING_DEPENDENT_AGENTS:
        return None
    return offering_context_hold(session, campaign)


def reconcile_offering_hold(session: Session, *, campaign: Campaign, actor: str = "system") -> int:
    """Project this Campaign's current offering hold — or its absence — onto its work.

    Called at all four moments the answer to :func:`offering_context_hold`
    changes: a read is requested, a version becomes current, the last run fails,
    and the Campaign returns to its Library offering. It runs in both directions
    deliberately, because both are needed and neither is the interesting one on
    its own — asking for a read has to stop work already standing at Insights,
    and a version arriving has to start it again.

    It is the ordinary control reconciliation, not a second mechanism: paused
    jobs whose reason was a control return to ``PENDING``, memberships standing
    at a held stage are scheduled again, and a job paused for a *domain* reason
    keeps its own reason. That reuse is the point — a bespoke resume here would
    be a second thing to keep in step with the first.

    The import is local: ``orchestrator`` imports ``controls``, and ``controls``
    imports this module for the hold itself. Deferring it keeps that a
    dependency rather than a cycle.
    """

    from app.services.agents import orchestrator

    changed = 0
    for agent_id in OFFERING_DEPENDENT_AGENTS:
        changed += orchestrator.reconcile_agent_control(
            session,
            agent_id=agent_id,
            campaign_id=campaign.id,
            actor=actor,
        )
    return changed
