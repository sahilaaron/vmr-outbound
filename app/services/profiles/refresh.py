"""Exact-URL contact matching and refresh from profile snapshots (DAT-012E).

The absolute identity rule for the first release:

    An existing contact may be matched and automatically refreshed ONLY through
    an exact normalized LinkedIn profile URL (or the equivalent stable public
    identifier embedded in it).

One shared normalization — :func:`app.services.imports.normalization.
normalize_linkedin_profile_url` — is applied to *both* sides of the comparison
(the snapshot's captured URL and every contact's stored ``linkedin_url``,
however it originally arrived: CSV import, SalesNav staging, or manual entry),
so all sources converge on the same identity key.

Weak evidence (matching name, name+company, name+title, name+location, fuzzy
URL resemblance, inferred identity) can only produce *review candidates* stored
on the snapshot for the operator; it never merges, never creates a contact, and
never refreshes a field.

Field refresh is delegated entirely to the DAT-005 provenance/freshness
service: each snapshot value becomes one append-only observation, and the
versioned freshness policy decides the winner — a manual override outranks the
snapshot, and an older capture can never displace newer evidence. Conflicting
and superseded observations are preserved, not deleted.

Suppression (DAT-006) stays authoritative: a suppressed contact's snapshot is
linked and preserved as evidence, but no canonical field is refreshed and the
suppression itself is never touched.

A refresh outcome never makes a contact outreach-ready, verifies an email,
removes a suppression, adds a campaign approval, or schedules outreach.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.models.enums import LinkedInSnapshotOutcome
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.services.audit import record_audit_event
from app.services.imports.normalization import (
    collapse_whitespace,
    normalize_linkedin_profile_url,
)
from app.services.provenance import service as provenance
from app.services.suppressions import evaluate_suppression

REFRESH_ACTOR = "linkedin-profile-refresh"
REFRESH_AUDIT_ACTION = "contact.profile_refresh_evaluated"

# The snapshot-derived fields the refresh may propose to the freshness policy.
# Everything else observed on the profile stays snapshot evidence only.
_REFRESHABLE_FIELDS = ("title", "company_name", "linkedin_url")

# Review-candidate scan cap: candidates exist for operator context, not
# completeness; an unbounded list would bury the reviewer.
_MAX_REVIEW_CANDIDATES = 10


# --- Weak-match helpers (REVIEW-ONLY, never merge) ---------------------------


def _norm_person_name(value: str | None) -> str | None:
    """Legacy-informed conservative name normalization for *candidate display*.

    Strips credential suffixes (", SHRM-CP"), parentheticals, apostrophes, and
    hyphens, collapses whitespace, casefolds. Used ONLY to surface review
    candidates — never as a merge key.
    """

    if value is None:
        return None
    text = unicodedata.normalize("NFKC", value)
    if "," in text:
        text = text.split(",", 1)[0]
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            continue
        if depth:
            continue
        if ch in "'’ʼ":
            continue
        out.append(" " if ch == "-" else ch)
    collapsed = collapse_whitespace("".join(out))
    return collapsed.casefold() if collapsed else None


def _token_overlap(a: str, b: str) -> float:
    """Fraction of ``a``'s tokens found in ``b`` (casefolded). 0.0 when empty."""

    tokens = [t for t in a.casefold().split() if t]
    if not tokens:
        return 0.0
    other = b.casefold()
    return sum(1 for t in tokens if t in other) / len(tokens)


def _company_names_match(expected: str | None, observed: str | None) -> bool:
    """Legacy ``company_name_identifier`` behaviour, both directions, >50%."""

    if not expected or not observed:
        return False
    return _token_overlap(expected, observed) > 0.5 or _token_overlap(observed, expected) > 0.5


# --- Result ------------------------------------------------------------------


@dataclass
class RefreshResult:
    """Truthful result of reconciling one snapshot against existing contacts."""

    outcome: LinkedInSnapshotOutcome
    snapshot_id: str
    matched_contact_id: str | None = None
    refreshed_fields: list[str] = field(default_factory=list)
    unchanged_fields: list[str] = field(default_factory=list)
    skipped_fields: dict[str, str] = field(default_factory=dict)
    review_candidates: list[dict[str, Any]] = field(default_factory=list)
    suppression_reason: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "matched_contact_id": self.matched_contact_id,
            "refreshed_fields": self.refreshed_fields,
            "unchanged_fields": self.unchanged_fields,
            "skipped_fields": self.skipped_fields,
            "review_candidate_count": len(self.review_candidates),
            "suppression_reason": self.suppression_reason,
        }


# --- Matching ----------------------------------------------------------------


def _contacts_with_linkedin_urls(session: Session) -> list[Contact]:
    # Merged (tombstoned) duplicates are excluded: identity points at survivors.
    return list(
        session.scalars(
            select(Contact).where(
                Contact.linkedin_url.is_not(None), Contact.merged_into_id.is_(None)
            )
        )
    )


def find_exact_matches(session: Session, normalized_url: str) -> list[Contact]:
    """Every contact whose stored LinkedIn URL normalizes to ``normalized_url``.

    The comparison normalizes the CONTACT side with the same function used for
    the snapshot side at ingest, so historical rows stored with query strings,
    mixed-case slugs, or missing schemes still match exactly — and nothing that
    does not normalize to a main profile URL can ever match anything.
    """

    matches: list[Contact] = []
    for contact in _contacts_with_linkedin_urls(session):
        if normalize_linkedin_profile_url(contact.linkedin_url) == normalized_url:
            matches.append(contact)
    return matches


def find_review_candidates(
    session: Session,
    snapshot: LinkedInProfileSnapshot,
    *,
    exclude_ids: set[Any] | None = None,
) -> list[dict[str, Any]]:
    """Weak-evidence candidates for OPERATOR REVIEW ONLY.

    Bases considered: same normalized full name; plus corroborating company /
    title token overlap when present. The result is advisory context — nothing
    downstream may merge, refresh, or create contacts from it.
    """

    fields = snapshot.profile_fields or {}
    snap_name = _norm_person_name(fields.get("full_name"))
    if snap_name is None:
        return []
    current = _current_experiences(snapshot)
    snap_company = current[0]["company_name"] if current else None
    snap_title = current[0]["job_title"] if current else None
    excluded = exclude_ids or set()

    candidates: list[dict[str, Any]] = []
    for contact in session.scalars(select(Contact).where(Contact.merged_into_id.is_(None))):
        if contact.id in excluded:
            continue
        contact_name = _norm_person_name(f"{contact.first_name} {contact.last_name}")
        if contact_name != snap_name:
            continue
        basis = ["name"]
        if snap_company and _company_names_match(contact.company_name, snap_company):
            basis.append("name_company")
        if snap_title and contact.title and _company_names_match(contact.title, snap_title):
            basis.append("name_title")
        candidates.append(
            {
                "contact_id": str(contact.id),
                "match_basis": basis,
                "contact_name": f"{contact.first_name} {contact.last_name}",
                "contact_company": contact.company_name,
                "contact_title": contact.title,
                "auto_merge": False,  # explicit, permanent: review-only evidence
            }
        )
        if len(candidates) >= _MAX_REVIEW_CANDIDATES:
            break
    return candidates


def snapshot_experiences(snapshot: LinkedInProfileSnapshot) -> list[dict[str, Any]]:
    """The snapshot's experience entries, whichever contract produced them.

    ``linkedin-profile-capture/1.0.0`` nests them under ``experiences``; the
    contact-first ``linkedin-contact-capture/2.0.0`` calls the same list
    ``experience_observations``. The stored payload stays verbatim in both
    cases, so the accessor — not the evidence — absorbs the difference.
    """

    payload = snapshot.payload or {}
    entries = payload.get("experiences")
    if entries is None:
        entries = payload.get("experience_observations")
    return list(entries or [])


def _current_experiences(snapshot: LinkedInProfileSnapshot) -> list[dict[str, Any]]:
    return [e for e in snapshot_experiences(snapshot) if e.get("is_current") is True]


def _employment_hint(snapshot: LinkedInProfileSnapshot) -> dict[str, Any]:
    """The contact-first ``current_employment_hint`` block, when present.

    A Sales Navigator result row shows a person's current title and company but
    no experience history. The hint carries exactly those visible values; it is
    used only when the capture has no current-role experience entry, so a richer
    profile capture always wins.
    """

    payload = snapshot.payload or {}
    hint = payload.get("current_employment_hint")
    return hint if isinstance(hint, dict) else {}


# --- Field refresh (DAT-005 delegation) --------------------------------------


def _proposed_values(snapshot: LinkedInProfileSnapshot) -> dict[str, str | None]:
    """Snapshot-derived values proposed to the freshness policy.

    ``title``/``company_name`` come from the top current experience entry (the
    page's most recent role), falling back to the capture's visible
    ``current_employment_hint`` when the surface showed no experience history at
    all. Missing values are simply not proposed — a null never overwrites
    anything.
    """

    current = _current_experiences(snapshot)
    top = current[0] if current else _employment_hint(snapshot)
    proposed: dict[str, str | None] = {}
    if top:
        title = top.get("job_title") or top.get("title")
        if title:
            proposed["title"] = title
        if top.get("company_name"):
            proposed["company_name"] = top["company_name"]
    if snapshot.normalized_profile_url:
        proposed["linkedin_url"] = snapshot.normalized_profile_url
    return proposed


def _observed_at(snapshot: LinkedInProfileSnapshot) -> datetime:
    return (snapshot.captured_at or snapshot.ingested_at or datetime.now(UTC)).astimezone(UTC)


def _refresh_fields(
    session: Session,
    *,
    contact: Contact,
    snapshot: LinkedInProfileSnapshot,
    actor: str,
    result: RefreshResult,
) -> None:
    """Append snapshot observations and let the freshness policy pick winners.

    Every proposed value is recorded append-only (evidence is preserved even
    when it loses); the DAT-005 total order decides whether the contact's
    operational column changes. Manual overrides and newer evidence win by
    policy, not by special cases here.
    """

    proposed = _proposed_values(snapshot)
    observed_at = _observed_at(snapshot)
    for field_name in _REFRESHABLE_FIELDS:
        if field_name not in proposed:
            result.skipped_fields[field_name] = "not observed on the profile"
            continue
        value = proposed[field_name]
        before = getattr(contact, field_name)
        provenance.record_observation(
            session,
            contact_id=contact.id,
            field_name=field_name,
            value=value,
            source_name="linkedin-profile-capture",
            source_reference=str(snapshot.id),
            observed_at=observed_at,
            created_by=actor,
        )
        provenance.reconcile_field(session, contact=contact, field_name=field_name, actor=actor)
        after = getattr(contact, field_name)
        if before != after:
            result.refreshed_fields.append(field_name)
        else:
            result.unchanged_fields.append(field_name)


# --- Entry point -------------------------------------------------------------


def reconcile_snapshot(
    session: Session,
    snapshot: LinkedInProfileSnapshot,
    *,
    actor: str = REFRESH_ACTOR,
) -> RefreshResult:
    """Match one stored snapshot against existing contacts and refresh truthfully.

    Outcomes:

    * ``exact_match_refreshed``  — exactly one URL match; ≥1 field changed.
    * ``exact_match_unchanged``  — exactly one URL match; evidence recorded,
      nothing newer than the current winners.
    * ``unmatched_staged``       — no URL match; weak candidates (if any) stored
      for review; the snapshot stays staged.
    * ``ambiguous_review``       — more than one contact carries this URL; the
      conflict is surfaced, nothing merges.
    * ``suppressed``             — the matched contact is suppressed; evidence
      linked, no canonical refresh, suppression untouched.

    The function is idempotent: re-running recomputes the same truthful state
    (the freshness policy resolves repeated identical observations to the same
    winners). It never creates or deletes contacts, never unsuppresses, never
    changes workflow/verification/approval state, and never schedules anything.
    """

    if snapshot.normalized_profile_url is None:
        raise ValueError("snapshot has no normalized profile URL; cannot reconcile")

    result = RefreshResult(
        outcome=LinkedInSnapshotOutcome.UNMATCHED_STAGED, snapshot_id=str(snapshot.id)
    )

    matches = find_exact_matches(session, snapshot.normalized_profile_url)

    if len(matches) > 1:
        result.outcome = LinkedInSnapshotOutcome.AMBIGUOUS_REVIEW
        result.review_candidates = [
            {
                "contact_id": str(c.id),
                "match_basis": ["exact_linkedin_url"],
                "contact_name": f"{c.first_name} {c.last_name}",
                "contact_company": c.company_name,
                "contact_title": c.title,
                "auto_merge": False,
            }
            for c in matches
        ]
    elif len(matches) == 0:
        result.outcome = LinkedInSnapshotOutcome.UNMATCHED_STAGED
        result.review_candidates = find_review_candidates(session, snapshot)
    else:
        contact = matches[0]
        result.matched_contact_id = str(contact.id)
        snapshot.matched_contact_id = contact.id
        decision = evaluate_suppression(session, email=contact.email, domain=contact.company_domain)
        if decision.blocked:
            result.outcome = LinkedInSnapshotOutcome.SUPPRESSED
            result.suppression_reason = decision.blocked_reason
            for field_name in _REFRESHABLE_FIELDS:
                result.skipped_fields[field_name] = (
                    f"contact is suppressed ({decision.blocked_reason}); evidence "
                    "linked, canonical fields untouched"
                )
        else:
            _refresh_fields(session, contact=contact, snapshot=snapshot, actor=actor, result=result)
            result.outcome = (
                LinkedInSnapshotOutcome.EXACT_MATCH_REFRESHED
                if result.refreshed_fields
                else LinkedInSnapshotOutcome.EXACT_MATCH_UNCHANGED
            )

    snapshot.outcome = result.outcome
    snapshot.reconciled_at = datetime.now(UTC)
    snapshot.review_candidates = result.review_candidates or None
    snapshot.refresh_summary = result.summary()
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action=REFRESH_AUDIT_ACTION,
        entity_type="linkedin_profile_snapshot",
        entity_id=str(snapshot.id),
        new_state=result.outcome.value,
        reason=f"profile snapshot reconciled: {result.outcome.value}",
        context=result.summary(),
    )
    return result
