"""The one place that decides what a Campaign is actually pitching.

``app/services/seller/context.py`` answers "what does the seller know that bears
on this Campaign?" and deliberately does not rank it. This module answers the
question that came after it: **of the things this Campaign could lead with, which
one is primary?**

The precedence is fixed and lives here alone:

1. A successful, current Campaign URL offering research — the **primary** pitch.
2. The Campaign's selected Library/VMI offering — **supporting** credibility when
   there is a primary above it, and the primary itself when there is not.
3. The seller profile — who we are, and why we can credibly offer either.

Recipient evidence is not in this list at all. It is untrusted external material
and it is assembled separately, by the callers, exactly as before — see the trust
note in ``app/services/thinking/prompts.py``. Nothing here touches prospect data.

**Why one resolver rather than a branch at each call site.** There are four
places that assemble seller context for a model call — the Insights adapter, the
single-email path, the seven-email sequence path and the context decision that
feeds both. A precedence rule copied four times is a rule that will be four rules
within a release, and the symptom would be a Campaign whose first email leads
with the researched offering and whose fourth does not. So the per-contact Agents
ask this module what the offering *is*; none of them knows how it was chosen.

**A stored version that no longer parses is treated as absent.** ``READY`` rows
are validated on the way in, but a future contract change could leave an old
payload unreadable, and a Campaign must not break because of that. It falls back
to the Library offering, which is the same behaviour a failed research has.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.campaign_offering_research import CampaignOfferingResearch
from app.models.enums import CampaignOfferingSource
from app.services.campaign_offering import jobs as offering_jobs
from app.services.campaign_offering.contracts import (
    OfferingIntelligence,
    offering_from_stored,
)
from app.services.seller import context as seller_context
from app.services.seller.context import SellerContext

#: What ``primary_source`` reports. Strings rather than an enum: they are labels
#: for prompts, diagnostics and tests, not a stored value, and nothing branches
#: on them outside this module's own callers.
PRIMARY_URL_RESEARCH = "url_research"
PRIMARY_LIBRARY = "library"


@dataclass(frozen=True)
class EffectiveCampaignOffering:
    """What one Campaign leads with, and what supports it."""

    campaign_id: uuid.UUID
    mode: CampaignOfferingSource
    #: The Library/VMI side, unchanged and always present. Supporting when there
    #: is researched primary above it; the primary itself otherwise.
    seller: SellerContext
    #: The current READY research row, when one is leading. ``None`` in every
    #: other case, including "URL mode but still preparing" and "URL mode but the
    #: research failed".
    research: CampaignOfferingResearch | None = None
    #: The parsed structure from that row. ``None`` exactly when ``research`` is.
    offering: OfferingIntelligence | None = None
    #: True when URL mode is elected and no version is current yet — the state in
    #: which preparation waits. Reported so a caller can say why, never so it can
    #: decide differently.
    preparing: bool = False
    #: True when URL mode is elected, nothing is current, and nothing is running:
    #: the Campaign has fallen back to its Library offering and the customer has
    #: been told so.
    fell_back: bool = False

    @property
    def primary_source(self) -> str:
        return PRIMARY_URL_RESEARCH if self.offering is not None else PRIMARY_LIBRARY

    @property
    def has_researched_primary(self) -> bool:
        return self.offering is not None


def resolve(session: Session, campaign: Campaign) -> EffectiveCampaignOffering:
    """Decide this Campaign's effective offering context. Read-only."""

    seller = seller_context.assemble(session, campaign_id=campaign.id)
    if campaign.offering_source is not CampaignOfferingSource.URL_RESEARCH:
        return EffectiveCampaignOffering(
            campaign_id=campaign.id,
            mode=campaign.offering_source,
            seller=seller,
        )

    current = offering_jobs.current_version(session, campaign_id=campaign.id)
    parsed = offering_from_stored(current.offering_context) if current is not None else None
    if parsed is not None:
        return EffectiveCampaignOffering(
            campaign_id=campaign.id,
            mode=campaign.offering_source,
            seller=seller,
            research=current,
            offering=parsed,
        )

    running = offering_jobs.active_run(session, campaign_id=campaign.id) is not None
    return EffectiveCampaignOffering(
        campaign_id=campaign.id,
        mode=campaign.offering_source,
        seller=seller,
        preparing=running,
        fell_back=not running,
    )


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------
#
# Two renderings already existed for the Library half — the Insights adapter's
# and Personalization's — and they differ for good reasons. Only the *primary*
# block is shared, because that is the part the precedence decides.


def primary_offering_block(offering: OfferingIntelligence, *, source_url: str) -> str:
    """The researched offering, as the first thing a drafting prompt reads."""

    lines = [
        f"PRIMARY OFFERING — {offering.offering_name}",
        f"  (researched for this campaign from {source_url})",
        f"  {offering.summary}",
    ]
    for label, values in (
        ("For", offering.target_audience),
        ("Problems it addresses", offering.customer_problems),
        ("What it does", offering.key_capabilities),
        ("Outcomes it claims", offering.benefits),
        ("Use cases", offering.use_cases),
        ("Market context", offering.market_context),
        ("Why a buyer cares", offering.buyer_relevance),
    ):
        if values:
            lines.append(f"  {label}: {'; '.join(values)}")
    lines.append(f"  How this stands with what we sell: {offering.seller_connection}")
    if offering.unknowns:
        lines.append(f"  Not established by the page: {'; '.join(offering.unknowns)}")
    return "\n".join(lines)


def supporting_header(effective: EffectiveCampaignOffering) -> str:
    """The one line that tells a prompt what the Library half is *for* here.

    Without it a model reads two offerings and picks whichever it finds more
    quotable, which is precisely the mixed pitch the precedence exists to stop.
    """

    if not effective.has_researched_primary:
        return ""
    return (
        "SUPPORTING OFFERING AND CREDIBILITY (secondary — lead with the primary "
        "offering above; use the following only to show we can credibly offer it)"
    )


def keyword_text(effective: EffectiveCampaignOffering) -> str:
    """Everything the effective offering says, flattened for keyword matching.

    Used by the Personalization context decision, which scores recipient evidence
    by overlap with what we sell. A Campaign leading with a researched offering
    whose words never entered this text would score every genuinely relevant fact
    as irrelevant, and fall back to the weakest writing strategy — the failure
    would look like bad copy rather than a missing join, so it is worth the two
    lines.
    """

    offering = effective.offering
    if offering is None:
        return ""
    parts: list[str] = [offering.offering_name, offering.summary, offering.seller_connection]
    for values in (
        offering.target_audience,
        offering.customer_problems,
        offering.use_cases,
        offering.key_capabilities,
        offering.benefits,
        offering.market_context,
        offering.buyer_relevance,
    ):
        parts.extend(values)
    return "\n".join(part for part in parts if part)
