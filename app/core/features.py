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
    # the DAT-005 freshness policy, and applies operator labels and notes. A
    # Campaign is optional; when selected, the permanent Contact is additionally
    # filed through the idempotent Campaign Contact service and the filing
    # outcome is reported separately. Suppression stays authoritative.
    contact_capture_intake: bool = False
    # Promotion of a staged contact capture into a canonical contact (DAT-014).
    # Off by default. When on, the workbench can resolve a captured company's
    # domain through the existing DAT-010 logo.dev candidate flow and promote the
    # capture. It never fabricates a domain, never auto-accepts a provider
    # result, never merges an ambiguous identity, and never makes a contact
    # outreach-eligible; suppression stays authoritative. The lookup itself also
    # requires ``salesnav_domain_enrichment`` and a configured logo.dev key.
    #
    # Where it may run is decided in ``app/core/runtime.py``, not here. Local
    # development is unrestricted; staging permits it only with
    # ``automatic_company_domain_resolution``, ``salesnav_domain_enrichment`` and
    # a configured ``LOGO_DEV_API_KEY``, because without them the services above
    # fail closed and a hosted deployment would promote nothing while looking
    # healthy; production refuses it outright.
    contact_capture_promotion: bool = False
    # Automatic company-domain resolution for captured contacts (DAT-017A). Off
    # by default: while off, every captured company waits for an explicit
    # operator decision exactly as DAT-014 built it, and no decision record is
    # ever written. When on, the versioned resolution policy may decide a domain
    # without asking and reports one of three truthful states — ``confirmed``
    # (evidence that was already established), ``provisional`` (a provider-backed
    # candidate, good enough to start company research and nothing else), or
    # ``unresolved`` (never a fabricated domain). It never treats provider rank
    # as confirmation, never merges companies, and a provisional domain never
    # opens qualification, drafting, email discovery, campaign eligibility or
    # sending. The lookup half still also requires ``salesnav_domain_enrichment``
    # and a configured logo.dev key; without them the policy decides from stored
    # evidence only and stays truthful about the provider being unavailable.
    automatic_company_domain_resolution: bool = False
    # Operator workbench UI (server-rendered pages). Off by default so the UI
    # stays disabled until it is deliberately enabled for local operation.
    workbench: bool = False
    # Seller-side Knowledge Base (KB-001): the operator-maintained company
    # profile, offerings, proof points, restricted claims, personas, and the
    # campaign-to-offering association. Off by default; while off the pages
    # return 404 and the campaign editor shows no offerings section, exactly as
    # if the area did not exist. It also requires ``workbench``, because it is
    # part of that UI and inherits its local-only gate.
    #
    # Turning it on lets an operator record and read their own commercial
    # knowledge. It does not draft anything, does not call a model, does not
    # change how any prospect is researched, scored, verified, or suppressed,
    # and does not make any contact outreach-eligible. Associating an offering
    # with a campaign is a statement about what that campaign concerns; it never
    # writes email copy or selects a call to action.
    seller_knowledge_base: bool = False
    # Workbench Agent monitor and controls (MVP-01B): the operator control room
    # over the Phase 2 execution backbone — the Agent registry and controls,
    # Campaign execution, Campaign Contact pipeline state, durable Agent Jobs,
    # and the operator commands over them. Off by default; while off the area
    # renders one clean unavailable state and the navigation entry is disabled.
    # It also requires ``workbench``, because it is part of that UI and inherits
    # its local-only gate.
    #
    # Turning it on shows authoritative Phase 2 state and routes every operator
    # command through the Phase 2 service layer. It adds no execution capability
    # of its own: it cannot enable an Agent that has no adapter, cannot advance a
    # stage, and cannot release a suppression or any other domain block — those
    # remain authoritative above every control on the pages.
    agent_workbench: bool = False
    # Operator-driven Sales Navigator company-domain enrichment via the official
    # logo.dev Search Brands API (DAT-010). Off by default so the lookup UI and
    # any outbound call stay fully disabled until deliberately enabled for local
    # operation. Turning it on does not import anything and never auto-accepts a
    # candidate: the operator still confirms every domain by hand.
    salesnav_domain_enrichment: bool = False
    # The model fallback behind the logo.dev lookup: when the brand matcher returns
    # nothing usable, ask the local Claude CLI — with web search — for the
    # company's own domain, using the location already recorded on the capture to
    # tell same-named companies apart. Off by default, and it changes nothing while
    # off: a capture the provider could not resolve stays exactly as unresolved as
    # it was.
    #
    # Turning it on spends one model call per company the provider failed on, and
    # only for those. It cannot reach CONFIRMED — the policy caps a model answer at
    # provisional, which authorizes company research and nothing that spends money
    # or sends mail — it cannot overrule an approved mapping or an established
    # Company, and it is never consulted to break a tie between candidates that
    # aligned. It requires ``automatic_company_domain_resolution``, because it is
    # the second half of that path rather than a route of its own.
    model_company_domain_lookup: bool = False
    normalization: bool = False
    deduplication: bool = False
    suppressions: bool = False
    # Phase 2 — Email Verification
    email_generation: bool = False
    millionverifier: bool = False
    # Phase 3 — Lead Scoring
    scoring: bool = False
    # Phase 4 — Insights
    # Lets the Research Agent gather source-backed company facts. Production
    # Research uses the bounded Claude CLI web-research source and still does not
    # make a Contact outreach-eligible or write canonical Company fields. A
    # Campaign must additionally set the Agent config {"live": true}.
    company_research: bool = False
    # Legacy RES-002 key retained as the availability control for the required
    # primary Claude source. Off means Research is unavailable; it never restores
    # deterministic production crawling. Every claim must carry a source URL and
    # supporting text or it is discarded.
    research_claude_fallback: bool = False

    insights_research: bool = False
    # Company Intelligence (CI-001): versioned, evidence-linked classification of
    # a Company from Research evidence that has already been committed. Off by
    # default. While off, the Admin pages return 404, the producer refuses, the
    # worker exits, and the backfill will not enqueue -- the area behaves exactly
    # as if it did not exist.
    #
    # Turning it on lets a bounded producer read persisted dossiers and sourced
    # facts and store structured classifications with their evidence, their
    # uncertainty and their conflicts. It never browses, never rewrites a
    # canonical Company field, never edits Research, never makes a Contact
    # outreach-eligible, and cannot release a suppression or reach Sending. A
    # model-produced classification is not verified; only an operator decision
    # makes it operator-confirmed.
    company_intelligence: bool = False
    # Phase 5 — Claude Bridge
    claude_mcp_bridge: bool = False
    # Phase 6 — Draft & Approval
    drafting: bool = False
    # SEQ-001 — seven-message Personalization sequences.
    #
    # Off by default, and off means genuinely unchanged: the Personalization
    # Agent writes exactly the single DraftVersion it has always written, the
    # Review queue shows exactly the draft cards it has always shown, and no
    # sequence row is created on any code path.
    #
    # Turning it on is necessary but not sufficient. A Campaign must also opt in
    # through ``cadence_config["sequence"]["enabled"]``, so switching the
    # deployment flag does not silently change what every existing Campaign
    # produces. Both must be true before one Campaign Contact gets a sequence.
    #
    # What "on" adds is one generation unit producing seven immutable messages
    # for human review, and nothing else. It creates no Gmail draft, contacts no
    # Google API, schedules nothing and sends nothing — approval remains a
    # statement about text, not authority to deliver it.
    email_sequences: bool = False
    # #267 — one-click Gmail *draft* creation from a reviewed sequence.
    #
    # Off by default, and off means the area does not exist: the Gmail OAuth
    # routes return 404, the draft route returns 404, no Connect Gmail control
    # renders, and no Google endpoint is contacted on any code path.
    #
    # Turning it on lets an approved hosted operator authorize one Gmail mailbox
    # through a separate consent screen and create Gmail drafts from the exact
    # immutable sequence message versions on screen. It adds no sending
    # capability of any kind: the Gmail scope requested is ``gmail.compose``,
    # the adapter implements draft creation and a bounded draft lookup and
    # nothing else, and there is no code path in the application that can reach
    # ``users.messages.send`` or ``users.drafts.send``. Approval remains a
    # statement about text; creating a draft is a separate, explicit operator
    # action that still leaves the human to press send in Gmail.
    #
    # It also requires ``email_sequences``, because it acts on a sequence rather
    # than being a route of its own.
    gmail_drafts: bool = False
    # The Google Sheets add-on intake/output surface.
    #
    # Off by default, and off means the surface does not exist: every route under
    # ``/integrations/sheets`` answers 404, no Google key set is fetched and no
    # row is accepted on any code path.
    #
    # Turning it on adds one thin client and no new intelligence. A row submitted
    # from a sheet becomes a permanent Contact and a Campaign membership through
    # the same services the capture path uses, runs the same Agents under the
    # same Campaign switches, and returns the same verified address and the same
    # seven-message sequence the operator product shows. It creates no sending
    # capability, contacts no mailbox, and cannot bypass suppression, Campaign
    # access, provider authorization or the Agent controls — each of those is
    # decided by the code it calls, not by the caller.
    #
    # It also requires ``email_sequences``, because a "Ready" row is defined as a
    # usable verified address *and* a validated seven-message sequence, and
    # without that flag no sequence is ever produced.
    google_sheets_integration: bool = False
    # Phase 7 — Saleshandy
    saleshandy: bool = False

    def enabled(self) -> list[str]:
        """Return the names of currently enabled features (for audit/health)."""

        return [name for name, value in self.model_dump().items() if value]
