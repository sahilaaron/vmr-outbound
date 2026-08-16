"""Stable registry for the operator-visible outbound Agents."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import AgentControlStatus, AgentIdentifier


@dataclass(frozen=True)
class AgentSpec:
    identifier: AgentIdentifier
    display_name: str
    position: int
    dependencies: tuple[AgentIdentifier, ...]
    default_status: AgentControlStatus
    implemented: bool
    skippable: bool = False
    max_attempts: int = 3
    retry_base_seconds: float = 30.0
    retry_cap_seconds: float = 900.0
    #: Whether this Agent refuses to execute until the Campaign's effective Agent
    #: configuration contains ``{"live": true}``.
    #:
    #: A registry *fact*, not a setting: it records that the adapter itself asks
    #: for a per-Campaign opt-in before it will reach a provider, fetch another
    #: organisation's website, or spend model budget. Enabling the Agent is not
    #: enough, which is exactly why it needs to be visible — an Agent shown as
    #: enabled while every job it claims returns ``research_not_live`` is a
    #: screen telling an operator something untrue.
    #:
    #: The adapters remain the authority for their own refusal; this flag only
    #: says which of them have one. ``tests/test_campaign_live_opt_in.py`` pins
    #: the two together so the flag cannot drift from the code that enforces it.
    requires_live_opt_in: bool = False
    #: Whether this Agent is part of *preparing* the outbound package.
    #:
    #: The product is autonomous until Ready for Sending: preparation runs on its
    #: own, and sending is a separate, manual, human action. That boundary needs
    #: to exist somewhere the pipeline can read, because without it the walk kept
    #: going — a Contact whose seven messages were written advanced into Sending,
    #: found it disabled and not skippable, and parked there permanently while
    #: the finished package sat next to it.
    #:
    #: So Sending stays stage 9 for the registry, the Workbench and stage
    #: history, and stops being an execution prerequisite. Preparation ends at
    #: the last preparation Agent; what happens after that is not this pipeline's
    #: to schedule.
    preparation: bool = True


_SPECS = (
    AgentSpec(
        AgentIdentifier.CAPTURE,
        "Capture Agent",
        0,
        (),
        AgentControlStatus.ENABLED,
        True,
        max_attempts=1,
    ),
    AgentSpec(
        AgentIdentifier.IDENTITY,
        "Identity Agent",
        1,
        (AgentIdentifier.CAPTURE,),
        AgentControlStatus.ENABLED,
        True,
    ),
    AgentSpec(
        AgentIdentifier.COMPANY,
        "Company Agent",
        2,
        (AgentIdentifier.IDENTITY,),
        AgentControlStatus.ENABLED,
        True,
    ),
    AgentSpec(
        AgentIdentifier.RESEARCH,
        "Research Agent",
        3,
        (AgentIdentifier.COMPANY,),
        AgentControlStatus.DISABLED,
        True,
        skippable=True,
        # A language-model call is slow and occasionally flaky. Two retries is
        # generous enough to ride out a transient failure and mean enough that a
        # genuinely stuck Contact stops rather than looping.
        max_attempts=3,
        retry_base_seconds=60.0,
        requires_live_opt_in=True,
    ),
    AgentSpec(
        AgentIdentifier.EMAIL,
        "Email Agent",
        4,
        (AgentIdentifier.RESEARCH,),
        AgentControlStatus.DISABLED,
        True,
        # One execution may yield once per Verification child and resume after
        # each committed decision. Three candidates therefore require four
        # claims even without a worker restart; this budget also leaves bounded
        # room for lease recovery without confusing candidate count with worker
        # attempt count.
        max_attempts=8,
    ),
    AgentSpec(
        AgentIdentifier.VERIFICATION,
        "Verification Agent",
        5,
        (AgentIdentifier.EMAIL,),
        AgentControlStatus.DISABLED,
        True,
        requires_live_opt_in=True,
    ),
    AgentSpec(
        AgentIdentifier.INSIGHTS,
        "Insights Agent",
        6,
        (AgentIdentifier.VERIFICATION,),
        AgentControlStatus.DISABLED,
        True,
        skippable=True,
        max_attempts=3,
        retry_base_seconds=60.0,
        requires_live_opt_in=True,
    ),
    AgentSpec(
        AgentIdentifier.PERSONALIZATION,
        "Personalization Agent",
        7,
        (AgentIdentifier.INSIGHTS,),
        AgentControlStatus.DISABLED,
        True,
        # Skippable for explicit operator recovery. Thin prospect evidence may
        # now produce the policy's earnest offering-led fallback; skipping still
        # means no draft exists and Sending must treat that as "do not send".
        skippable=True,
        max_attempts=3,
        retry_base_seconds=60.0,
        requires_live_opt_in=True,
    ),
    AgentSpec(
        AgentIdentifier.SENDING,
        "Sending Agent",
        8,
        (AgentIdentifier.PERSONALIZATION,),
        AgentControlStatus.DISABLED,
        False,
        max_attempts=1,
        preparation=False,
    ),
)

AGENT_SPECS = {spec.identifier: spec for spec in _SPECS}
PIPELINE_ORDER = tuple(spec.identifier for spec in _SPECS)

#: Every Agent that refuses to run without the Campaign's live opt-in, in pipeline
#: order. Derived from the specs rather than written out a second time.
LIVE_OPT_IN_AGENTS: tuple[AgentIdentifier, ...] = tuple(
    spec.identifier for spec in _SPECS if spec.requires_live_opt_in
)

#: Every Agent a Contact must pass through to reach Ready for Sending, in
#: pipeline order. Derived from ``AgentSpec.preparation`` rather than listed a
#: second time, so an Agent added to the pipeline is in this set by default and
#: excluding one is a deliberate edit to its spec.
PREPARATION_AGENTS: tuple[AgentIdentifier, ...] = tuple(
    spec.identifier for spec in _SPECS if spec.preparation
)

#: Every Agent that is *not* preparation. Sending is the only member today, and
#: naming the set rather than the Agent keeps callers from hardcoding it.
NON_PREPARATION_AGENTS: tuple[AgentIdentifier, ...] = tuple(
    spec.identifier for spec in _SPECS if not spec.preparation
)

#: Where preparation stops. Completing this Agent completes the pipeline.
PREPARATION_TERMINAL_AGENT: AgentIdentifier = PREPARATION_AGENTS[-1]

#: Every Agent that has no executable adapter. A stage sitting at ``DISABLED`` on
#: one of these is parked permanently rather than waiting: ``controls`` refuses
#: to enable an Agent with nothing to run, so no operator action exists that
#: would move it. Callers projecting a truthful status need to tell that apart
#: from a stage an administrator can simply switch back on.
AGENTS_WITHOUT_ADAPTER: tuple[AgentIdentifier, ...] = tuple(
    spec.identifier for spec in _SPECS if not spec.implemented
)


def get_agent_spec(agent_id: AgentIdentifier) -> AgentSpec:
    try:
        return AGENT_SPECS[agent_id]
    except KeyError as exc:  # pragma: no cover - AgentIdentifier constrains callers
        raise ValueError(f"unregistered Agent {agent_id!r}") from exc


def next_agent(agent_id: AgentIdentifier) -> AgentIdentifier | None:
    position = PIPELINE_ORDER.index(agent_id)
    return PIPELINE_ORDER[position + 1] if position + 1 < len(PIPELINE_ORDER) else None


def next_preparation_agent(agent_id: AgentIdentifier) -> AgentIdentifier | None:
    """The next Agent the *preparation* pipeline runs, or ``None`` at the boundary.

    Distinct from :func:`next_agent`, which answers "what is stage N+1" and is
    still the right question for the registry, the Workbench and stage history.
    This answers "what does the pipeline run next", and the two differ at exactly
    one place: after the last preparation Agent there is no next stage to run,
    because sending is a manual action rather than queued work.
    """

    following = next_agent(agent_id)
    if following is None or not get_agent_spec(following).preparation:
        return None
    return following


def agents_through(desired: AgentIdentifier) -> tuple[AgentIdentifier, ...]:
    return PIPELINE_ORDER[: PIPELINE_ORDER.index(desired) + 1]
