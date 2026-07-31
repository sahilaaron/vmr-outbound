"""Small typed registry of Agent-specific Studio capabilities.

This is intentionally not a plug-in runtime.  The authoritative Agent registry
continues to define execution.  This map only tells the Admin presentation layer
which dedicated inspection/configuration/test page exists for each registered
Agent and which unavailable states must be shown truthfully.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import AgentIdentifier


@dataclass(frozen=True)
class StudioCapabilities:
    inspection: bool
    configuration: bool
    preview_testing: bool
    live_execution: bool
    reporting: bool


@dataclass(frozen=True)
class AgentStudioModule:
    agent_id: AgentIdentifier
    capabilities: StudioCapabilities
    dedicated_path: str
    configuration_boundary: str
    preview_boundary: str


def _module(
    agent_id: AgentIdentifier,
    *,
    configuration: bool = False,
    preview: bool = False,
    live: bool = True,
    reporting: bool = False,
    configuration_boundary: str = "No editable Agent-specific configuration is available.",
    preview_boundary: str = "No side-effect-free preview is implemented for this Agent.",
) -> AgentStudioModule:
    suffix = agent_id.value
    return AgentStudioModule(
        agent_id=agent_id,
        capabilities=StudioCapabilities(
            inspection=True,
            configuration=configuration,
            preview_testing=preview,
            live_execution=live,
            reporting=reporting,
        ),
        dedicated_path=f"/admin/agents/studio/{suffix}",
        configuration_boundary=configuration_boundary,
        preview_boundary=preview_boundary,
    )


AGENT_STUDIO_MODULES: dict[AgentIdentifier, AgentStudioModule] = {
    AgentIdentifier.CAPTURE: _module(AgentIdentifier.CAPTURE),
    AgentIdentifier.IDENTITY: _module(AgentIdentifier.IDENTITY),
    AgentIdentifier.COMPANY: _module(AgentIdentifier.COMPANY),
    AgentIdentifier.RESEARCH: _module(
        AgentIdentifier.RESEARCH,
        reporting=True,
        configuration_boundary=(
            "Research worker and collection-rule editing are intentionally unavailable."
        ),
    ),
    AgentIdentifier.EMAIL: _module(AgentIdentifier.EMAIL),
    AgentIdentifier.VERIFICATION: _module(AgentIdentifier.VERIFICATION, reporting=True),
    AgentIdentifier.INSIGHTS: _module(AgentIdentifier.INSIGHTS, reporting=True),
    AgentIdentifier.PERSONALIZATION: _module(
        AgentIdentifier.PERSONALIZATION,
        configuration=True,
        preview=True,
        reporting=True,
        configuration_boundary=(
            "Versioned Personalization policy only. Agent execution authority remains in "
            "the existing global control and Campaign override services."
        ),
        preview_boundary=(
            "Preview reads persisted inputs and invokes the bounded thinking seam without "
            "creating a job, DraftVersion, approval or send."
        ),
    ),
    AgentIdentifier.SENDING: _module(
        AgentIdentifier.SENDING,
        live=False,
        configuration_boundary="Sending has no production adapter and remains disabled.",
        preview_boundary="Sending tests are unavailable because no production adapter exists.",
    ),
}


def module_for(agent_id: AgentIdentifier) -> AgentStudioModule:
    return AGENT_STUDIO_MODULES[agent_id]
