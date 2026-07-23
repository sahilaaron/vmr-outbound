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
    """Why an identity is suppressed. Opt-outs and hard bounces never expire."""

    OPT_OUT = "opt_out"
    HARD_BOUNCE = "hard_bounce"
    CUSTOMER = "customer"
    COMPETITOR = "competitor"
    INTERNAL_EXCLUSION = "internal_exclusion"
    MANUAL = "manual"


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


class EmailVerificationResult(enum.StrEnum):
    """Outcome of an exact full-address verification.

    Catch-all and unknown are deliberately distinct from valid/invalid so that
    uncertainty can never be silently treated as a confirmed mailbox (AGENTS.md).
    """

    VALID = "valid"
    INVALID = "invalid"
    CATCH_ALL = "catch_all"
    UNKNOWN = "unknown"


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
