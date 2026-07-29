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
        False,
        skippable=True,
    ),
    AgentSpec(
        AgentIdentifier.EMAIL,
        "Email Agent",
        4,
        (AgentIdentifier.RESEARCH,),
        AgentControlStatus.DISABLED,
        True,
    ),
    AgentSpec(
        AgentIdentifier.VERIFICATION,
        "Verification Agent",
        5,
        (AgentIdentifier.EMAIL,),
        AgentControlStatus.DISABLED,
        True,
    ),
    AgentSpec(
        AgentIdentifier.INSIGHTS,
        "Insights Agent",
        6,
        (AgentIdentifier.VERIFICATION,),
        AgentControlStatus.DISABLED,
        False,
        skippable=True,
    ),
    AgentSpec(
        AgentIdentifier.PERSONALIZATION,
        "Personalization Agent",
        7,
        (AgentIdentifier.INSIGHTS,),
        AgentControlStatus.DISABLED,
        False,
    ),
    AgentSpec(
        AgentIdentifier.SENDING,
        "Sending Agent",
        8,
        (AgentIdentifier.PERSONALIZATION,),
        AgentControlStatus.DISABLED,
        False,
        max_attempts=1,
    ),
)

AGENT_SPECS = {spec.identifier: spec for spec in _SPECS}
PIPELINE_ORDER = tuple(spec.identifier for spec in _SPECS)


def get_agent_spec(agent_id: AgentIdentifier) -> AgentSpec:
    try:
        return AGENT_SPECS[agent_id]
    except KeyError as exc:  # pragma: no cover - AgentIdentifier constrains callers
        raise ValueError(f"unregistered Agent {agent_id!r}") from exc


def next_agent(agent_id: AgentIdentifier) -> AgentIdentifier | None:
    position = PIPELINE_ORDER.index(agent_id)
    return PIPELINE_ORDER[position + 1] if position + 1 < len(PIPELINE_ORDER) else None


def agents_through(desired: AgentIdentifier) -> tuple[AgentIdentifier, ...]:
    return PIPELINE_ORDER[: PIPELINE_ORDER.index(desired) + 1]
