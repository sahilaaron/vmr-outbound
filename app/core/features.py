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
    # Local LinkedIn person-profile capture intake endpoint (DAT-012D). Off by
    # default so the endpoint stays fully disabled (returns 404) until
    # deliberately enabled for local operation. Turning it on stages immutable
    # snapshots only; it never updates a canonical contact by itself.
    linkedin_profile_intake: bool = False
    # Exact-URL contact refresh from stored profile snapshots (DAT-012E) plus
    # the versioned QA-policy evaluation (DAT-012F). Off by default: while off,
    # accepted snapshots stay at outcome ``stored`` and no canonical contact
    # field is touched. Turning it on never merges weak matches — only an exact
    # normalized profile-URL match can refresh, and suppression stays authoritative.
    linkedin_profile_refresh: bool = False
    # Local LinkedIn company-page capture intake endpoint (DAT-012G). Off by
    # default; when on it stores immutable firmographic evidence and links it
    # to existing companies by exact LinkedIn URL lineage or exact unique
    # domain only — it never rewrites a canonical company record.
    linkedin_company_intake: bool = False
    # Contact-first capture intake (DAT-013): the endpoint the contact
    # acquisition extension submits to. Off by default. When on, one reviewed
    # submission persists immutable per-person capture evidence, matches only on
    # an exact normalized LinkedIn profile URL, refreshes canonical fields under
    # the DAT-005 freshness policy, and applies operator labels and notes. It
    # never creates a campaign membership, verifies an email, or makes any
    # contact outreach-eligible; suppression stays authoritative.
    contact_capture_intake: bool = False
    # Promotion of a staged contact capture into a canonical contact (DAT-014).
    # Off by default. When on, the workbench can resolve a captured company's
    # domain through the existing DAT-010 logo.dev candidate flow and promote the
    # capture. It never fabricates a domain, never auto-accepts a provider
    # result, never merges an ambiguous identity, and never makes a contact
    # outreach-eligible; suppression stays authoritative. The lookup itself also
    # requires ``salesnav_domain_enrichment`` and a configured logo.dev key.
    contact_capture_promotion: bool = False
    # Operator workbench UI (server-rendered pages). Off by default so the UI
    # stays disabled until it is deliberately enabled for local operation.
    workbench: bool = False
    # Operator-driven Sales Navigator company-domain enrichment via the official
    # logo.dev Search Brands API (DAT-010). Off by default so the lookup UI and
    # any outbound call stay fully disabled until deliberately enabled for local
    # operation. Turning it on does not import anything and never auto-accepts a
    # candidate: the operator still confirms every domain by hand.
    salesnav_domain_enrichment: bool = False
    # Automatic company-domain resolution (DAT-017). Off by default. While off,
    # every captured company waits for an operator exactly as it did in DAT-014.
    # When on, a versioned policy may confirm a domain WITHOUT an operator — but
    # only where two independent evidence axes agree, or an operator-captured
    # company page names it under an exact identity match. It never accepts a
    # provider result on rank or on being the only result, never invents a
    # domain when the provider is unreachable, never overwrites a decision an
    # operator made, and never relaxes suppression or identity ambiguity: those
    # block a promotion whoever chose the domain. Requires
    # ``contact_capture_promotion``; a provider lookup additionally requires
    # ``salesnav_domain_enrichment`` and a configured logo.dev key.
    automatic_domain_resolution: bool = False
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
