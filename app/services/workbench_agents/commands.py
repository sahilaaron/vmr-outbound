"""Operator commands, routed through the Phase 2 service layer.

Every action an operator can take from the Workbench arrives here and leaves
through a Phase 2 service. This layer never writes a row, never decides a state,
and never relaxes a gate. It adds exactly four things a UI needs on top of the
services:

1. **An optimistic-concurrency guard.** Control writes in Phase 2 carry a
   monotonic ``version``. A page rendered five minutes ago carries the version it
   saw; if the stored version has moved on, the command is refused with an
   explanation instead of silently overwriting a newer decision.
2. **A truthful outcome.** :class:`CommandOutcome` says whether Phase 2 accepted
   the command, what it did to work already in flight, and — when it declined —
   the service's own reason. A refusal is a normal, displayable answer.
3. **Sanitized text.** Service errors can quote a provider URL or a connection
   string; nothing reaches a page unredacted.
4. **An audit trail for the operator's intent**, accepted or refused, on top of
   whatever the Phase 2 service records for the state change itself.

What is deliberately *not* here: a single-job pause. Phase 2 can pause one job
(``jobs.mark_paused``) but has no single-job resume, so exposing it would let an
operator strand work with no way back. Pausing and resuming a Contact's work goes
through ``campaign_contacts.pause_membership`` / ``resume_membership``, which own
the reversible pair; cancelling a stage's work goes through
``pipeline.skip_current_stage``, which owns the cancel semantics and the event
that explains it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent import AgentControl, CampaignAgentOverride
from app.models.campaign import Campaign, CampaignContact
from app.models.enums import AgentControlStatus, AgentIdentifier
from app.services.agents import controls as agent_controls
from app.services.agents import jobs as agent_jobs
from app.services.agents.orchestrator import reconcile_agent_control
from app.services.agents.registry import get_agent_spec
from app.services.audit import record_audit_event
from app.services.campaign_contacts import (
    CampaignContactError,
    CampaignContactNotFound,
    pause_membership,
    resume_membership,
    retry_processing,
)
from app.services.pipeline import PipelineStateError, skip_current_stage
from app.services.workbench_agents.sanitize import sanitize_text

OPERATOR_ACTOR = "operator"


class WorkbenchCommandError(Exception):
    """A command the Workbench could not even form (unknown id, bad input)."""


@dataclass(frozen=True)
class CommandOutcome:
    """What actually happened, never what was asked for."""

    action: str
    accepted: bool
    message: str
    refusal_reason: str | None = None
    #: What the command did to work already running or queued, in plain words.
    in_flight_note: str | None = None
    #: Jobs Phase 2 reconciled as a direct result of the control change.
    reconciled_jobs: int = 0
    entity_id: str | None = None

    @property
    def summary(self) -> str:
        parts = [self.message]
        if self.in_flight_note:
            parts.append(self.in_flight_note)
        return " ".join(parts)


_IN_FLIGHT_NOTES: dict[AgentControlStatus, str] = {
    AgentControlStatus.ENABLED: (
        "Work this control had paused is released for claiming again; "
        "work paused by a domain block keeps its own reason."
    ),
    AgentControlStatus.PAUSED: "Claimable and leased work is held at paused; nothing is discarded.",
    # This note used to end at "nothing is discarded", which was false in the one
    # case an operator most needs it to be true. Disabling an Agent reconciles
    # every Campaign Contact standing at it, and a *skippable* Agent's disabled
    # stage was auto-skipped there — terminally, since SKIPPED has no outgoing
    # transition. One click therefore discarded the stage for the whole cohort
    # while promising the opposite. Reconciliation now holds that work instead,
    # so the first sentence is true; the second says plainly what disabling still
    # does to a Contact that arrives afterwards, rather than leaving the operator
    # to discover it.
    AgentControlStatus.DISABLED: (
        "No new work is claimed and in-flight work is held at paused; work already "
        "at this Agent holds there and resumes when it is enabled again. A Contact "
        "that reaches a skippable Agent while it is disabled is still stepped over, "
        "and that skip is permanent."
    ),
}


class WorkbenchCommands:
    """The Workbench's command surface over Phase 2."""

    def __init__(self, session: Session, *, actor: str = OPERATOR_ACTOR) -> None:
        self._session = session
        self._actor = actor

    # --- concurrency ------------------------------------------------------

    def _global_version(self, agent_id: AgentIdentifier) -> int | None:
        control = self._session.get(AgentControl, agent_id)
        return control.version if control else None

    def _campaign_version(self, campaign_id: uuid.UUID, agent_id: AgentIdentifier) -> int | None:
        override = self._session.scalars(
            select(CampaignAgentOverride).where(
                CampaignAgentOverride.campaign_id == campaign_id,
                CampaignAgentOverride.agent_id == agent_id,
            )
        ).one_or_none()
        return override.version if override else None

    @staticmethod
    def _stale(current: int | None, expected: int | None) -> bool:
        """Whether the page the operator acted from is out of date.

        ``expected is None`` means the page saw no stored control at all. That is
        still a claim about the world: if a control exists now, someone created
        it after the page rendered, and the operator has not seen it.
        """

        return current != expected

    def _stale_outcome(
        self, action: str, *, current: int | None, expected: int | None
    ) -> CommandOutcome:
        seen = "no stored control" if expected is None else f"version {expected}"
        now = "no stored control" if current is None else f"version {current}"
        return CommandOutcome(
            action=action,
            accepted=False,
            message="This control changed while the page was open. Nothing was applied.",
            refusal_reason=(
                f"The screen was showing {seen}; the stored control is now {now}. "
                "Reload to see the current state before deciding again."
            ),
        )

    # --- audit ------------------------------------------------------------

    def _audit(
        self, outcome: CommandOutcome, *, reason: str | None, context: dict[str, Any]
    ) -> None:
        record_audit_event(
            self._session,
            actor=self._actor,
            action=f"workbench.{outcome.action}",
            entity_type="workbench_command",
            entity_id=outcome.entity_id,
            new_state="accepted" if outcome.accepted else "refused",
            reason=reason or outcome.refusal_reason,
            context={**context, "message": outcome.message},
        )

    # --- Agent controls ---------------------------------------------------

    def set_global_agent_status(
        self,
        agent_id: AgentIdentifier,
        status: AgentControlStatus,
        *,
        expected_version: int | None,
        reason: str | None = None,
    ) -> CommandOutcome:
        """Enable, pause or disable one Agent for every Campaign that inherits it."""

        action = f"agent.global.{status.value}"
        current = self._global_version(agent_id)
        if self._stale(current, expected_version):
            outcome = self._stale_outcome(action, current=current, expected=expected_version)
            self._audit(outcome, reason=reason, context={"agent_id": agent_id.value})
            return outcome
        spec = get_agent_spec(agent_id)
        try:
            control = agent_controls.set_global_control(
                self._session,
                agent_id=agent_id,
                status=status,
                actor=self._actor,
                reason=reason,
            )
            reconciled = reconcile_agent_control(
                self._session, agent_id=agent_id, actor=self._actor
            )
        except agent_controls.AgentControlError as exc:
            outcome = CommandOutcome(
                action=action,
                accepted=False,
                message=f"{spec.display_name} was not changed.",
                refusal_reason=sanitize_text(str(exc)),
                entity_id=agent_id.value,
            )
            self._audit(outcome, reason=reason, context={"agent_id": agent_id.value})
            return outcome
        outcome = CommandOutcome(
            action=action,
            accepted=True,
            message=f"{spec.display_name} is {control.status.value} globally.",
            in_flight_note=_IN_FLIGHT_NOTES[status],
            reconciled_jobs=reconciled,
            entity_id=agent_id.value,
        )
        self._audit(
            outcome,
            reason=reason,
            context={
                "agent_id": agent_id.value,
                "version": control.version,
                "reconciled_jobs": reconciled,
            },
        )
        return outcome

    def set_campaign_override(
        self,
        campaign_id: uuid.UUID,
        agent_id: AgentIdentifier,
        status: AgentControlStatus,
        *,
        expected_version: int | None,
        reason: str | None = None,
    ) -> CommandOutcome:
        """Override one Agent for one Campaign, leaving every other untouched."""

        action = f"agent.campaign_override.{status.value}"
        spec = get_agent_spec(agent_id)
        if self._session.get(Campaign, campaign_id) is None:
            raise WorkbenchCommandError("that Campaign does not exist")
        current = self._campaign_version(campaign_id, agent_id)
        if self._stale(current, expected_version):
            outcome = self._stale_outcome(action, current=current, expected=expected_version)
            self._audit(
                outcome,
                reason=reason,
                context={"agent_id": agent_id.value, "campaign_id": str(campaign_id)},
            )
            return outcome
        try:
            override = agent_controls.set_campaign_override(
                self._session,
                campaign_id=campaign_id,
                agent_id=agent_id,
                status=status,
                actor=self._actor,
                reason=reason,
            )
            reconciled = reconcile_agent_control(
                self._session,
                agent_id=agent_id,
                campaign_id=campaign_id,
                actor=self._actor,
            )
        except agent_controls.AgentControlError as exc:
            outcome = CommandOutcome(
                action=action,
                accepted=False,
                message=f"{spec.display_name} was not changed for this Campaign.",
                refusal_reason=sanitize_text(str(exc)),
                entity_id=str(campaign_id),
            )
            self._audit(
                outcome,
                reason=reason,
                context={"agent_id": agent_id.value, "campaign_id": str(campaign_id)},
            )
            return outcome
        outcome = CommandOutcome(
            action=action,
            accepted=True,
            message=(
                f"{spec.display_name} is {override.status.value} for this Campaign only. "
                "No other Campaign changed."
            ),
            in_flight_note=_IN_FLIGHT_NOTES[status],
            reconciled_jobs=reconciled,
            entity_id=str(override.id),
        )
        self._audit(
            outcome,
            reason=reason,
            context={
                "agent_id": agent_id.value,
                "campaign_id": str(campaign_id),
                "version": override.version,
                "reconciled_jobs": reconciled,
            },
        )
        return outcome

    def set_campaign_live_opt_in(
        self,
        campaign_id: uuid.UUID,
        agent_id: AgentIdentifier,
        *,
        live: bool,
        expected_version: int | None,
        reason: str | None = None,
    ) -> CommandOutcome:
        """Let this Campaign's Agent do real work, or stop letting it.

        The same optimistic-concurrency guard, the same refusal shape and the
        same audit trail as :meth:`set_campaign_override`, because it writes the
        same row through the same Phase 2 service. What it does *not* do is
        reconcile: the opt-in changes what the Agent may do, not whether it may
        claim, so nothing that was held by a control is released by it — and work
        already refused for a *different* reason must not be swept back into the
        queue by a decision that has nothing to do with it. Releasing the jobs
        this opt-in refused is the operator's own act, through the re-run the
        stage already offers, which names every contact it would touch.
        """

        action = f"agent.campaign_live_opt_in.{'on' if live else 'off'}"
        spec = get_agent_spec(agent_id)
        if self._session.get(Campaign, campaign_id) is None:
            raise WorkbenchCommandError("that Campaign does not exist")
        current = self._campaign_version(campaign_id, agent_id)
        if self._stale(current, expected_version):
            outcome = self._stale_outcome(action, current=current, expected=expected_version)
            self._audit(
                outcome,
                reason=reason,
                context={"agent_id": agent_id.value, "campaign_id": str(campaign_id)},
            )
            return outcome
        try:
            override = agent_controls.set_campaign_live_opt_in(
                self._session,
                campaign_id=campaign_id,
                agent_id=agent_id,
                live=live,
                actor=self._actor,
                reason=reason,
            )
        except agent_controls.AgentControlError as exc:
            outcome = CommandOutcome(
                action=action,
                accepted=False,
                message=f"{spec.display_name} was not changed for this Campaign.",
                refusal_reason=sanitize_text(str(exc)),
                entity_id=str(campaign_id),
            )
            self._audit(
                outcome,
                reason=reason,
                context={"agent_id": agent_id.value, "campaign_id": str(campaign_id)},
            )
            return outcome
        outcome = CommandOutcome(
            action=action,
            accepted=True,
            message=(
                f"Live {spec.display_name} work is allowed for this Campaign only. "
                "No other Campaign changed."
                if live
                else f"Live {spec.display_name} work is no longer allowed for this Campaign."
            ),
            in_flight_note=(
                "Work already held at this Agent stays held until you run the stage "
                "again; nothing was released automatically."
                if live
                else "Nothing was discarded: jobs, evidence and stage history are untouched, "
                "and the Agent refuses the next execution instead."
            ),
            entity_id=str(override.id),
        )
        self._audit(
            outcome,
            reason=reason,
            context={
                "agent_id": agent_id.value,
                "campaign_id": str(campaign_id),
                "live": live,
                "version": override.version,
            },
        )
        return outcome

    def clear_campaign_override(
        self,
        campaign_id: uuid.UUID,
        agent_id: AgentIdentifier,
        *,
        expected_version: int | None,
    ) -> CommandOutcome:
        action = "agent.campaign_override.cleared"
        spec = get_agent_spec(agent_id)
        if self._session.get(Campaign, campaign_id) is None:
            raise WorkbenchCommandError("that Campaign does not exist")
        current = self._campaign_version(campaign_id, agent_id)
        if self._stale(current, expected_version):
            outcome = self._stale_outcome(action, current=current, expected=expected_version)
            self._audit(
                outcome,
                reason=None,
                context={"agent_id": agent_id.value, "campaign_id": str(campaign_id)},
            )
            return outcome
        removed = agent_controls.clear_campaign_override(
            self._session,
            campaign_id=campaign_id,
            agent_id=agent_id,
            actor=self._actor,
        )
        if not removed:
            outcome = CommandOutcome(
                action=action,
                accepted=False,
                message="This Campaign had no override for that Agent.",
                refusal_reason="There was nothing to clear.",
                entity_id=str(campaign_id),
            )
            self._audit(
                outcome,
                reason=None,
                context={"agent_id": agent_id.value, "campaign_id": str(campaign_id)},
            )
            return outcome
        reconciled = reconcile_agent_control(
            self._session, agent_id=agent_id, campaign_id=campaign_id, actor=self._actor
        )
        campaign = self._session.get(Campaign, campaign_id)
        assert campaign is not None
        effective = agent_controls.effective_control(
            self._session, campaign=campaign, agent_id=agent_id
        )
        outcome = CommandOutcome(
            action=action,
            accepted=True,
            message=(
                f"{spec.display_name} follows the inherited control again "
                f"({effective.status.value}, from {effective.source.replace('_', ' ')})."
            ),
            in_flight_note=_IN_FLIGHT_NOTES[effective.status],
            reconciled_jobs=reconciled,
            entity_id=str(campaign_id),
        )
        self._audit(
            outcome,
            reason=None,
            context={
                "agent_id": agent_id.value,
                "campaign_id": str(campaign_id),
                "reconciled_jobs": reconciled,
            },
        )
        return outcome

    # --- Sending ----------------------------------------------------------

    def stop_sending(
        self, *, expected_version: int | None, reason: str | None = None
    ) -> CommandOutcome:
        """Stop new Sending work everywhere, immediately.

        This is the global Sending control set to ``disabled``. There is no
        second, parallel "emergency" flag: one authoritative control means the
        stop is visible in exactly the same place as every other Sending state,
        including to the worker.
        """

        outcome = self.set_global_agent_status(
            AgentIdentifier.SENDING,
            AgentControlStatus.DISABLED,
            expected_version=expected_version,
            reason=reason or "operator emergency stop",
        )
        if not outcome.accepted:
            return outcome
        return CommandOutcome(
            action="agent.sending.emergency_stop",
            accepted=True,
            message="Sending is stopped. No new sending work will be claimed in any Campaign.",
            in_flight_note=_IN_FLIGHT_NOTES[AgentControlStatus.DISABLED],
            reconciled_jobs=outcome.reconciled_jobs,
            entity_id=AgentIdentifier.SENDING.value,
        )

    def resume_sending(
        self, *, expected_version: int | None, reason: str | None = None
    ) -> CommandOutcome:
        """Ask Phase 2 to allow Sending again.

        Deliberately routed through the ordinary control path so the Phase 2
        safety checks apply unchanged. While the Sending Agent has no executable
        adapter registered, Phase 2 refuses, and the Workbench shows that refusal
        rather than hiding the control or implying sending is available.
        """

        return self.set_global_agent_status(
            AgentIdentifier.SENDING,
            AgentControlStatus.ENABLED,
            expected_version=expected_version,
            reason=reason or "operator requested sending resume",
        )

    # --- Jobs -------------------------------------------------------------

    def retry_job(self, job_id: uuid.UUID, *, reason: str | None = None) -> CommandOutcome:
        """Retry one failed job under its existing durable identity."""

        action = "job.retry"
        try:
            job = agent_jobs.retry_failed_job(self._session, job_id=job_id, actor=self._actor)
        except agent_jobs.AgentJobNotFound as exc:
            raise WorkbenchCommandError(str(exc)) from exc
        except agent_jobs.AgentJobError as exc:
            outcome = CommandOutcome(
                action=action,
                accepted=False,
                message="The job was not requeued.",
                refusal_reason=sanitize_text(str(exc)),
                entity_id=str(job_id),
            )
            self._audit(outcome, reason=reason, context={"job_id": str(job_id)})
            return outcome
        outcome = CommandOutcome(
            action=action,
            accepted=True,
            message=(
                f"Attempt {job.attempts + 1} of {job.max_attempts} is queued under the job's "
                f"existing identity, so completed work cannot be duplicated."
            ),
            entity_id=str(job_id),
        )
        self._audit(
            outcome,
            reason=reason,
            context={"job_id": str(job_id), "agent_id": job.agent_id.value},
        )
        return outcome

    # --- Campaign Contacts ------------------------------------------------

    def pause_contact(self, campaign_contact_id: uuid.UUID, *, reason: str) -> CommandOutcome:
        action = "campaign_contact.pause"
        try:
            membership = pause_membership(
                self._session,
                campaign_contact_id=campaign_contact_id,
                actor=self._actor,
                reason=reason,
            )
        except CampaignContactNotFound as exc:
            raise WorkbenchCommandError(str(exc)) from exc
        except CampaignContactError as exc:
            return self._refused(action, campaign_contact_id, exc, reason=reason)
        outcome = CommandOutcome(
            action=action,
            accepted=True,
            message="This Campaign Contact is paused.",
            in_flight_note=(
                "Its claimable and leased jobs are held at paused; nothing is discarded."
            ),
            entity_id=str(membership.id),
        )
        self._audit(outcome, reason=reason, context={"campaign_contact_id": str(membership.id)})
        return outcome

    def resume_contact(self, campaign_contact_id: uuid.UUID) -> CommandOutcome:
        action = "campaign_contact.resume"
        try:
            membership = resume_membership(
                self._session,
                campaign_contact_id=campaign_contact_id,
                actor=self._actor,
            )
        except CampaignContactNotFound as exc:
            raise WorkbenchCommandError(str(exc)) from exc
        except CampaignContactError as exc:
            return self._refused(action, campaign_contact_id, exc, reason=None)
        outcome = CommandOutcome(
            action=action,
            accepted=True,
            message="This Campaign Contact is active again.",
            in_flight_note=(
                "Work paused by the membership is released; a domain block keeps its own reason."
            ),
            entity_id=str(membership.id),
        )
        self._audit(outcome, reason=None, context={"campaign_contact_id": str(membership.id)})
        return outcome

    def retry_contact(self, campaign_contact_id: uuid.UUID, *, reason: str) -> CommandOutcome:
        """Retry the Contact's current stage without bypassing any control."""

        action = "campaign_contact.retry"
        try:
            job = retry_processing(
                self._session,
                campaign_contact_id=campaign_contact_id,
                actor=self._actor,
                reason=reason,
            )
        except CampaignContactNotFound as exc:
            raise WorkbenchCommandError(str(exc)) from exc
        except CampaignContactError as exc:
            return self._refused(action, campaign_contact_id, exc, reason=reason)
        outcome = CommandOutcome(
            action=action,
            accepted=True,
            message=(
                f"The current stage is queued again as job {job.id}."
                if job is not None
                else "The stage was reset to waiting; no job was queued yet."
            ),
            entity_id=str(campaign_contact_id),
        )
        self._audit(
            outcome, reason=reason, context={"campaign_contact_id": str(campaign_contact_id)}
        )
        return outcome

    def skip_stage(self, campaign_contact_id: uuid.UUID, *, reason: str) -> CommandOutcome:
        """Cancel the current stage's work and move past it, deliberately."""

        action = "campaign_contact.skip_stage"
        membership = self._session.get(CampaignContact, campaign_contact_id)
        if membership is None:
            raise WorkbenchCommandError("that Campaign Contact does not exist")
        agent_id = membership.next_stage
        if agent_id is None:
            return CommandOutcome(
                action=action,
                accepted=False,
                message="Nothing was skipped.",
                refusal_reason="This Campaign Contact has no current stage to skip.",
                entity_id=str(campaign_contact_id),
            )
        try:
            skip_current_stage(
                self._session,
                membership=membership,
                agent_id=agent_id,
                actor=self._actor,
                reason=reason,
            )
        except PipelineStateError as exc:
            return self._refused(action, campaign_contact_id, exc, reason=reason)
        outcome = CommandOutcome(
            action=action,
            accepted=True,
            message=f"The {agent_id.value} stage was skipped for this Campaign Contact.",
            in_flight_note="Its non-terminal jobs for that stage were cancelled and recorded.",
            entity_id=str(campaign_contact_id),
        )
        self._audit(
            outcome,
            reason=reason,
            context={
                "campaign_contact_id": str(campaign_contact_id),
                "agent_id": agent_id.value,
            },
        )
        return outcome

    def _refused(
        self,
        action: str,
        entity_id: uuid.UUID,
        exc: Exception,
        *,
        reason: str | None,
    ) -> CommandOutcome:
        outcome = CommandOutcome(
            action=action,
            accepted=False,
            message="Phase 2 declined the command; nothing changed.",
            refusal_reason=sanitize_text(str(exc)),
            entity_id=str(entity_id),
        )
        self._audit(outcome, reason=reason, context={"entity_id": str(entity_id)})
        return outcome
