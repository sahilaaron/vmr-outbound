"""Small typed registry of Studio capabilities.

This is intentionally not a plug-in runtime.  The authoritative Agent registry
continues to define execution.  These maps only tell the Admin presentation layer
which dedicated inspection/configuration/test page exists and which unavailable
states must be shown truthfully.

Two registries live here, and the split is the point:

``AGENT_STUDIO_MODULES`` is keyed by :class:`AgentIdentifier` and covers the nine
registered pipeline Agents.

``STUDIO_CAPABILITY_MODULES`` covers operator areas that are **not** Agents.  An
area belongs here when it is real operator work that an Admin reasonably expects
to reach from the Studio, but it does not run as a Campaign Contact pipeline
stage, has no ``AgentIdentifier``, and is not in ``PIPELINE_ORDER``.  Modelling
those as Agents to get a Studio tile would be exactly backwards: the tile is
presentation, the identifier is execution authority, and inventing an identifier
to satisfy a link would put a company-scoped area into per-Contact pipeline
ordering where it does not belong.

Nothing in either registry executes anything.  Both describe where an operator
can go and what the boundaries are once they get there.
"""

from __future__ import annotations

from collections.abc import Iterable
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
    AgentIdentifier.CAPTURE: _module(
        AgentIdentifier.CAPTURE,
        reporting=True,
        configuration_boundary=(
            "Capture source, promotion and filing lineage is read-only; intake, suppression, "
            "deduplication and Campaign enrollment services remain authoritative."
        ),
        preview_boundary=(
            "No retrospective match, promotion, filing, retry, replay or Identity job runs from "
            "Studio reports."
        ),
    ),
    AgentIdentifier.IDENTITY: _module(AgentIdentifier.IDENTITY),
    AgentIdentifier.COMPANY: _module(
        AgentIdentifier.COMPANY,
        reporting=True,
        configuration_boundary=(
            "Company identity and domain decisions are read-only here; audited resolution "
            "services remain authoritative."
        ),
        preview_boundary=("No retrospective matching or provider lookup runs from Studio reports."),
    ),
    AgentIdentifier.RESEARCH: _module(
        AgentIdentifier.RESEARCH,
        reporting=True,
        configuration_boundary=(
            "Research worker and collection-rule editing are intentionally unavailable."
        ),
    ),
    AgentIdentifier.EMAIL: _module(
        AgentIdentifier.EMAIL,
        configuration=True,
        reporting=True,
        configuration_boundary=(
            "Versioned candidate-pattern policy only; execution authority stays in Agent controls."
        ),
    ),
    AgentIdentifier.VERIFICATION: _module(
        AgentIdentifier.VERIFICATION,
        configuration=True,
        preview=True,
        reporting=True,
        configuration_boundary=(
            "Provider credentials and immutable waterfall policy only; job authority is unchanged."
        ),
        preview_boundary=(
            "Explicit one-address provider tests record agent_studio usage but create no job "
            "or evidence."
        ),
    ),
    AgentIdentifier.INSIGHTS: _module(
        AgentIdentifier.INSIGHTS,
        reporting=True,
        configuration_boundary=(
            "Claim and Employee Size derivation is append-only; manual rewriting is unavailable."
        ),
        preview_boundary=(
            "No separate preview or model call exists; execution uses only committed Research "
            "evidence through the authoritative Insights job."
        ),
    ),
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


# ---------------------------------------------------------------------------
# Non-Agent Studio capability modules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StudioSurface:
    """One existing Admin page this module links out to.

    A link, not a projection.  Studio does not re-render the owning area's data,
    so there is no second read model to keep in step with the first.
    """

    label: str
    path: str
    description: str


@dataclass(frozen=True)
class StudioDistinction:
    """One neighbouring area this module is routinely confused with."""

    name: str
    difference: str


@dataclass(frozen=True)
class StudioCapabilityModule:
    """An Admin Studio module that is deliberately not a pipeline Agent."""

    key: str
    display_name: str
    #: What the module is, in the operator's terms.
    summary: str
    #: The feature flag that must be on for the owning area to exist at all.
    #: While it is off the owning router is not mounted, so linking to it would
    #: produce a 404 -- the module is therefore hidden rather than shown broken.
    feature_flag: str
    #: Where "open this module" goes.  An existing route owned by that area, never
    #: a new Studio route.
    entry_path: str
    surfaces: tuple[StudioSurface, ...]
    #: How this module's work is executed.  Stated because the whole reason it is
    #: not an Agent is that it does not run on the Campaign Contact queue.
    execution: str
    scope: str
    #: What Studio may and may not do here.
    boundary: str
    distinctions: tuple[StudioDistinction, ...]


COMPANY_INTELLIGENCE_MODULE = StudioCapabilityModule(
    key="company-intelligence",
    display_name="Company Intelligence",
    summary=(
        "Versioned, evidence-linked classification of what a Company is -- industry, "
        "specialty, geography, business model -- derived from Research evidence that "
        "has already been committed, and reviewed by an operator."
    ),
    feature_flag="company_intelligence",
    entry_path="/admin/company-intelligence",
    surfaces=(
        StudioSurface(
            label="Review queue",
            path="/admin/company-intelligence",
            description=(
                "Companies with produced classifications awaiting operator judgement, "
                "including unresolved values and open conflicts."
            ),
        ),
        StudioSurface(
            label="Vocabulary browser",
            path="/admin/company-intelligence/taxonomy",
            description=(
                "The controlled vocabularies and their editions. A new edition is a new "
                "row, so a classification stored under an older vocabulary keeps resolving."
            ),
        ),
        StudioSurface(
            label="Backfill console",
            path="/admin/company-intelligence/backfill",
            description=(
                "Bounded, resumable backfill runs. Preview never enqueues, and a skip "
                "always carries a truthful reason code."
            ),
        ),
    ),
    execution=(
        "Its own durable company-scoped queue and its own standalone worker "
        "(scripts/run_company_intelligence_worker.py). It is not on the Campaign "
        "Contact Agent queue and no Agent control governs it."
    ),
    scope="Company. Never a Contact, never a Campaign Contact, never a Campaign.",
    boundary=(
        "Studio links to the owning Admin pages and does not duplicate their read, "
        "review, queue, taxonomy or persistence systems. Opening a Studio view reads "
        "nothing from Company Intelligence and writes nothing to it: production, "
        "operator decisions, alias promotion and backfill all stay on the owning "
        "routes. Company Intelligence does not feed Personalization and is not a "
        "Sending dependency."
    ),
    distinctions=(
        StudioDistinction(
            name="Company Agent",
            difference=(
                "Resolves identity and domain -- which real company this is, and which "
                "website is authoritative for it. It answers WHO the company is. It is a "
                "registered pipeline Agent."
            ),
        ),
        StudioDistinction(
            name="Research Agent",
            difference=(
                "Collects and commits evidence about a company from its website. It "
                "answers WHAT WAS FOUND, and it does not classify. It is a registered "
                "pipeline Agent."
            ),
        ),
        StudioDistinction(
            name="Company Intelligence",
            difference=(
                "Classifies committed Research evidence into structured, versioned, "
                "reviewable values. It answers WHAT THE COMPANY IS, and it produces no "
                "evidence of its own. It is not an Agent and has no AgentIdentifier."
            ),
        ),
    ),
)


#: Every non-Agent Studio module, in display order.
STUDIO_CAPABILITY_MODULES: tuple[StudioCapabilityModule, ...] = (COMPANY_INTELLIGENCE_MODULE,)


def enabled_capability_modules(
    enabled_features: Iterable[str],
) -> tuple[StudioCapabilityModule, ...]:
    """The non-Agent modules whose owning area currently exists.

    A module whose flag is off is omitted rather than shown disabled. That is not
    cosmetic: while the flag is off the owning router is never mounted, so every
    path the module advertises returns 404, and a tile linking into a 404 is a
    worse answer than no tile.
    """

    names = frozenset(enabled_features)
    return tuple(module for module in STUDIO_CAPABILITY_MODULES if module.feature_flag in names)
