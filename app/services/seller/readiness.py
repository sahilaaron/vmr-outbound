"""Deterministic context readiness (KB-001).

What this is: a description of what an operator has entered, computed in plain
Python from stored columns.

What this is not, and these are load-bearing distinctions:

* **Not a score.** There is no total, no percentage, and no weighting. A
  rollup number would invite "get it to 100" and would hide which specific
  thing is missing, which is the only useful part.
* **Not an approval.** Entering a record is already the authorization
  (KB-001). Readiness never grants or withholds permission.
* **Not a gate.** Nothing in the application consults this module before doing
  anything. A campaign with no offerings and an empty knowledge base runs
  exactly as it did before this existed. The one place a missing item already
  blocks something, an existing rule blocks it — not this.
* **Not model output.** No LLM is involved, at any point, in any item. Every
  state is reproducible from the database alone, and every one carries a reason
  string naming the fact that produced it.

Each item reports one of four states, and the four are genuinely different:
``CONFIGURED``, ``INCOMPLETE`` (started, and this named part is missing),
``NOT_CONFIGURED`` (never begun), and ``NOT_APPLICABLE`` (the question does not
arise here, for a structural reason the reason states).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.enums import ContextReadinessState
from app.services.seller import campaign_offerings, records
from app.services.seller.profile import get_profile


@dataclass(frozen=True)
class ReadinessItem:
    """One readiness statement, and the fact that produced it."""

    key: str
    label: str
    state: ContextReadinessState
    reason: str
    # Where an operator goes to act on this, when there is somewhere to go.
    link: str | None = None

    @property
    def is_configured(self) -> bool:
        return self.state is ContextReadinessState.CONFIGURED


@dataclass(frozen=True)
class ReadinessReport:
    """The full set of readiness items for a subject."""

    items: tuple[ReadinessItem, ...]

    @property
    def configured_count(self) -> int:
        return sum(1 for item in self.items if item.is_configured)

    @property
    def applicable_count(self) -> int:
        """How many items could be configured at all.

        ``NOT_APPLICABLE`` items are excluded so the counter never implies an
        operator has work to do that the system does not currently support.
        """

        return sum(
            1 for item in self.items if item.state is not ContextReadinessState.NOT_APPLICABLE
        )


# The profile fields that make it useful to a reader, and the label each is
# reported under when missing. Name is excluded: it is NOT NULL, so a profile
# that exists always has one.
_PROFILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("short_description", "short description"),
    ("description", "standard description"),
    ("positioning", "positioning"),
)

_PROFILE_LISTS: tuple[tuple[str, str], ...] = (
    ("industries_served", "industries served"),
    ("geographies_served", "geographies served"),
    ("capabilities", "capabilities"),
)


def company_profile_item(session: Session) -> ReadinessItem:
    """Whether the seller company profile is filled in."""

    profile = get_profile(session)
    if profile is None:
        return ReadinessItem(
            key="company_profile",
            label="Company profile",
            state=ContextReadinessState.NOT_CONFIGURED,
            reason="No seller company profile has been entered.",
            link="/knowledge-base/company",
        )

    missing = [label for field, label in _PROFILE_FIELDS if not getattr(profile, field)]
    # ``None`` is missing; ``[]`` is an answer. An operator who recorded that
    # they serve no specific industry list has addressed the question.
    missing += [label for field, label in _PROFILE_LISTS if getattr(profile, field) is None]

    if missing:
        return ReadinessItem(
            key="company_profile",
            label="Company profile",
            state=ContextReadinessState.INCOMPLETE,
            reason=f"“{profile.name}” is entered, but has no {_join(missing)}.",
            link="/knowledge-base/company",
        )
    return ReadinessItem(
        key="company_profile",
        label="Company profile",
        state=ContextReadinessState.CONFIGURED,
        reason=f"“{profile.name}” is entered with a description, positioning and coverage.",
        link="/knowledge-base/company",
    )


def _record_item(
    *,
    key: str,
    label: str,
    counts: records.RecordCounts,
    singular: str,
    plural: str,
    link: str,
) -> ReadinessItem:
    """Readiness for a record type, where "some active rows exist" is the test."""

    if counts.active:
        return ReadinessItem(
            key=key,
            label=label,
            state=ContextReadinessState.CONFIGURED,
            reason=f"{counts.active} active {plural if counts.active != 1 else singular}.",
            link=link,
        )
    if counts.archived:
        # Every one withdrawn is not the same as never having had any: the
        # operator has been here, and undoing an archive is a different action
        # from starting from nothing.
        return ReadinessItem(
            key=key,
            label=label,
            state=ContextReadinessState.INCOMPLETE,
            reason=(
                f"{counts.archived} {plural if counts.archived != 1 else singular} exist, "
                "but all of them are archived."
            ),
            link=link,
        )
    return ReadinessItem(
        key=key,
        label=label,
        state=ContextReadinessState.NOT_CONFIGURED,
        reason=f"No {plural} have been entered.",
        link=link,
    )


def seller_report(session: Session) -> ReadinessReport:
    """Readiness of the seller-side knowledge base as a whole."""

    counts = records.counts(session)
    return ReadinessReport(
        items=(
            company_profile_item(session),
            _record_item(
                key="offerings",
                label="Offerings",
                counts=counts["offerings"],
                singular="offering",
                plural="offerings",
                link="/knowledge-base/offerings",
            ),
            _record_item(
                key="proof_points",
                label="Proof points",
                counts=counts["proof_points"],
                singular="proof point",
                plural="proof points",
                link="/knowledge-base/proof-points",
            ),
            _record_item(
                key="restricted_claims",
                label="Restricted claims",
                counts=counts["restricted_claims"],
                singular="restricted claim",
                plural="restricted claims",
                link="/knowledge-base/restricted-claims",
            ),
            _record_item(
                key="personas",
                label="Personas",
                counts=counts["personas"],
                singular="persona",
                plural="personas",
                link="/knowledge-base/personas",
            ),
        )
    )


def campaign_report(session: Session, campaign: Campaign) -> ReadinessReport:
    """Readiness of one campaign's context configuration."""

    return ReadinessReport(
        items=(
            _campaign_offerings_item(session, campaign.id),
            _campaign_messaging_item(),
        )
    )


def _campaign_offerings_item(session: Session, campaign_id: uuid.UUID) -> ReadinessItem:
    linked = campaign_offerings.offerings_for_campaign(session, campaign_id)
    if not linked:
        return ReadinessItem(
            key="campaign_offerings",
            label="Offering associations",
            state=ContextReadinessState.NOT_CONFIGURED,
            reason=(
                "This campaign names no offerings. That is allowed — the association "
                "is optional and nothing is blocked by its absence."
            ),
            link=f"/campaigns/{campaign_id}",
        )
    return ReadinessItem(
        key="campaign_offerings",
        label="Offering associations",
        state=ContextReadinessState.CONFIGURED,
        reason=f"This campaign concerns {_join([offering.name for offering in linked])}.",
        link=f"/campaigns/{campaign_id}",
    )


def _campaign_messaging_item() -> ReadinessItem:
    """Whether the campaign carries its own messaging and call to action.

    Reported as ``NOT_APPLICABLE`` because the campaign record does not yet
    have anywhere to put them. ``Campaign`` currently holds a name, a
    description and a status; purpose, email instructions, call to action,
    messaging direction and sequence configuration are owned by the campaign
    and drafting cards (CMP-*, DRF-*) and have no columns yet.

    Reporting this as ``NOT_CONFIGURED`` would have been a lie in the operator's
    direction — it would show work they cannot do. Omitting the row entirely
    would have hidden a real gap in the context an assembler will eventually
    need. ``NOT_APPLICABLE`` says exactly what is true: the question does not
    arise against today's schema.
    """

    return ReadinessItem(
        key="campaign_messaging",
        label="Campaign messaging and CTA",
        state=ContextReadinessState.NOT_APPLICABLE,
        reason=(
            "The campaign record has no messaging, instruction or call-to-action "
            "fields yet, so there is nothing here to configure. The campaign "
            "operator still defines these directly when drafting is built."
        ),
        link=None,
    )


def _join(values: list[str]) -> str:
    """Join a list the way a person would read it aloud."""

    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])} and {values[-1]}"
