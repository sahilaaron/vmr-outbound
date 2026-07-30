"""What a company-domain resolution authorizes downstream (DAT-017A).

``provisional`` is only a real state if something refuses to treat it as
``confirmed``. This module is that something.

The rule, from issue #171: a provisional domain may start company research, and
may not by itself authorize final qualification, personalized drafting, email
discovery against the domain, campaign eligibility, or sending. Research or later
reviewed evidence must confirm the company identity before those proceed.

Two design points worth stating plainly, because both could be got wrong quietly:

**No resolution record is not a restriction.** A company whose domain came from a
spreadsheet column, an operator's own confirmation, or a pre-DAT-017A promotion
has no decision row at all, and this gate authorizes it exactly as before. This
task introduced ``provisional``; it did not retroactively cast doubt on every
domain the system already had. The gate restricts what this policy actually
marked uncertain — nothing else. ``UNRESOLVED`` blocks the same stages as
``provisional`` for the obvious reason that there is no domain to work from.

**The gate is a backend rule, not a UI state.** It is enforced in the service
that would otherwise act (see :func:`require`), so a route, a script, or a later
feature that forgets to look at a badge still cannot spend a provider call on a
domain nobody has confirmed.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import DomainResolutionState
from app.services.resolution import store


class DownstreamStage(enum.StrEnum):
    """A stage that depends on knowing which company someone actually works at.

    Named here in full — including stages this repository has not built yet — so
    that when they are built they wire into an existing rule instead of each
    inventing its own reading of what ``provisional`` allows.
    """

    #: Gathering evidence about the company. The one stage provisional opens.
    COMPANY_RESEARCH = "company_research"
    #: Deciding the company is a fit. Needs the identity settled first.
    FINAL_QUALIFICATION = "final_qualification"
    #: Writing outreach that names the company back to the person.
    PERSONALIZED_DRAFTING = "personalized_drafting"
    #: Generating or verifying addresses AT the domain — the stage that spends
    #: money and touches a mail server on the strength of the domain being right.
    EMAIL_DISCOVERY = "email_discovery"
    #: Becoming a member of a campaign.
    CAMPAIGN_ELIGIBILITY = "campaign_eligibility"
    #: Sending.
    SENDING = "sending"


#: The stages a provisional domain opens when nothing says otherwise.
#:
#: This stays the default because the permissive reading has to be asked for, not
#: inherited: the stages it withholds are the ones that spend money and send mail
#: on the strength of the domain being right. A caller that knows which Campaign
#: it is acting for may widen it — see :func:`provisional_allows_for`.
_PROVISIONAL_ALLOWS = frozenset({DownstreamStage.COMPANY_RESEARCH})

#: Every stage, for a Campaign that has accepted provisional domains.
_ALL_STAGES = frozenset(DownstreamStage)


def provisional_allows_for(campaign: Campaign | None) -> frozenset[DownstreamStage]:
    """What a provisional domain opens for one Campaign.

    A Campaign with ``allow_provisional_domains`` set has decided that an
    uncorroborated provider candidate is good enough to act on, and this returns
    every stage accordingly. ``None`` — a caller with no Campaign in scope, such
    as the contact-scoped candidate generator or the company workspace — gets the
    strict default, so an un-campaigned path fails closed rather than silently
    inheriting the most permissive campaign's answer.

    What this never affects: a provisional decision still writes nothing to the
    approved-mapping store, and a Company standing on a provisional decision is
    still not established evidence. Those two guards are what stop a guess
    upgrading itself into certainty, and no Campaign setting reaches them.
    """

    if campaign is not None and campaign.allow_provisional_domains:
        return _ALL_STAGES
    return _PROVISIONAL_ALLOWS


_STAGE_TEXT: dict[DownstreamStage, str] = {
    DownstreamStage.COMPANY_RESEARCH: "company research",
    DownstreamStage.FINAL_QUALIFICATION: "final qualification",
    DownstreamStage.PERSONALIZED_DRAFTING: "personalized drafting",
    DownstreamStage.EMAIL_DISCOVERY: "email discovery",
    DownstreamStage.CAMPAIGN_ELIGIBILITY: "campaign eligibility",
    DownstreamStage.SENDING: "sending",
}


class DownstreamBlocked(Exception):
    """A stage was attempted against a company whose domain is not confirmed."""


@dataclass(frozen=True)
class GateDecision:
    """Whether a stage may proceed, and the sentence explaining why not."""

    stage: DownstreamStage
    allowed: bool
    #: ``None`` when no automatic resolution ever spoke about this company.
    state: DomainResolutionState | None
    reason: str | None = None

    @property
    def blocked(self) -> bool:
        return not self.allowed


def evaluate_state(
    state: DomainResolutionState | None,
    stage: DownstreamStage,
    *,
    provisional_allows: frozenset[DownstreamStage] = _PROVISIONAL_ALLOWS,
) -> GateDecision:
    """The gate rule itself, over a state alone. Pure and directly testable.

    ``provisional_allows`` is the Campaign's answer to "how far do we trust an
    uncorroborated candidate?", supplied by :func:`provisional_allows_for`. It is
    a parameter rather than a lookup so this function stays pure, and so a caller
    that has no Campaign cannot accidentally get the permissive answer.
    """

    if state is None or state is DomainResolutionState.CONFIRMED:
        return GateDecision(stage=stage, allowed=True, state=state)

    label = _STAGE_TEXT[stage]
    if state is DomainResolutionState.PROVISIONAL:
        if stage in provisional_allows:
            return GateDecision(stage=stage, allowed=True, state=state)
        return GateDecision(
            stage=stage,
            allowed=False,
            state=state,
            reason=(
                f"the company domain is provisional and this Campaign has not accepted "
                f"provisional domains, so {label} needs a confirmed company identity first"
            ),
        )

    return GateDecision(
        stage=stage,
        allowed=False,
        state=state,
        reason=(
            f"the company domain is unresolved, so {label} has no confirmed company to work from"
        ),
    )


def authorize_company(
    session: Session,
    *,
    company_id: uuid.UUID | None,
    stage: DownstreamStage,
    campaign: Campaign | None = None,
) -> GateDecision:
    """Whether *stage* may proceed for a permanent company."""

    if company_id is None:
        return GateDecision(stage=stage, allowed=True, state=None)
    return evaluate_state(
        store.company_state(session, company_id),
        stage,
        provisional_allows=provisional_allows_for(campaign),
    )


def authorize_contact(
    session: Session,
    *,
    contact: Contact,
    stage: DownstreamStage,
    campaign: Campaign | None = None,
) -> GateDecision:
    """Whether *stage* may proceed for one contact.

    Read through the permanent ``company_id`` edge, which is the link a
    resolution decision actually sets. A contact carrying only the transitional
    ``company_domain`` string has no decision behind it and is not restricted
    here — same reasoning as an unresolved-by-this-policy company above.
    """

    return authorize_company(session, company_id=contact.company_id, stage=stage, campaign=campaign)


def require(
    session: Session,
    *,
    contact: Contact,
    stage: DownstreamStage,
    campaign: Campaign | None = None,
) -> None:
    """Raise :class:`DownstreamBlocked` unless *stage* may proceed for *contact*."""

    decision = authorize_contact(session, contact=contact, stage=stage, campaign=campaign)
    if decision.blocked:
        raise DownstreamBlocked(decision.reason or "this stage is not authorized")


# --- Research readiness -------------------------------------------------------


@dataclass(frozen=True)
class ResearchReadiness:
    """Whether company research may start, stated in the operator's terms."""

    ready: bool
    state: DomainResolutionState | None
    domain: str | None
    reason: str

    @property
    def is_provisional(self) -> bool:
        return self.state is DomainResolutionState.PROVISIONAL


def research_readiness(
    session: Session, *, company_id: uuid.UUID, domain: str | None
) -> ResearchReadiness:
    """Whether this company is research-ready, and why.

    Research needs a domain to research and no reason to doubt it names this
    company. A provisional domain qualifies — that is what provisional is for —
    and says so, rather than reporting a bare "ready" that would read as more
    settled than it is.
    """

    state = store.company_state(session, company_id)
    if not domain:
        return ResearchReadiness(
            ready=False,
            state=state,
            domain=None,
            reason="No company domain has been resolved yet, so there is nothing to research.",
        )
    if state is DomainResolutionState.UNRESOLVED:
        return ResearchReadiness(
            ready=False,
            state=state,
            domain=domain,
            reason=(
                "The latest domain resolution for this company is unresolved, so its "
                "identity is not settled enough to research."
            ),
        )
    if state is DomainResolutionState.PROVISIONAL:
        return ResearchReadiness(
            ready=True,
            state=state,
            domain=domain,
            reason=(
                "Research may start on a provisional domain. Nothing after research — "
                "qualification, drafting, email discovery, campaigns, sending — may proceed "
                "until the company identity is confirmed."
            ),
        )
    if state is DomainResolutionState.CONFIRMED:
        return ResearchReadiness(
            ready=True,
            state=state,
            domain=domain,
            reason="The company domain is confirmed, so research may start.",
        )
    return ResearchReadiness(
        ready=True,
        state=None,
        domain=domain,
        reason=(
            "This company's domain did not come from automatic resolution, so nothing "
            "restricts it. Research may start."
        ),
    )
