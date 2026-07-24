"""Feature switches.

Every pipeline capability beyond the Phase 0 foundation is represented here and
defaults to **off**. Unfinished functionality must stay disabled until its phase
is built and verified (AGENTS.md, FND-007). Flags are read from the environment
with the ``FEATURES__`` prefix, e.g. ``FEATURES__CSV_IMPORT=true``.

Turning a flag on does not implement the feature; it only unlocks code paths
that a later phase will add. Keeping the switch here from the start means later
phases wire behind an existing gate rather than introducing new global state.
"""

from __future__ import annotations

from pydantic import BaseModel


class FeatureFlags(BaseModel):
    """Named capability switches. All default to False for the first launch."""

    model_config = {"frozen": True}

    # Phase 1 — Data & Campaigns
    csv_import: bool = False
    # Local Sales Navigator capture intake endpoint (DAT-009). Off by default so
    # the endpoint stays fully disabled (returns 404) until deliberately enabled
    # for local operation, matching the FND-007 default-off pattern.
    salesnav_intake: bool = False
    # Operator workbench UI (server-rendered pages). Off by default so the UI
    # stays disabled until it is deliberately enabled for local operation.
    workbench: bool = False
    # Operator-driven Sales Navigator company-domain enrichment via the official
    # logo.dev Search Brands API (DAT-010). Off by default so the lookup UI and
    # any outbound call stay fully disabled until deliberately enabled for local
    # operation. Turning it on does not import anything and never auto-accepts a
    # candidate: the operator still confirms every domain by hand.
    salesnav_domain_enrichment: bool = False
    normalization: bool = False
    deduplication: bool = False
    suppressions: bool = False
    # Phase 2 — Email Verification
    email_generation: bool = False
    millionverifier: bool = False
    # Phase 3 — Lead Scoring
    scoring: bool = False
    # Phase 4 — Insights
    insights_research: bool = False
    # Phase 5 — Claude Bridge
    claude_mcp_bridge: bool = False
    # Phase 6 — Draft & Approval
    drafting: bool = False
    # Phase 7 — Saleshandy
    saleshandy: bool = False

    def enabled(self) -> list[str]:
        """Return the names of currently enabled features (for audit/health)."""

        return [name for name, value in self.model_dump().items() if value]
