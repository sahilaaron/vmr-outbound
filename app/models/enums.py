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

    ``PRIOR_MAPPING`` replays a decision the operator already made for the same
    normalized company. ``AUTOMATIC_POLICY`` (DAT-017) is a domain the versioned
    resolution policy selected because two independent evidence axes agreed, or
    because an operator-captured company page named it under an exact identity
    match. Neither is a provider's opinion: a provider's top-ranked name match
    never qualifies on its own.

    The two automatic sources stay distinct because they answer to different
    evidence and are corrected at different rates — reuse replays a human
    decision, whereas the policy makes one.
    """

    CANDIDATE = "candidate"
    MANUAL = "manual"
    UNRESOLVED = "unresolved"
    PRIOR_MAPPING = "prior_mapping"
    AUTOMATIC_POLICY = "automatic_policy"


class DomainResolutionDecision(enum.StrEnum):
    """What the DAT-017 resolution policy concluded for one company.

    Recorded alongside the policy version and the evidence that produced it, so
    an automatic decision can be explained, audited and — when it turns out to
    be wrong — corrected and counted.

    Deliberately separate from :class:`CompanyResolutionOutcome`, which is the
    promotion-blocking view of the same record. This enum says *why the policy
    decided what it did*; that one says *whether a capture may proceed*.
    Collapsing them would lose the difference between "several plausible
    candidates" and "two sources actively disagree", which are the two review
    cases an operator handles most differently.
    """

    #: Two independent evidence axes named the same domain, or an
    #: identity-matched company page named it. Applied without an operator.
    AUTO_CONFIRMED = "auto_confirmed"
    #: A domain an operator already confirmed for this company was replayed.
    #: Costs no provider call.
    PRIOR_MAPPING_REUSED = "prior_mapping_reused"
    #: Evidence exists but does not settle the question. Carries a
    #: recommendation that is shown and never applied.
    REVIEW_REQUIRED = "review_required"
    #: The provider answered and nothing usable came back, and no other
    #: evidence names a domain. Distinct from an unreachable provider.
    NO_CREDIBLE_CANDIDATE = "no_credible_candidate"
    #: Two authoritative sources named different domains. Never resolved by
    #: preferring one; a wrong domain is worse than an unanswered one.
    CONFLICT = "conflict"
    #: The provider could not be reached or answered unusably. No domain is
    #: invented to fill the gap; a retry may succeed.
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class CompanyResolutionOutcome(enum.StrEnum):
    """How the COMPANY behind a contact capture was resolved (DAT-014).

    Deliberately separate from :class:`ContactPromotionOutcome`: knowing which
    company a person works for and knowing which person they are, are two
    different questions with different failure modes, and collapsing them into
    one result would hide which of the two actually blocked a promotion.

    Two outcomes are reachable without an operator. ``EXISTING_COMPANY_RESOLVED``
    replays a previous CONFIRMED decision for the same normalized company.
    ``DOMAIN_AUTO_CONFIRMED`` (DAT-017) is the versioned resolution policy
    selecting a domain that two independent evidence axes agreed on, or that an
    operator-captured company page named under an exact identity match.

    A provider result on its own is still only a *candidate* awaiting the
    operator, because a top-ranked name match is not evidence of identity. What
    changed in DAT-017 is not the trust placed in the provider but the arrival
    of a second, independent source to corroborate it against.
    """

    PENDING_LOOKUP = "pending_lookup"
    EXISTING_COMPANY_RESOLVED = "existing_company_resolved"
    DOMAIN_CANDIDATE_CONFIRMED = "domain_candidate_confirmed"
    DOMAIN_AUTO_CONFIRMED = "domain_auto_confirmed"
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


class VerificationJobStatus(enum.StrEnum):
    """Lifecycle of one queued exact-address verification job (VER-005 / OPS-001).

    A job is the unit of background work. ``PENDING`` is claimable now;
    ``IN_PROGRESS`` is leased by a worker; ``RETRY_SCHEDULED`` is a transient
    failure waiting for its backoff window; the three terminal states record how
    it ended. Only transient failures reach ``RETRY_SCHEDULED``; a definite
    address result always ends ``SUCCEEDED``.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    """Captured, but no canonical company domain yet, so no contact row.

    Resolution runs through DAT-010's logo.dev candidates and an operator
    confirmation. The person is saved; only the promotion is outstanding.
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
