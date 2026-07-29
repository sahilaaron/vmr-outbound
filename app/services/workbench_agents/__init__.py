"""The operator Workbench Agent surface: a projection over Phase 2.

The Workbench owns no execution vocabulary. Phase 2 owns the Agent registry,
execution states, job lifecycle, controls, Campaign overrides, pipeline stages,
retry semantics, and the event vocabulary; this package only *projects* those
authoritative objects into immutable view models a template can render, and
routes operator intent back through the Phase 2 service layer.

Three modules, three responsibilities:

* :mod:`app.services.workbench_agents.views` — frozen view models. Presentation shapes
  with no SQLAlchemy in them. They are DTOs, never a second source of truth: no
  Workbench code decides what a state *means*, only how to show it.
* :mod:`app.services.workbench_agents.reader` — the read model. One narrow
  :class:`~app.services.workbench_agents.reader.WorkbenchReader` port plus the Phase 2
  implementation that fills it from the real registry, controls, jobs,
  memberships and pipeline events.
* :mod:`app.services.workbench_agents.commands` — the command path. Every operator
  action calls a Phase 2 service; this layer adds only what a UI needs on top:
  an optimistic-concurrency guard against stale control versions, sanitized
  failure text, and a truthful outcome to display.

Nothing here writes to the database directly, and no route or template may. The
Workbench cannot invent a state, cannot advance a stage, and cannot relax a gate:
a stage is complete only when Phase 2 committed a domain outcome and a pipeline
event to say so.
"""

from __future__ import annotations

from app.services.workbench_agents.commands import (
    CommandOutcome,
    WorkbenchCommandError,
    WorkbenchCommands,
)
from app.services.workbench_agents.reader import PhaseTwoWorkbenchReader, WorkbenchReader
from app.services.workbench_agents.views import (
    AgentCardView,
    AgentDetailView,
    CampaignExecutionView,
    ContactExecutionView,
    DraftOutcomeView,
    EmailCandidateAttemptView,
    EmailOutcomeView,
    InsightsOutcomeView,
    JobListView,
    JobView,
    PipelineEventView,
    QueueCounts,
    ResearchOutcomeView,
    StageView,
    WorkbenchOverviewView,
)

__all__ = [
    "AgentCardView",
    "AgentDetailView",
    "CampaignExecutionView",
    "CommandOutcome",
    "ContactExecutionView",
    "EmailCandidateAttemptView",
    "DraftOutcomeView",
    "EmailOutcomeView",
    "JobListView",
    "InsightsOutcomeView",
    "JobView",
    "PhaseTwoWorkbenchReader",
    "PipelineEventView",
    "QueueCounts",
    "ResearchOutcomeView",
    "StageView",
    "WorkbenchCommandError",
    "WorkbenchCommands",
    "WorkbenchOverviewView",
    "WorkbenchReader",
]
