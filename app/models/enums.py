"""Enumerations used by the Phase 1 data model.

These are the *explicit, validated* value sets for campaign status, import
processing, row outcomes, suppression, and the contact workflow. They are stored
as native PostgreSQL enum types (see the model columns), so the database itself
rejects arbitrary strings (AGENTS.md: "Use explicit workflow states; reject
illegal transitions"; DAT-001 / CMP-002).
"""

from __future__ import annotations

import enum


class CampaignStatus(enum.StrEnum):
    """Lifecycle of a campaign shell that receives imports."""

    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class AgentIdentifier(enum.StrEnum):
    """Stable identifiers for the operator-visible outbound Agents.

    These values are a public contract. Display names may change, but jobs,
    Campaign overrides, and pipeline history always use these identifiers.
    """

    CAPTURE = "capture"
    IDENTITY = "identity"
    COMPANY = "company"
    RESEARCH = "research"
    EMAIL = "email"
    VERIFICATION = "verification"
    INSIGHTS = "insights"
    PERSONALIZATION = "personalization"
    SENDING = "sending"


class AgentControlStatus(enum.StrEnum):
    """Global or Campaign-level execution control for one Agent."""

    ENABLED = "enabled"
    PAUSED = "paused"
    DISABLED = "disabled"


class CampaignMembershipStatus(enum.StrEnum):
    """Lifecycle of a Contact's membership in one Campaign."""

    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class CampaignContactEligibility(enum.StrEnum):
    """Campaign-specific eligibility, independent of pipeline execution."""

    UNKNOWN = "unknown"
    ELIGIBLE = "eligible"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"


class PipelineStageStatus(enum.StrEnum):
    """Durable operator-visible state of one Campaign Contact Agent stage."""

    WAITING = "waiting"
    RUNNING = "running"
    PAUSED = "paused"
    RETRYING = "retrying"
    FAILED = "failed"
    COMPLETED = "completed"
    DISABLED = "disabled"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class PipelineEventType(enum.StrEnum):
    """Append-only facts from which pipeline state can be explained."""

    ENROLLED = "enrolled"
    MEMBERSHIP_PAUSED = "membership_paused"
    MEMBERSHIP_RESUMED = "membership_resumed"
    MEMBERSHIP_ARCHIVED = "membership_archived"
    STAGE_WAITING = "stage_waiting"
    JOB_QUEUED = "job_queued"
    JOB_LEASED = "job_leased"
    JOB_STARTED = "job_started"
    STAGE_COMPLETED = "stage_completed"
    STAGE_SKIPPED = "stage_skipped"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    AGENT_PAUSED = "agent_paused"
    AGENT_DISABLED = "agent_disabled"
    ELIGIBILITY_BLOCKED = "eligibility_blocked"
    ELIGIBILITY_RESTORED = "eligibility_restored"
    JOB_CANCELLED = "job_cancelled"


class CaptureCampaignFilingStatus(enum.StrEnum):
    """Outcome of the optional Campaign filing attached to a capture."""

    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"


class ImportBatchStatus(enum.StrEnum):
    """Processing state of a single CSV import batch."""

    PENDING = "pending"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"


class ImportRowOutcome(enum.StrEnum):
    """Per-row outcome after staged validation.

    ``PENDING`` is the state at raw capture, before validation runs. The five
    terminal outcomes are mutually exclusive and together account for every
    imported row, so no malformed row is ever silently dropped (DAT-002).

    ``AMBIGUOUS`` marks a row whose identity match is uncertain (several existing
    contacts share its natural key). Such a row is neither merged nor silently
    accepted: no contact is created, the reason is recorded, and the row waits
    for human review in the workbench (DAT-004).
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    SUPPRESSED = "suppressed"
    AMBIGUOUS = "ambiguous"


class DedupMatchType(enum.StrEnum):
    """How a duplicate row was matched to an existing contact.

    Only exact, deterministic matches are used (DAT-004): a shared normalized
    email, or an exact normalized natural key (first name + last name + company
    domain). Similar-but-not-equal names or companies never merge.
    """

    EMAIL = "email"
    NATURAL_KEY = "natural_key"


class IdentityResolutionType(enum.StrEnum):
    """How an operator resolved an ambiguous imported identity or duplicate pair.

    An ambiguous import row (several existing contacts share its natural key) is
    never merged silently; the operator makes one explicit, audited decision:

    * ``ASSIGN_EXISTING`` — the row is the same person as one chosen existing
      contact; it is linked to that contact (membership + provenance), no new
      contact is created.
    * ``CREATE_NEW`` — none of the candidates match; a new contact is created.
    * ``MARK_SEPARATE`` — the row is a new, distinct person deliberately recorded
      as separate from the shown candidates (a confirmed non-match). A new
      contact is created and the candidates it was distinguished from are
      recorded on the resolution. This resolves the *present row only*: the
      distinction is intentionally not used to auto-suppress future matching, so
      a later import sharing the same natural key can still be flagged ambiguous
      for a fresh, explicit decision (conservative by design).
    * ``MERGE`` — two *existing* contacts are confirmed duplicates and merged
      into a single survivor under a deterministic transfer policy.
    """

    ASSIGN_EXISTING = "assign_existing"
    CREATE_NEW = "create_new"
    MARK_SEPARATE = "mark_separate"
    MERGE = "merge"


class SuppressionType(enum.StrEnum):
    """The identity dimension a suppression entry applies to."""

    EMAIL = "email"
    DOMAIN = "domain"


class SuppressionReason(enum.StrEnum):
    """Why an identity is suppressed. Opt-outs and hard bounces never expire.

    ``LEGAL_COMPLIANCE`` covers a legal or compliance hold (e.g. a GDPR erasure
    request or a jurisdiction the campaign may not contact). New reasons are added
    here and take effect everywhere the ledger is consulted (DAT-006).
    """

    OPT_OUT = "opt_out"
    HARD_BOUNCE = "hard_bounce"
    CUSTOMER = "customer"
    COMPETITOR = "competitor"
    INTERNAL_EXCLUSION = "internal_exclusion"
    LEGAL_COMPLIANCE = "legal_compliance"
    MANUAL = "manual"


# Precedence when one identity carries several active suppressions: the strongest
# reason is the one reported as the blocking reason. Opt-out and hard bounce are
# the hardest (never expire, deliverability/consent), then legal, then the
# commercial/manual reasons. A total, deterministic order (DAT-006).
SUPPRESSION_REASON_PRECEDENCE: tuple[SuppressionReason, ...] = (
    SuppressionReason.OPT_OUT,
    SuppressionReason.HARD_BOUNCE,
    SuppressionReason.LEGAL_COMPLIANCE,
    SuppressionReason.COMPETITOR,
    SuppressionReason.CUSTOMER,
    SuppressionReason.INTERNAL_EXCLUSION,
    SuppressionReason.MANUAL,
)


class SuppressionEventType(enum.StrEnum):
    """A change to one suppression record's lifecycle, kept as history (DAT-006).

    Unsuppressing never deletes a suppression; it appends a ``DEACTIVATED`` event
    and flips the record inactive, so the full history — who suppressed an
    identity, why, when, and any later reactivation — is always preserved.
    """

    CREATED = "created"
    REACTIVATED = "reactivated"
    DEACTIVATED = "deactivated"


class ContactWorkflowState(enum.StrEnum):
    """Explicit workflow state of a contact *within a campaign*.

    Only the states reachable at the import stage of the first launch are
    defined here. Later phases (verification, scoring, research, drafting,
    scheduling) extend :data:`ALLOWED_CONTACT_TRANSITIONS`; they do not need new
    global state. ``SUPPRESSED`` and ``EXCLUDED`` are terminal for outreach.
    """

    IMPORTED = "imported"
    AWAITING_VERIFICATION = "awaiting_verification"
    SUPPRESSED = "suppressed"
    EXCLUDED = "excluded"


# Legal contact-state transitions. A transition is allowed only if the target
# appears in the set for the current state. Terminal states have an empty set.
# Kept here (next to the enum) so the state machine has one authoritative source.
ALLOWED_CONTACT_TRANSITIONS: dict[ContactWorkflowState, frozenset[ContactWorkflowState]] = {
    ContactWorkflowState.IMPORTED: frozenset(
        {
            ContactWorkflowState.AWAITING_VERIFICATION,
            ContactWorkflowState.SUPPRESSED,
            ContactWorkflowState.EXCLUDED,
        }
    ),
    ContactWorkflowState.AWAITING_VERIFICATION: frozenset(
        {
            ContactWorkflowState.SUPPRESSED,
            ContactWorkflowState.EXCLUDED,
        }
    ),
    ContactWorkflowState.SUPPRESSED: frozenset(),
    ContactWorkflowState.EXCLUDED: frozenset(),
}


class ImportSourceFormat(enum.StrEnum):
    """Authorized import sources for the first launch.

    ``CSV`` and ``XLSX`` are the authorized spreadsheet upload formats. Legacy
    formats (.xls), Google Sheets direct import, and other spreadsheet formats
    are intentionally out of scope until explicitly approved.

    ``SALES_NAVIGATOR`` marks a batch staged from the operator-driven Sales
    Navigator capture extension (DAT-009). It is not a spreadsheet: the records
    arrive as an authorized JSON batch over the local intake endpoint and are
    captured verbatim as raw rows. It never bypasses the staged-import gates — a
    Sales Navigator batch is staged for operator preview exactly like an uploaded
    file, and creates no contacts until the operator confirms it downstream.
    """

    CSV = "csv"
    XLSX = "xlsx"
    SALES_NAVIGATOR = "sales_navigator"


class ImportedEmailSlot(enum.StrEnum):
    """Which address of an imported row one evidence record describes (IMP-001).

    A vendor export routinely carries more than one address per person, and they
    are not interchangeable: the primary is the one the operator is asking us to
    use, and the others are alternatives nobody has chosen. Keeping them in one
    table separated by slot — rather than promoting a secondary into the primary
    column when the primary looks worse — is what makes "we did not guess which
    address to use" a property of the schema instead of a promise in a docstring.
    """

    PRIMARY = "primary"
    SECONDARY = "secondary"
    TERTIARY = "tertiary"


class ImportedEmailStageOutcome(enum.StrEnum):
    """What the Email stage did with an operator-supplied imported address.

    Deliberately a separate vocabulary from anything the Email Agent's discovery
    path produces. ``imported_email_accepted`` says exactly one thing: an address
    that arrived in a file was taken as the campaign's address for this person.
    It is not a claim that the mailbox exists, that a provider verified it, or
    that it is deliverable — no candidate was generated, no pattern was applied,
    and no provider was called, so none of those claims could be true.

    ``imported_email_rejected`` covers a supplied address the import refused:
    malformed syntax, or a suppressed identity. The row keeps the raw value as
    evidence; the address never becomes the campaign's address.
    """

    IMPORTED_EMAIL_ACCEPTED = "imported_email_accepted"
    IMPORTED_EMAIL_REJECTED = "imported_email_rejected"


class ImportedVerificationOutcome(enum.StrEnum):
    """What the Verification stage did for an imported address (IMP-001).

    ``verification_bypassed_imported_email`` is a truthful *absence*: no
    MillionVerifier call, no ZeroBounce call, no provider of any kind, and
    therefore no evidence about the mailbox. It exists as its own durable value
    precisely so that a bypassed Contact can never be read as a verified one —
    :class:`EmailVerificationResult` remains reserved for answers a provider
    actually gave about an exact address, and no import ever writes one.

    ``verification_not_performed`` is the state of a slot that never reached the
    stage at all (a rejected primary, or a retained secondary/tertiary).
    """

    VERIFICATION_BYPASSED_IMPORTED_EMAIL = "verification_bypassed_imported_email"
    VERIFICATION_NOT_PERFORMED = "verification_not_performed"


class EnrichmentLookupStatus(enum.StrEnum):
    """State of a company-domain lookup against logo.dev (DAT-010).

    These states are truthful and distinct so the operator always sees whether a
    lookup has run and how it turned out. ``OK`` means candidates were returned;
    it never means a domain was chosen — the operator confirms every domain by
    hand. ``NOT_STARTED`` is the initial state before any lookup; ``ERROR`` covers
    an unexpected failure that is neither a clean no-match nor a recognised
    provider condition.
    """

    NOT_STARTED = "not_started"
    OK = "ok"
    NO_MATCH = "no_match"
    API_UNAVAILABLE = "api_unavailable"
    RATE_LIMITED = "rate_limited"
    MALFORMED = "malformed"
    ERROR = "error"


class EnrichmentConfirmationStatus(enum.StrEnum):
    """Whether an operator has decided this company's domain (DAT-010).

    ``UNCONFIRMED`` is the default: no domain is applied and the company's rows
    stay truthfully rejected for a missing domain. ``CONFIRMED`` means the
    operator explicitly chose a domain (a candidate or a manual override).
    ``UNRESOLVED`` means the operator explicitly decided to leave the company
    without a domain; its rows remain rejected, but the decision is now recorded
    rather than merely pending.
    """

    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    UNRESOLVED = "unresolved"


class EnrichmentConfirmationSource(enum.StrEnum):
    """How a confirmed domain was chosen (DAT-010 provenance).

    Recorded separately from the immutable raw capture values so the origin of an
    applied domain is always auditable: a logo.dev ``CANDIDATE`` the operator
    selected, a ``MANUAL`` domain the operator typed, ``UNRESOLVED`` when the
    operator deliberately left the company without a domain, or
    ``PRIOR_MAPPING`` (DAT-014) when a domain was reused from an EARLIER
    operator confirmation of the same normalized company.

    ``PRIOR_MAPPING`` and ``AUTOMATIC_POLICY`` are the only non-interactive
    sources, and neither is an exception to the rule that a domain is never
    guessed. ``PRIOR_MAPPING`` replays a decision the operator already made for
    the same normalized company. ``AUTOMATIC_POLICY`` (DAT-017A) records a
    CONFIRMED decision reached by the versioned company-domain resolution
    policy, which confirms only from evidence that was ALREADY established — an
    approved mapping, or a permanent Company whose identity and domain are both
    on record. A provider's top-ranked name match never qualifies for either:
    provider-backed evidence reaches ``provisional`` at best, and a provisional
    decision deliberately writes no confirmation here at all, so it can never
    become a reusable approved mapping for the next capture.
    """

    CANDIDATE = "candidate"
    MANUAL = "manual"
    UNRESOLVED = "unresolved"
    PRIOR_MAPPING = "prior_mapping"
    AUTOMATIC_POLICY = "automatic_policy"


class CompanyResolutionOutcome(enum.StrEnum):
    """How the COMPANY behind a contact capture was resolved (DAT-014).

    Deliberately separate from :class:`ContactPromotionOutcome`: knowing which
    company a person works for and knowing which person they are, are two
    different questions with different failure modes, and collapsing them into
    one result would hide which of the two actually blocked a promotion.

    ``EXISTING_COMPANY_RESOLVED`` is the only outcome reachable without asking
    the provider: a previously CONFIRMED decision for the same normalized
    company already names the domain. Everything a provider returns is a
    *candidate*, because a top-ranked name match is not evidence of identity.

    ``DOMAIN_PROVISIONAL`` (DAT-017A) is the one outcome that authorizes a
    promotion on provider-backed evidence alone, and it says so out loud rather
    than borrowing a confirmed-sounding name. It permits exactly two things —
    creating/reusing the permanent Company and linking the Contact — so company
    research can start. It authorizes nothing further: qualification, drafting,
    email discovery, campaign eligibility and sending all stay closed until the
    identity is confirmed (see :mod:`app.services.resolution.gates`).
    """

    PENDING_LOOKUP = "pending_lookup"
    EXISTING_COMPANY_RESOLVED = "existing_company_resolved"
    DOMAIN_CANDIDATE_CONFIRMED = "domain_candidate_confirmed"
    DOMAIN_PROVISIONAL = "domain_provisional"
    CANDIDATE_REVIEW_REQUIRED = "candidate_review_required"
    MULTIPLE_CANDIDATES_REVIEW_REQUIRED = "multiple_candidates_review_required"
    NO_CANDIDATE = "no_candidate"
    COMPANY_IDENTITY_AMBIGUOUS = "company_identity_ambiguous"
    LOOKUP_UNAVAILABLE = "lookup_unavailable"
    LEFT_UNRESOLVED = "left_unresolved"


class ContactPromotionOutcome(enum.StrEnum):
    """What happened to the PERSON when a capture was promoted (DAT-014).

    A promotion may create a canonical contact or link an existing one, but it
    never merges on weak evidence: an ambiguous identity blocks the promotion so
    an operator decides. ``SUPPRESSED`` is authoritative — a suppressed identity
    is never promoted, and the suppression itself is never touched.
    """

    PENDING = "pending"
    CONTACT_CREATED = "contact_created"
    CONTACT_EXACT_MATCH_LINKED = "contact_exact_match_linked"
    CONTACT_IDENTITY_AMBIGUOUS = "contact_identity_ambiguous"
    SUPPRESSED = "suppressed"
    ALREADY_PROMOTED = "already_promoted"
    PROMOTION_BLOCKED = "promotion_blocked"
    PROMOTION_FAILED = "promotion_failed"


class EmailVerificationResult(enum.StrEnum):
    """Address-level outcome of an exact full-address verification (VER-002).

    These are the mailbox-level answers that constitute *durable evidence about
    one exact address* and are therefore cached. Catch-all, unknown, and
    disposable are deliberately distinct from valid/invalid so that uncertainty
    or a risky mailbox can never be silently treated as a confirmed valid mailbox
    (AGENTS.md). Provider errors, timeouts, and insufficient-credit conditions are
    *not* represented here: they are job/operational outcomes, not evidence about
    the address, and never create an :class:`ExactEmailVerification` row.
    """

    VALID = "valid"
    INVALID = "invalid"
    CATCH_ALL = "catch_all"
    UNKNOWN = "unknown"
    DISPOSABLE = "disposable"


class EmailCandidateSource(enum.StrEnum):
    """Where a candidate exact address came from (EML-002/005).

    ``IMPORTED`` is an exact address supplied by the import itself — it is still
    verified, never trusted on faith. ``GENERATED`` is a deterministic
    pattern-derived candidate produced by the versioned generation engine.
    """

    IMPORTED = "imported"
    GENERATED = "generated"


class AgentJobStatus(enum.StrEnum):
    """Stored lifecycle of a durable Agent job.

    The original verification queue established the first six labels. They stay
    unchanged in PostgreSQL so existing rows and migrations remain safe.
    ``LEASED`` separates a generic worker claim from committed execution, and
    ``PAUSED`` makes operator control durable. API serializers expose the
    canonical queued/running/retrying/completed vocabulary.
    """

    PENDING = "pending"
    LEASED = "leased"
    IN_PROGRESS = "in_progress"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


# Backward-compatible import name for the existing verification service.
VerificationJobStatus = AgentJobStatus


class EmailPreciseStatus(enum.StrEnum):
    """The precise underlying verification status beside a prospect's email.

    These preserve the exact meaning defined in issue #137 and never collapse
    distinct conditions together — a provider error, an insufficient-credit
    exception, a timeout, an unknown result, and a catch-all are all separate
    states even though several share the same amber *visual* treatment. The
    deterministic map from a precise status to one of the four visible states is
    :data:`PRECISE_TO_VISUAL`.
    """

    # --- Pending family (neutral / clock) ---
    UNVERIFIED = "unverified"
    QUEUED = "queued"
    CHECKING = "checking"
    RETRY_SCHEDULED = "retry_scheduled"
    STALE_RECHECK_SCHEDULED = "stale_recheck_scheduled"
    # --- Successful (green) ---
    VALID = "valid"
    # --- Failure (red) ---
    INVALID = "invalid"
    # --- Warning (amber) — each cause kept distinct ---
    CATCH_ALL = "catch_all"
    UNKNOWN = "unknown"
    DISPOSABLE = "disposable"
    ROLE_BASED = "role_based"
    PROVIDER_ERROR = "provider_error"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    STALE_EVIDENCE = "stale_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"


class EmailVisualStatus(enum.StrEnum):
    """The four visible states shown as a compact status icon (#137)."""

    PENDING = "pending"
    SUCCESSFUL = "successful"
    FAILURE = "failure"
    WARNING = "warning"


# Deterministic, total mapping from every precise status to its single visible
# state. Kept next to the enums as the one authoritative source so the icon, the
# accessible label, and any filter all agree. Provider errors, insufficient
# credits, timeouts, unknown, and catch-all map to WARNING — never to FAILURE
# (which means a definitively bad mailbox) and never to SUCCESSFUL.
PRECISE_TO_VISUAL: dict[EmailPreciseStatus, EmailVisualStatus] = {
    EmailPreciseStatus.UNVERIFIED: EmailVisualStatus.PENDING,
    EmailPreciseStatus.QUEUED: EmailVisualStatus.PENDING,
    EmailPreciseStatus.CHECKING: EmailVisualStatus.PENDING,
    EmailPreciseStatus.RETRY_SCHEDULED: EmailVisualStatus.PENDING,
    EmailPreciseStatus.STALE_RECHECK_SCHEDULED: EmailVisualStatus.PENDING,
    EmailPreciseStatus.VALID: EmailVisualStatus.SUCCESSFUL,
    EmailPreciseStatus.INVALID: EmailVisualStatus.FAILURE,
    EmailPreciseStatus.CATCH_ALL: EmailVisualStatus.WARNING,
    EmailPreciseStatus.UNKNOWN: EmailVisualStatus.WARNING,
    EmailPreciseStatus.DISPOSABLE: EmailVisualStatus.WARNING,
    EmailPreciseStatus.ROLE_BASED: EmailVisualStatus.WARNING,
    EmailPreciseStatus.PROVIDER_ERROR: EmailVisualStatus.WARNING,
    EmailPreciseStatus.INSUFFICIENT_CREDITS: EmailVisualStatus.WARNING,
    EmailPreciseStatus.STALE_EVIDENCE: EmailVisualStatus.WARNING,
    EmailPreciseStatus.CONFLICTING_EVIDENCE: EmailVisualStatus.WARNING,
}


class UsageCacheStatus(enum.StrEnum):
    """Whether a usage ledger entry served a cache hit, a real call, or neither.

    Provider-neutral: ``HIT`` means fresh evidence was reused and no external call
    (or charge) occurred; ``MISS`` means a real provider request was attempted;
    ``NOT_APPLICABLE`` covers operations without a cache dimension.
    """

    HIT = "hit"
    MISS = "miss"
    NOT_APPLICABLE = "not_applicable"


class UsageChargeStatus(enum.StrEnum):
    """Whether an external charge is confirmed, uncertain, or absent (VER-006+).

    ``CONFIRMED`` means the provider billed for this request (for MillionVerifier:
    an ok/invalid/disposable result). ``NONE`` means no charge (a cache hit, a free
    result such as catch-all/unknown, or a pre-charge failure). ``UNCERTAIN`` means
    a paid call may have completed but could not be confirmed — for example a
    worker that died mid-flight and left its job to be reclaimed. Uncertainty is
    recorded, never silently treated as free or as charged.
    """

    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"
    NONE = "none"


class VerificationUsageEventType(enum.StrEnum):
    """Recorded verification usage and exception events (VER-006).

    Makes provider spend and every exception visible and auditable: whether a
    paid call was made, whether cached evidence was reused instead, and each
    distinct failure mode. ``credited`` on the row records whether MillionVerifier
    actually charged (only ok/invalid/disposable are billed).
    """

    CALL_MADE = "call_made"
    CACHE_REUSE = "cache_reuse"
    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    RETRY_SCHEDULED = "retry_scheduled"
    STALE_DETECTED = "stale_detected"
    CONFLICT_DETECTED = "conflict_detected"
    RECOVERED = "recovered"


class InsightSubject(enum.StrEnum):
    """Whether a research insight is about a company or an individual contact."""

    COMPANY = "company"
    CONTACT = "contact"


class InsightKind(enum.StrEnum):
    """Whether a claim reports evidence directly or interprets that evidence."""

    FACT = "fact"
    INTERPRETATION = "interpretation"


class InsightState(enum.StrEnum):
    """What the stored evidence currently says about an insight."""

    SUPPORTED = "supported"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class ScoreType(enum.StrEnum):
    """The two launch scores: computed before and after deep research."""

    INITIAL_FIT = "initial_fit"
    OUTREACH_READINESS = "outreach_readiness"


class ApprovalStatus(enum.StrEnum):
    """State of an approval that references one exact immutable draft version.

    Editing a draft creates a new version and INVALIDATES the prior approval.
    """

    APPROVED = "approved"
    INVALIDATED = "invalidated"


class LinkedInSnapshotOutcome(enum.StrEnum):
    """Truthful backend outcome of ingesting one LinkedIn capture snapshot.

    ``STORED`` is the DAT-012D baseline: the snapshot is persisted immutably and
    nothing else happens. The reconciliation outcomes are produced once exact-URL
    matching runs (DAT-012E, and every contact-first capture in DAT-013);
    ``rejected`` payloads are never persisted, so no member exists for them.

    ``DUPLICATE_IN_SUBMISSION`` (DAT-013) marks the second and later captures of
    the SAME person inside one contact-first submission. The evidence is still
    stored; only the first capture is reconciled, so one submission can never
    refresh or stage the same person twice.
    """

    STORED = "stored"
    EXACT_MATCH_REFRESHED = "exact_match_refreshed"
    EXACT_MATCH_UNCHANGED = "exact_match_unchanged"
    CONTACT_CREATED = "created"
    UNMATCHED_STAGED = "unmatched_staged"
    AMBIGUOUS_REVIEW = "ambiguous_review"
    SUPPRESSED = "suppressed"
    DUPLICATE_IN_SUBMISSION = "duplicate_in_submission"


class QAOutcome(enum.StrEnum):
    """High-level outcome of one versioned QA-policy evaluation (DAT-012F).

    These are policy *recommendations* backed by evidence — never hard
    eligibility gates. A QA outcome cannot unsuppress, verify an email, approve
    a draft, alter sending limits, or schedule outreach.
    """

    LIVE_CONTACT = "live_contact"
    LEFT_COMPANY = "left_company"
    TITLE_CHANGED = "title_changed"
    COMPANY_UNRESOLVED = "company_unresolved"
    MULTIPLE_CURRENT_ROLES = "multiple_current_roles"
    EXPERIENCE_MISSING = "experience_missing"
    EXPERIENCE_UNRECOGNIZED = "experience_unrecognized"
    TENURE_REVIEW = "tenure_review"
    NON_FULL_TIME_REVIEW = "non_full_time_review"
    OPEN_TO_WORK_REVIEW = "open_to_work_review"
    LOW_CONNECTIONS_REVIEW = "low_connections_review"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NEEDS_REVIEW = "needs_review"


class CaptureIdentityState(enum.StrEnum):
    """How far one person has travelled from observation to canonical record.

    This is the *identity* dimension only. It says nothing about whether the
    person is researched, qualified, emailable, or safe to contact — those are
    separate dimensions with their own vocabularies (APP-001, ADR 0002).

    It is derived from the capture evidence, never stored on the contact, so
    there is exactly one source of truth for it.
    """

    CANONICAL = "canonical"
    """A permanent contact row exists."""

    AWAITING_COMPANY = "awaiting_company"
    """A permanent Contact exists, but its company domain is unresolved.

    Resolution runs through the Company evidence and decision flow. Missing
    fields remain NULL and downstream Company/email work stays blocked.
    """

    AMBIGUOUS_IDENTITY = "ambiguous_identity"
    """The capture matched more than one existing contact and needs a decision.

    An exact normalized profile URL may match automatically. A name, title,
    company, location or headline may not.
    """

    REJECTED = "rejected"
    """Suppressed at capture time, or a duplicate within one submission."""


class ResearchState(enum.StrEnum):
    """Progress of company and contact research for one person.

    No research engine exists yet (APP-004 owns it), so every record currently
    reports ``NOT_REQUESTED``. The dimension is defined here so the CRM shows a
    truthful empty state instead of implying research that has not happened.
    """

    NOT_REQUESTED = "not_requested"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    STALE = "stale"


class QualificationState(enum.StrEnum):
    """Whether a person has been judged a fit, and how confidently.

    Distinct from :class:`QAOutcome`, which is a versioned *employment* QA
    signal over capture evidence. A qualification assessment is the broader
    judgement APP-006 will build; until then every record reports
    ``NOT_ASSESSED``.
    """

    NOT_ASSESSED = "not_assessed"
    PENDING = "pending"
    QUALIFIED = "qualified"
    BORDERLINE = "borderline"
    DISQUALIFIED = "disqualified"
    NEEDS_REVIEW = "needs_review"


class CompanyFieldSource(enum.StrEnum):
    """Where an observation of a canonical company field came from (APP-003).

    Deliberately describes the *kind* of origin, never the vendor. A dossier
    claim is ``RESEARCH_DOSSIER`` whether it was produced by a crawler, a model,
    a paid API or an operator pasting a payload — swapping the research
    implementation must not require a schema migration or a new member here.

    ``MANUAL`` is separate from every automatic source because an operator
    decision outranks all of them, and the ledger has to be able to say so.
    """

    MANUAL = "manual"
    # A LinkedIn company page the operator captured (DAT-012G evidence).
    LINKEDIN_COMPANY_SNAPSHOT = "linkedin_company_snapshot"
    # Carried across when a capture was promoted into a contact (DAT-014).
    CAPTURE_PROMOTION = "capture_promotion"
    # Claimed by a structured reading of a research submission (APP-004 fills
    # these; APP-003 only provides the landing zone).
    RESEARCH_DOSSIER = "research_dossier"
    # Imported alongside a contact spreadsheet.
    IMPORT = "import"


class DossierSection(enum.StrEnum):
    """The nine sections a company dossier may address (APP-003).

    This is the display and storage boundary. It is a closed set on purpose: a
    research implementation that wants a tenth section needs a schema change and
    a review, not a new key in a blob. Each member maps to one nullable column on
    :class:`~app.models.company_dossier.CompanyDossierVersion`, where NULL means
    "this version did not address it" and an empty value means "it looked and
    found nothing".
    """

    OVERVIEW = "overview"
    PRODUCTS_SERVICES = "products_services"
    INDUSTRIES = "industries"
    GEOGRAPHY = "geography"
    LEADERSHIP = "leadership"
    ACTIVITY_SIGNALS = "activity_signals"
    PUBLIC_CONTACTS = "public_contacts"
    SOURCES = "sources"
    UNKNOWNS = "unknowns"


class CompanyConflictKind(enum.StrEnum):
    """A visible, reviewable disagreement about company identity (APP-003).

    Every member is *derived* from records that already exist rather than stored
    in a queue of its own. That is deliberate: a second review architecture
    alongside the import-row queue would be two places to look and two places to
    forget, and a conflict that no longer holds should stop being reported the
    moment the underlying rows agree — which a derived view gets for free and a
    stored queue does not.

    None of these block anything. They are surfaced so an operator can decide,
    because a company whose sources disagree about its domain is a fact worth
    knowing rather than an error worth swallowing.
    """

    # A linked contact's captured company_domain disagrees with the domain of
    # the company it is linked to. The most common cause is a company whose
    # canonical domain was corrected after the contact was created.
    CONTACT_DOMAIN_MISMATCH = "contact_domain_mismatch"
    # A contact still linked only by domain string, with no company_id. Legacy
    # rows and rows the backfill declined to guess at.
    CONTACT_LINK_UNRESOLVED = "contact_link_unresolved"
    # Another company row claims the same LinkedIn company identifier.
    LINKEDIN_ID_SHARED = "linkedin_id_shared"
    # A captured LinkedIn company page matched to this company states a website
    # domain that is not this company's domain.
    SNAPSHOT_DOMAIN_MISMATCH = "snapshot_domain_mismatch"
    # This company has no domain at all, so domain-based identity cannot apply.
    NO_CANONICAL_DOMAIN = "no_canonical_domain"


class DomainResolutionState(enum.StrEnum):
    """How certain the system is about a captured employer's domain (DAT-017A).

    Three states, and the middle one is the whole point. Before DAT-017A a
    captured company was either operator-confirmed or it was nothing, so the
    only way to get a normal LinkedIn capture moving was for a human to approve
    every domain by hand. Collapsing that middle ground into "confirmed" would
    have bought throughput by lying; leaving it at "unresolved" kept the lie out
    but kept the operator in.

    * ``CONFIRMED`` — deterministic evidence that was already established
      (an approved mapping, or a permanent Company whose identity and domain are
      both on record) names this domain. Good enough for normal downstream use.
    * ``PROVISIONAL`` — a provider-backed candidate is likely enough to start
      company research and no better evidence contradicts it, but nothing has
      corroborated it independently. It authorizes research and nothing else.
    * ``UNRESOLVED`` — evidence is missing, ambiguous, conflicting, invalid, or
      the provider failed. No domain is selected, and none is invented.

    ``PROVISIONAL`` is deliberately not a weaker ``CONFIRMED``: the difference
    is enforced by :mod:`app.services.resolution.gates`, not left to whoever
    reads the value next.
    """

    CONFIRMED = "confirmed"
    PROVISIONAL = "provisional"
    UNRESOLVED = "unresolved"


class DomainResolutionKind(enum.StrEnum):
    """Why a company-domain resolution decision was written (DAT-017A).

    Decisions are append-only. A correction never edits or deletes the decision
    it disagrees with — it supersedes it — so this records which of the three
    ways produced each row and the earlier evidence stays readable.
    """

    # The first automatic evaluation for a capture.
    AUTOMATIC = "automatic"
    # A later automatic re-evaluation that reached a DIFFERENT answer. An
    # identical re-evaluation writes nothing at all, so this never accumulates
    # duplicate rows saying the same thing.
    RECALCULATION = "recalculation"
    # An operator disagreed with the automatic decision and said so explicitly.
    OPERATOR_CORRECTION = "operator_correction"


class SellerRecordState(enum.StrEnum):
    """Whether a seller-side knowledge record may still be used (KB-001).

    Seller knowledge is operator-authored, and an operator who stops standing
    behind a statement needs somewhere to put it that is not deletion. A
    campaign that already referenced an offering must keep reading the same
    row afterwards, so the knowledge base archives and never deletes.

    * ``ACTIVE`` — the operator stands behind this record today. It counts
      towards context readiness and future context assembly may read it.
    * ``ARCHIVED`` — the operator has withdrawn it. It stays readable, keeps
      every association it already had, and still resolves for any campaign
      that references it, but it is excluded from readiness counts and from
      the default pickers used when composing new context.

    Archiving is reversible. It is deliberately not a third "draft" state:
    entering a record IS the approval (KB-001), so there is no stage between
    "an operator wrote this" and "this may be used".
    """

    ACTIVE = "active"
    ARCHIVED = "archived"


class SellerOfferingType(enum.StrEnum):
    """What kind of commercial item a seller offering is (KB-001).

    A closed set, because the category is used for organisation and reporting
    and free text would make those counts meaningless. ``OTHER`` exists so an
    operator is never forced to mis-file something; the offering's own notes
    carry the detail. Adding a member later is one ``ALTER TYPE ... ADD VALUE``
    and does not invalidate stored rows.
    """

    PRODUCT = "product"
    SERVICE = "service"
    SOLUTION = "solution"
    SUBSCRIPTION = "subscription"
    RESEARCH_REPORT = "research_report"
    RESEARCH_ENGAGEMENT = "research_engagement"
    OTHER = "other"


class SellerClaimScope(enum.StrEnum):
    """How widely a restricted claim applies (KB-001).

    * ``GLOBAL`` — the restriction holds for everything the system may write,
      whatever a campaign is selling. It carries no offering associations, and
      the service refuses to create one.
    * ``OFFERING`` — the restriction applies only to the offerings it is linked
      to. It is created before those links exist and may legitimately sit
      unlinked, in which case it restricts nothing; the Knowledge Base says so
      on the page rather than pretending otherwise. Nothing forces a link,
      because an operator who has written the rule but not yet decided where it
      applies has done something useful and should not lose it.

    The distinction is kept explicit rather than inferred from "does this row
    have links", precisely because an unlinked offering-scoped claim is a real
    state: inferring the scope would make it indistinguishable from a global
    rule and silently widen it.
    """

    GLOBAL = "global"
    OFFERING = "offering"


class ContextReadinessState(enum.StrEnum):
    """Whether a single piece of seller context exists (KB-001).

    This is a description of what an operator has entered, not a score, not a
    quality judgement, and not permission to do anything. It is computed by
    deterministic Python over stored columns (``app.services.seller.readiness``)
    and never by a model.

    * ``CONFIGURED`` — everything this item asks for is present.
    * ``INCOMPLETE`` — some of it is present and some named part is missing.
      The reason always says which part.
    * ``NOT_CONFIGURED`` — nothing has been entered for this item yet.
    * ``NOT_APPLICABLE`` — the item cannot apply to this subject at all, for a
      structural reason the reason string states. It is not a pass and not a
      failure; it means the question does not arise here.

    ``INCOMPLETE`` and ``NOT_CONFIGURED`` are deliberately distinct, for the
    same reason a NULL dossier section is not an empty one: "started and
    unfinished" and "never begun" call for different actions.
    """

    CONFIGURED = "configured"
    INCOMPLETE = "incomplete"
    NOT_CONFIGURED = "not_configured"
    NOT_APPLICABLE = "not_applicable"


class LinkedInIdentifierKind(enum.StrEnum):
    """The two forms a LinkedIn person identity arrives in (DAT-019).

    They are deliberately separate kinds rather than one "linkedin id" column,
    because they have different semantics and different comparison rules.

    ``PUBLIC_VANITY_URL`` is the published or directly observed ``/in/`` URL. It
    is normalized before storage and compared case-insensitively, as LinkedIn
    handles are.

    ``SALESNAV_MEMBER_ID`` is the opaque Sales Navigator member identifier. It is
    stored VERBATIM: it is case-sensitive, and the vanity-URL normalizer folds
    case, so putting it through that path corrupts the identifier. Building
    ``/in/<member-id>`` from it produces a URL that does resolve, but that alias
    is not automatically the contact's canonical published profile URL and never
    becomes one on its own.
    """

    PUBLIC_VANITY_URL = "public_vanity_url"
    SALESNAV_MEMBER_ID = "salesnav_member_id"


class IdentityLinkState(enum.StrEnum):
    """Whether an identifier currently speaks for a contact (DAT-019).

    ``ACTIVE`` is the single claim that answers lookups. ``SUPERSEDED`` is
    retained history — reversal never deletes, so an association can be undone
    without losing the record that it was once made. ``NEEDS_REVIEW`` is a claim
    that could not be accepted because another contact already holds the
    identifier: both identifiers survive and an operator decides, because an
    unresolved duplicate is safer than a false merge.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    NEEDS_REVIEW = "needs_review"


class IdentityLinkDecision(enum.StrEnum):
    """What justified an identity link (DAT-019).

    ``OBSERVED_CAPTURE`` — the identifier was read off a page for this person.

    ``SAME_CAPTURE_OBSERVED`` — the only automatic bridge between the two
    identifier forms. Both were directly observed in the SAME authenticated
    capture for the same displayed person, so relating them is an observation
    rather than an inference. Name, company, title, separate but compatible
    captures, and generated aliases are all explicitly insufficient.

    ``MIGRATION_BACKFILL`` — reconstructed from data that already existed. These
    may carry ``suspected_alias``; they are never treated as canonical.

    ``OPERATOR`` — a human decided, through the DAT-004 review path.
    """

    OBSERVED_CAPTURE = "observed_capture"
    SAME_CAPTURE_OBSERVED = "same_capture_observed"
    MIGRATION_BACKFILL = "migration_backfill"
    OPERATOR = "operator"


class VerificationFailureClass(enum.StrEnum):
    """Why one exact-address verification attempt produced no accepted answer.

    A *verification-domain* classification, deliberately not an Agent-level
    vocabulary: the Phase 2 Agent contract already owns execution states, and
    :class:`AgentJob.error_class` already carries the orchestration-visible class.
    This says what the provider did, so the Agent adapter can translate it into
    the shared contract exactly once.

    It is stored per attempt rather than recomputed because a later change to the
    retry policy must not reach back and relabel a historical failure.

    ``TRANSIENT_PROVIDER`` is the only class the domain reports as retryable —
    outages, timeouts and rate-limit style responses. A malformed address, a
    policy refusal, exhausted credits, a rejected credential and every definitive
    mailbox verdict are final; retrying them spends credit for nothing.
    ``INSUFFICIENT_CREDITS`` stays distinct from ``PERMANENT_PROVIDER`` because it
    names a different operator action: top up, rather than fix a credential.

    ``NONE`` means the attempt reached a verdict, including one answered from
    reused evidence. A verdict is not the same as an acceptance — whether the
    verdict may advance a Campaign Contact is decided by
    :mod:`app.services.verification.decisions`.
    """

    NONE = "none"
    INVALID_INPUT = "invalid_input"
    POLICY_REFUSAL = "policy_refusal"
    TRANSIENT_PROVIDER = "transient_provider"
    PERMANENT_PROVIDER = "permanent_provider"
    INSUFFICIENT_CREDITS = "insufficient_credits"


# ---------------------------------------------------------------------------
# Company Intelligence (CI-001)
# ---------------------------------------------------------------------------
#
# Company Intelligence turns *committed* Research evidence into structured,
# versioned, evidence-linked understanding of a Company. Every vocabulary below
# exists so an operator can tell four different things apart at a glance: what a
# model suggested, what a controlled vocabulary normalized it to, what a human
# confirmed, and what nobody could establish. Collapsing any two of those would
# turn an unverified classification into an apparent fact, which is the single
# failure this whole area is built to prevent.


class IntelligenceDimension(enum.StrEnum):
    """The classified dimensions of a Company.

    A closed set on purpose. A producer cannot invent a twelfth dimension
    without a schema change and a review, and a reader can tell which
    dimensions a given version actually addressed.

    ``INDUSTRY`` carries both the primary and the secondary industries: they
    differ by rank, not by kind, and modelling them as two dimensions would make
    "promote this secondary industry to primary" a cross-dimension move rather
    than a rank change. ``SUBINDUSTRY`` is separate because it is a child of an
    industry in the taxonomy hierarchy rather than another industry.
    """

    INDUSTRY = "industry"
    SUBINDUSTRY = "subindustry"
    PRODUCT = "product"
    SERVICE = "service"
    SPECIALTY = "specialty"
    CAPABILITY = "capability"
    GEOGRAPHY = "geography"
    OPERATING_MARKET = "operating_market"
    CUSTOMER_SEGMENT = "customer_segment"
    BUSINESS_MODEL = "business_model"
    COMPANY_TYPE = "company_type"


class IntelligenceValueState(enum.StrEnum):
    """How settled one classified value is.

    ``RESOLVED`` — a value supported by persisted evidence.
    ``UNRESOLVED`` — a value was proposed but could not be tied to evidence, or
    could not be normalized to a vocabulary that requires normalization. It is
    kept, visibly unresolved, rather than dropped: a discarded suggestion is
    invisible to review, and invisible work gets redone.
    ``UNKNOWN`` — the producer looked at this dimension and the evidence said
    nothing. Different from a dimension that was never addressed at all, which
    has no row.
    ``CONFLICTED`` — the evidence supports more than one mutually exclusive
    answer. Never flattened to whichever scored higher.
    """

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    UNKNOWN = "unknown"
    CONFLICTED = "conflicted"


class IntelligenceNormalization(enum.StrEnum):
    """How a suggested value reached (or failed to reach) a canonical term.

    Stored per classification so the operator can always see the difference
    between "the model said exactly the canonical label", "the model said
    something we recognised as an alias of it", and "nothing in the vocabulary
    matched, and this is the model's own wording".
    """

    CANONICAL = "canonical"
    ALIAS = "alias"
    UNMAPPED = "unmapped"
    #: The dimension has no controlled vocabulary in this taxonomy release, so
    #: free text is the intended representation rather than a failure.
    NOT_APPLICABLE = "not_applicable"


class IntelligenceConfidenceBand(enum.StrEnum):
    """A coarse band derived deterministically from the numeric confidence.

    Bands exist because a stored float invites false precision in a UI: 0.62 and
    0.58 are not meaningfully different judgements, and showing them side by side
    implies they are. The float is preserved; the band is what screens compare.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IntelligenceEvidenceSupport(enum.StrEnum):
    """Whether one evidence reference supports or contradicts a classification."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


class IntelligenceEvidenceStatus(enum.StrEnum):
    """Whether a classification is backed by evidence that was actually present.

    ``INSUFFICIENT`` is a first-class, storable outcome. A classification that
    cites nothing the input contained is recorded as unsupported rather than
    stored as a weaker fact — and never silently dropped.
    """

    SUPPORTED = "supported"
    INSUFFICIENT = "insufficient"


class IntelligenceValueSource(enum.StrEnum):
    """Who is responsible for the value a reader is looking at."""

    MODEL = "model"
    OPERATOR_CONFIRMED = "operator_confirmed"
    OPERATOR_CORRECTED = "operator_corrected"
    OPERATOR_UNRESOLVED = "operator_unresolved"


class IntelligenceDecisionAction(enum.StrEnum):
    """What an operator decided about one classified value.

    Every action is an append-only record. None of them edits the model-produced
    version: the historical classification stays exactly as it was produced, and
    the decision is applied on top when the effective value is resolved.
    """

    CONFIRM = "confirm"
    CORRECT = "correct"
    MARK_UNRESOLVED = "mark_unresolved"
    REJECT = "reject"


class TaxonomyAliasSource(enum.StrEnum):
    """Where a vocabulary alias came from.

    ``MODEL_SUGGESTION`` aliases are recorded but are **not** authoritative for
    normalization until an operator promotes them; that is what keeps a model
    from quietly widening the controlled vocabulary.
    """

    SEED = "seed"
    OPERATOR = "operator"
    MODEL_SUGGESTION = "model_suggestion"


class IntelligenceJobStatus(enum.StrEnum):
    """Durable lifecycle of one Company Intelligence production job.

    Deliberately its own vocabulary rather than a reuse of the Campaign Contact
    Agent job status. Company Intelligence is company-scoped derived work that
    runs outside the Campaign Contact pipeline, and sharing the pipeline's status
    type would make it look enrollable from the type alone.
    """

    PENDING = "pending"
    LEASED = "leased"
    IN_PROGRESS = "in_progress"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IntelligenceBackfillStatus(enum.StrEnum):
    """Lifecycle of one bounded backfill run."""

    PREVIEW = "preview"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class IntelligenceBackfillOutcome(enum.StrEnum):
    """What a backfill run decided about one Company.

    ``SKIPPED`` always carries a truthful reason code. A backfill that reports a
    company as done when it was skipped is worse than one that fails loudly.
    """

    PREVIEWED = "previewed"
    ENQUEUED = "enqueued"
    SKIPPED = "skipped"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Company Intelligence: geography and specialty hardening (CI-002)
# ---------------------------------------------------------------------------


class IntelligenceGeoRelationship(enum.StrEnum):
    """How a Company relates to a place.

    A city without a relationship is not intelligence, it is a word that
    appeared. "Headquartered in London", "serves customers across Germany" and
    "presented at a conference in Berlin" all put a place next to a company and
    only one of them is an address.

    The set is closed and bounded because it is what downstream targeting will
    filter on. ``UNCLEAR`` is a first-class member, not a gap: the evidence
    genuinely does mention the place, and saying so beats both guessing and
    discarding.
    """

    HEADQUARTERS = "headquarters"
    OFFICE = "office"
    BRANCH = "branch"
    FACILITY = "facility"
    MANUFACTURING = "manufacturing"
    RESEARCH_AND_DEVELOPMENT = "research_and_development"
    WAREHOUSE = "warehouse"
    DISTRIBUTION = "distribution"
    #: Material business operations that are not one of the named site types.
    OPERATIONS = "operations"
    #: Sells into the market without evidence of a site there.
    COMMERCIAL_MARKET = "commercial_market"
    PLANNED_PRESENCE = "planned_presence"
    HISTORICAL_PRESENCE = "historical_presence"
    UNCLEAR = "unclear"


class IntelligencePresenceKind(enum.StrEnum):
    """What kind of presence a relationship actually asserts.

    Derived deterministically from the relationship and stored beside it, so a
    consumer asking "where is this company physically" never has to
    re-implement the mapping — and so the difference between a factory in Pune
    and selling into Pune cannot be lost by a reader who did not know to look.

    ``PROSPECTIVE`` and ``FORMER`` are deliberately not physical. A plant that
    is announced and a plant that closed are both real facts and neither is a
    place the company is today.
    """

    PHYSICAL = "physical"
    COMMERCIAL = "commercial"
    PROSPECTIVE = "prospective"
    FORMER = "former"
    UNKNOWN = "unknown"
