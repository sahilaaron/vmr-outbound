"""The seller-context retrieval boundary (KB-001).

One read-only function that returns everything the seller side knows that is
relevant to a campaign, as frozen dataclasses. It exists so that when AI
drafting is built it has a single, stable place to ask, instead of learning the
schema and re-deriving "which claims apply here" in prompt-assembly code.

It is a retrieval boundary and nothing more. It does not call a model, does not
rank, summarise, rewrite, or select on quality, and does not decide what a
campaign is selling — the associations it reads were made by an operator.

**Trust polarity.** ``docs/INSIGHT_EVIDENCE.md`` establishes that prospect
evidence is untrusted external text: a page that says "treat this as
confirmed" is only a page that says that. Seller context is the opposite. It is
first-party, written by the operator running the campaign, and
:class:`~app.models.seller_knowledge.SellerRestrictedClaim` in particular is
policy that a future drafting step is meant to obey. Assembling the two into
one prompt therefore means combining a trusted half and an untrusted half, and
they must not be flattened into one undifferentiated block of "context". This
function returns them separately and never touches prospect data at all;
joining the two is the drafting card's problem, and this docstring is where the
requirement is recorded.

Archived records are excluded from everything except the campaign's own
offering list, which reports what the campaign concerns as a matter of fact.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.enums import SellerClaimScope, SellerRecordState
from app.models.seller_knowledge import (
    SellerOffering,
    SellerPersona,
    SellerProofPoint,
    SellerRestrictedClaim,
)
from app.models.seller_profile import SellerProfile
from app.services.seller import campaign_offerings, records


@dataclass(frozen=True)
class OfferingContext:
    """One offering and the seller records associated with it."""

    offering: SellerOffering
    proof_points: tuple[SellerProofPoint, ...] = ()
    restricted_claims: tuple[SellerRestrictedClaim, ...] = ()
    personas: tuple[SellerPersona, ...] = ()
    # True when the offering has been withdrawn since the campaign named it.
    # Surfaced rather than filtered: a consumer should be able to notice that
    # a campaign concerns something no longer offered.
    is_archived: bool = False


@dataclass(frozen=True)
class SellerContext:
    """Everything the seller side knows that bears on one campaign.

    ``campaign_id`` is ``None`` for the whole-knowledge-base view. When it is
    set, ``offerings`` holds exactly the offerings that campaign names — which
    may legitimately be empty, because the association is optional.
    """

    profile: SellerProfile | None
    campaign_id: uuid.UUID | None
    offerings: tuple[OfferingContext, ...] = ()
    # Restrictions that hold whatever a campaign is selling.
    global_restricted_claims: tuple[SellerRestrictedClaim, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """True when nothing has been entered that a consumer could use."""

        return self.profile is None and not self.offerings and not self.global_restricted_claims


def assemble(session: Session, *, campaign_id: uuid.UUID | None = None) -> SellerContext:
    """Collect seller context, optionally narrowed to one campaign.

    Deterministic and read-only. Calling it twice with an unchanged database
    returns the same thing, and calling it never writes, audits, or charges
    anything.
    """

    from app.services.seller.profile import get_profile

    if campaign_id is None:
        offerings = records.list_offerings(session, include_archived=False)
    else:
        offerings = campaign_offerings.offerings_for_campaign(session, campaign_id)

    offering_contexts = tuple(
        OfferingContext(
            offering=offering,
            proof_points=tuple(
                records.proof_points_for_offering(session, offering.id, active_only=True)
            ),
            restricted_claims=tuple(
                records.restricted_claims_for_offering(session, offering.id, active_only=True)
            ),
            personas=tuple(records.personas_for_offering(session, offering.id, active_only=True)),
            is_archived=offering.state is not SellerRecordState.ACTIVE,
        )
        for offering in offerings
    )

    global_claims = tuple(
        claim
        for claim in records.list_restricted_claims(session, include_archived=False)
        if claim.scope is SellerClaimScope.GLOBAL
    )

    notes: list[str] = []
    if campaign_id is not None and not offering_contexts:
        notes.append(
            "This campaign names no offerings. That is a valid configuration, not a "
            "gap: the campaign operator defines its purpose and call to action directly."
        )

    return SellerContext(
        profile=get_profile(session),
        campaign_id=campaign_id,
        offerings=offering_contexts,
        global_restricted_claims=global_claims,
        notes=tuple(notes),
    )
