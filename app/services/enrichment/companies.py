"""Company-domain enrichment service (DAT-010, shared with DAT-014).

Neither a Sales Navigator batch row nor a LinkedIn contact capture carries
``company_domain``, so neither can become canonical until a domain is supplied.
This service lets the operator supply that domain safely and by hand:

1. group a staged batch's raw rows into unique companies (by a normalized key);
2. look each unique company up ONCE through the official logo.dev Search Brands
   API (idempotent; only an explicit refresh/retry re-calls);
3. record the candidates truthfully, never choosing one;
4. apply the operator's EXPLICIT confirmation (a selected candidate, a manual
   override, or an explicit "unresolved") to every matching staged row via a
   domain overlay at preview/confirm time.

The grouping and overlay steps are batch-specific. The lookup, candidate
storage, confirmation, and rejection steps are shared: DAT-014 drives the same
record-level functions for a contact capture, so there is exactly one place
where a domain can be chosen and exactly one candidate store.

The Sales Navigator raw rows are never mutated: the confirmed domain lives on the
enrichment record (provenance/audit metadata) and is overlaid onto the transient
mapped view exactly where column-mapping is applied. A company the operator does
not resolve keeps no domain, so its rows stay rejected — never silently accepted.

Nothing here reads, stores, logs, or serializes the logo.dev API key; the client
receives it only for the duration of a call (see :mod:`.logodev`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import (
    EnrichmentConfirmationSource,
    EnrichmentConfirmationStatus,
    EnrichmentLookupStatus,
)
from app.models.import_batch import ImportBatch, ImportRow
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.services.audit import record_audit_event
from app.services.enrichment import logodev, model_domain
from app.services.imports import mapping as mapping_service
from app.services.imports import normalization as norm
from app.services.thinking.contracts import Thinker

_ENTITY_TYPE = "salesnav_company_enrichment"


class EnrichmentError(Exception):
    """A deterministic, operator-facing enrichment failure (bad input/state)."""


class ApiKeyMissing(Exception):
    """A lookup was requested but no logo.dev API key is configured."""


# --- Grouping ----------------------------------------------------------------


def company_key(name: str | None) -> str:
    """Normalized grouping key for a company name (collapsed + case-folded).

    Returns ``""`` for an empty/whitespace name — such rows carry no company to
    enrich and are handled by validation (missing company_name/domain), not here.
    """

    collapsed = norm.collapse_whitespace(name)
    return collapsed.casefold() if collapsed else ""


def _mapped_company_name(raw: dict[str, Any], column_mapping: dict[str, str] | None) -> str | None:
    """The company name a row would present after its column mapping is applied."""

    source = mapping_service.apply_mapping(raw, column_mapping) if column_mapping else raw
    value = source.get("company_name")
    if value is None and not column_mapping:
        # An unmapped/identity row may carry the field under a case variant.
        for key, val in raw.items():
            if isinstance(key, str) and key.strip().lower() == "company_name":
                value = val
                break
    return value if isinstance(value, str) else None


@dataclass
class CompanyGroup:
    """One unique company within a staged batch and how many rows it covers."""

    key: str
    name: str
    row_count: int


def group_companies(
    rows: list[ImportRow], column_mapping: dict[str, str] | None
) -> list[CompanyGroup]:
    """Group raw rows into unique companies by normalized key, preserving order.

    Rows with no company name are skipped (they have nothing to look up and are
    rejected by validation anyway). The first-seen display name is kept.
    """

    groups: dict[str, CompanyGroup] = {}
    # Iterate in stable capture order so the first-seen display name (and the
    # resulting lookup query) is deterministic regardless of DB row order.
    for row in sorted(rows, key=lambda r: (r.sheet_index, r.row_number)):
        name = _mapped_company_name(dict(row.raw_data), column_mapping)
        key = company_key(name)
        if not key:
            continue
        existing = groups.get(key)
        if existing is None:
            # ``key`` is non-empty, so the collapsed name is non-None; the ``or``
            # only guards the type checker.
            display = norm.collapse_whitespace(name) or ""
            groups[key] = CompanyGroup(key=key, name=display, row_count=1)
        else:
            existing.row_count += 1
    return list(groups.values())


# --- Enrichment record lifecycle ---------------------------------------------


def _records_by_key(session: Session, batch_id: uuid.UUID) -> dict[str, SalesNavCompanyEnrichment]:
    records = session.scalars(
        select(SalesNavCompanyEnrichment).where(SalesNavCompanyEnrichment.batch_id == batch_id)
    ).all()
    return {r.company_key: r for r in records}


def ensure_records(
    session: Session,
    *,
    batch: ImportBatch,
    rows: list[ImportRow],
    column_mapping: dict[str, str] | None,
) -> list[SalesNavCompanyEnrichment]:
    """Create a NOT_STARTED enrichment record for each unique company, once.

    Idempotent: a company that already has a record keeps it (and any prior
    lookup/confirmation). Row counts are refreshed so the display stays accurate
    if the mapping changed. Never runs a lookup and never calls out.
    """

    groups = group_companies(rows, column_mapping)
    existing = _records_by_key(session, batch.id)
    for group in groups:
        record = existing.get(group.key)
        if record is None:
            record = SalesNavCompanyEnrichment(
                batch_id=batch.id,
                company_key=group.key,
                company_name=group.name,
                row_count=group.row_count,
                lookup_status=EnrichmentLookupStatus.NOT_STARTED,
                confirmation_status=EnrichmentConfirmationStatus.UNCONFIRMED,
                lookup_attempts=0,
            )
            session.add(record)
            existing[group.key] = record
        else:
            record.row_count = group.row_count
            # Keep the display name current without disturbing lookup/decision.
            record.company_name = group.name
    session.flush()
    # Return in the batch's company order for a stable operator view.
    ordered = [existing[g.key] for g in groups if g.key in existing]
    return ordered


def contact_record(session: Session, contact_id: uuid.UUID) -> SalesNavCompanyEnrichment | None:
    """The candidate record owned by this permanent contact, if one exists."""

    query = select(SalesNavCompanyEnrichment).where(
        SalesNavCompanyEnrichment.contact_id == contact_id
    )
    return session.scalars(query).first()


def ensure_contact_record(
    session: Session,
    *,
    contact_id: uuid.UUID,
    company_name: str | None,
) -> SalesNavCompanyEnrichment | None:
    """Create the contact-owned candidate record, once, for a surface with no capture.

    The exact counterpart of the capture-owned record
    :func:`app.services.captures.promotion.ensure_records` creates, and it exists
    for the same reason: a company that has to be looked up needs somewhere
    truthful to keep what the provider returned, what the model said, and what
    an operator confirmed. A Contact acquired from a spreadsheet has no capture
    to hang that off, and inventing a capture id for it would be a lie about
    where the evidence came from — so the record is owned by the Contact.

    Idempotent, and it never calls the provider. Returns ``None`` for a contact
    with no usable company name: there is nothing to look up, and inventing a
    query would be the first step toward inventing a domain.

    The display name is refreshed on an existing record without disturbing the
    lookup or the decision, matching the capture path exactly — but only while
    the normalized key still matches. A contact whose employer has genuinely
    changed is a different company to resolve, and quietly re-pointing the
    record would leave its stored candidates, its lookup query and its
    confirmation describing a company nobody asked about. That case keeps the
    record it has and is left to an operator.
    """

    key = company_key(company_name)
    if not key:
        return None
    record = contact_record(session, contact_id)
    if record is None:
        record = SalesNavCompanyEnrichment(
            contact_id=contact_id,
            company_key=key,
            company_name=norm.collapse_whitespace(company_name) or "",
            row_count=1,
            lookup_status=EnrichmentLookupStatus.NOT_STARTED,
            confirmation_status=EnrichmentConfirmationStatus.UNCONFIRMED,
            lookup_attempts=0,
        )
        session.add(record)
    elif record.company_key == key:
        record.company_name = norm.collapse_whitespace(company_name) or record.company_name
    session.flush()
    return record


# Identifies which provider answered and which lookup contract produced the
# candidate list, so a stored candidate stays interpretable if either changes.
PROVIDER = "logo.dev"
LOOKUP_VERSION = "logo.dev/search-brands/v1"


def _candidates_to_json(result: logodev.LookupResult) -> list[dict[str, Any]]:
    """Candidate rows for storage: domain, name, provider rank, and confidence.

    ``rank`` is the provider's 1-based ordering, kept because the operator should
    see what the provider considered most likely — it is NOT a confirmation.
    ``confidence`` is always ``None``: logo.dev's Search Brands API returns no
    score, and recording the absence explicitly stops a later reader from
    mistaking rank for a score.
    """

    return [
        {"domain": c.domain, "name": c.name, "rank": index, "confidence": None}
        for index, c in enumerate(result.candidates, start=1)
    ]


def run_lookup(
    session: Session,
    *,
    record: SalesNavCompanyEnrichment,
    api_key: str,
    search_url: str,
    timeout: float,
    max_candidates: int,
    actor: str,
    force: bool = False,
    transport: logodev.Transport | None = None,
) -> SalesNavCompanyEnrichment:
    """Look one company up through logo.dev, at most once unless *force*.

    Without ``force`` a company that has already been looked up (any terminal
    status) is returned unchanged — one lookup per unique company. An explicit
    operator refresh/retry passes ``force=True`` to re-call. The API key is used
    only for the call and is never stored, logged, or placed in the audit record.
    """

    if not api_key or not api_key.strip():
        raise ApiKeyMissing("no logo.dev API key is configured")

    already_looked_up = record.lookup_status != EnrichmentLookupStatus.NOT_STARTED
    if already_looked_up and not force:
        return record

    result = logodev.search_brands(
        record.company_name,
        api_key=api_key,
        search_url=search_url,
        timeout=timeout,
        max_candidates=max_candidates,
        transport=transport,
    )

    record.lookup_status = result.status
    record.candidates = _candidates_to_json(result)
    record.lookup_query = record.company_name
    record.normalized_query = record.company_key
    record.provider = PROVIDER
    record.lookup_version = LOOKUP_VERSION
    record.looked_up_at = datetime.now(UTC)
    record.lookup_attempts = (record.lookup_attempts or 0) + 1
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="import.salesnav_domain_lookup",
        entity_type=_ENTITY_TYPE,
        entity_id=str(record.id),
        new_state=result.status.value,
        reason="logo.dev company-domain lookup",
        context={
            "owner": record.owner_label,
            "batch_id": str(record.batch_id) if record.batch_id else None,
            "capture_id": str(record.capture_id) if record.capture_id else None,
            "company_key": record.company_key,
            "provider": PROVIDER,
            "lookup_version": LOOKUP_VERSION,
            "status": result.status.value,
            "candidate_count": len(result.candidates),
            "attempt": record.lookup_attempts,
            "forced": force,
        },
    )
    return record


def run_model_lookup(
    session: Session,
    *,
    record: SalesNavCompanyEnrichment,
    thinker: Thinker,
    actor: str,
    timeout_seconds: float = model_domain.DEFAULT_TIMEOUT_SECONDS,
    force: bool = False,
) -> SalesNavCompanyEnrichment:
    """Ask the model for this company's domain, at most once unless *force*.

    Deliberately mirrors :func:`run_lookup` — same one-call-per-company rule, same
    audit shape, same "the answer becomes a status, never an exception" contract —
    so the two lookups behave the same way from every caller's point of view and
    an operator reads one page rather than learning two vocabularies.

    The caller decides *whether* this should run. This function does not re-check
    that the provider found nothing, because the policy is the only place allowed
    to judge what the provider's answer meant.
    """

    already = record.model_lookup_status != EnrichmentLookupStatus.NOT_STARTED
    if already and not force:
        return record

    result = model_domain.look_up(
        company_name=record.company_name,
        thinker=thinker,
        location_hint=record.location_hint,
        linkedin_company_url=record.company_linkedin_url,
        timeout_seconds=timeout_seconds,
    )

    record.model_lookup_status = result.status
    record.model_domain = result.domain
    record.model_source_url = result.source_url
    record.model_note = result.reason
    record.model_looked_up_at = datetime.now(UTC)
    record.model_lookup_attempts = (record.model_lookup_attempts or 0) + 1
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="import.model_domain_lookup",
        entity_type=_ENTITY_TYPE,
        entity_id=str(record.id),
        new_state=result.status.value,
        reason="model company-domain lookup (fallback behind logo.dev)",
        context={
            "owner": record.owner_label,
            "capture_id": str(record.capture_id) if record.capture_id else None,
            "company_key": record.company_key,
            "provider": model_domain.PROVIDER,
            "lookup_version": model_domain.LOOKUP_VERSION,
            "status": result.status.value,
            "domain": result.domain,
            # Recorded because it is the whole reason this lookup can succeed where
            # the brand matcher could not, and because an operator reviewing a run
            # of bad answers needs to know whether the hint was even present.
            "identifiers_given": list(
                model_domain.identifiers_for(
                    location_hint=record.location_hint,
                    linkedin_company_url=record.company_linkedin_url,
                )
            ),
            "attempt": record.model_lookup_attempts,
            "forced": force,
        },
    )
    return record


@dataclass
class LookupRunSummary:
    """Outcome of a batch lookup pass."""

    looked_up: int = 0
    skipped: int = 0
    by_status: dict[str, int] = field(default_factory=dict)


def run_pending_lookups(
    session: Session,
    *,
    batch: ImportBatch,
    rows: list[ImportRow],
    column_mapping: dict[str, str] | None,
    api_key: str,
    search_url: str,
    timeout: float,
    max_candidates: int,
    actor: str,
    transport: logodev.Transport | None = None,
) -> LookupRunSummary:
    """Look up every unique company that has not been looked up yet (one each).

    Ensures records exist first, then calls logo.dev once per NOT_STARTED
    company. Already-looked-up companies are skipped (refresh is a separate,
    explicit per-company action), so this is safe to run repeatedly.
    """

    if not api_key or not api_key.strip():
        raise ApiKeyMissing("no logo.dev API key is configured")

    records = ensure_records(session, batch=batch, rows=rows, column_mapping=column_mapping)
    summary = LookupRunSummary()
    for record in records:
        if record.lookup_status != EnrichmentLookupStatus.NOT_STARTED:
            summary.skipped += 1
            continue
        run_lookup(
            session,
            record=record,
            api_key=api_key,
            search_url=search_url,
            timeout=timeout,
            max_candidates=max_candidates,
            actor=actor,
            transport=transport,
        )
        summary.looked_up += 1
        summary.by_status[record.lookup_status.value] = (
            summary.by_status.get(record.lookup_status.value, 0) + 1
        )
    return summary


# --- Operator confirmation ---------------------------------------------------


def _candidate_domains(record: SalesNavCompanyEnrichment) -> set[str]:
    return {
        str(c["domain"])
        for c in (record.candidates or [])
        if isinstance(c, dict) and isinstance(c.get("domain"), str)
    }


def confirm_company(
    session: Session,
    *,
    batch: ImportBatch,
    company_key_value: str,
    source: EnrichmentConfirmationSource,
    domain: str | None,
    actor: str,
    note: str | None = None,
) -> SalesNavCompanyEnrichment:
    """Record the operator's domain decision for one company in a staged batch."""

    record = session.scalars(
        select(SalesNavCompanyEnrichment).where(
            SalesNavCompanyEnrichment.batch_id == batch.id,
            SalesNavCompanyEnrichment.company_key == company_key_value,
        )
    ).first()
    if record is None:
        raise EnrichmentError("that company is not part of this staged batch")
    return confirm_record(
        session, record=record, source=source, domain=domain, actor=actor, note=note
    )


def confirm_record(
    session: Session,
    *,
    record: SalesNavCompanyEnrichment,
    source: EnrichmentConfirmationSource,
    domain: str | None,
    actor: str,
    note: str | None = None,
) -> SalesNavCompanyEnrichment:
    """Record the operator's EXPLICIT domain decision for one enrichment record.

    Shared by both acquisition paths (a staged batch's company and a contact
    capture's company) so the decision rules cannot drift apart.

    ``source`` selects the decision kind:

    * ``CANDIDATE`` — ``domain`` must be one of the provider candidates.
    * ``MANUAL`` — ``domain`` is an operator-typed hostname; validated as a
      hostname (URLs and ``www.`` are accepted and normalized).
    * ``UNRESOLVED`` — the operator explicitly leaves the company without a
      domain; nothing downstream may proceed, but the decision is now recorded.

    Never auto-accepts: a candidate is applied only because the operator (or a
    deterministic, previously-confirmed mapping) named that exact domain.
    Raises :class:`EnrichmentError` for an invalid domain, changing nothing.
    """

    previous = record.confirmation_status.value

    if source is EnrichmentConfirmationSource.UNRESOLVED:
        record.confirmation_status = EnrichmentConfirmationStatus.UNRESOLVED
        record.confirmed_domain = None
        record.confirmation_source = EnrichmentConfirmationSource.UNRESOLVED
    else:
        normalized = norm.normalize_domain(domain)
        if normalized is None or not norm.is_valid_hostname(normalized):
            raise EnrichmentError(
                f"{domain!r} is not a valid company domain; enter a hostname like example.com"
            )
        if (
            source is EnrichmentConfirmationSource.CANDIDATE
            and normalized not in _candidate_domains(record)
        ):
            raise EnrichmentError(
                "that domain is not one of the looked-up candidates; use a manual override instead"
            )
        record.confirmation_status = EnrichmentConfirmationStatus.CONFIRMED
        record.confirmed_domain = normalized
        record.confirmation_source = source

    record.confirmed_by = actor
    record.confirmed_at = datetime.now(UTC)
    record.note = norm.collapse_whitespace(note)
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="import.salesnav_domain_confirmed",
        entity_type=_ENTITY_TYPE,
        entity_id=str(record.id),
        previous_state=previous,
        new_state=record.confirmation_status.value,
        reason="operator confirmed company domain",
        context={
            "owner": record.owner_label,
            "batch_id": str(record.batch_id) if record.batch_id else None,
            "capture_id": str(record.capture_id) if record.capture_id else None,
            "company_key": record.company_key,
            "confirmation_source": record.confirmation_source.value
            if record.confirmation_source
            else None,
            "confirmed_domain": record.confirmed_domain,
        },
    )
    return record


def reject_candidate(
    session: Session,
    *,
    record: SalesNavCompanyEnrichment,
    domain: str,
    actor: str,
    reason: str | None = None,
) -> SalesNavCompanyEnrichment:
    """Record that the operator rejected one provider candidate.

    The candidate is moved out of the reviewable list and preserved in
    ``rejected_candidates`` with the reason, actor and time — a rejection is a
    decision worth keeping, not a gap. Rejecting every candidate leaves the
    record unconfirmed, which is the truthful state: no domain was chosen.
    """

    normalized = norm.normalize_domain(domain)
    if normalized is None:
        raise EnrichmentError(f"{domain!r} is not a candidate domain")

    remaining = [
        c
        for c in (record.candidates or [])
        if not (isinstance(c, dict) and c.get("domain") == normalized)
    ]
    if len(remaining) == len(record.candidates or []):
        raise EnrichmentError("that domain is not one of the current candidates")

    rejected = list(record.rejected_candidates or [])
    original = next(
        (
            c
            for c in (record.candidates or [])
            if isinstance(c, dict) and c.get("domain") == normalized
        ),
        {"domain": normalized},
    )
    rejected.append(
        {
            **original,
            "rejected_by": actor,
            "rejected_at": datetime.now(UTC).isoformat(),
            "rejection_reason": norm.collapse_whitespace(reason),
        }
    )
    record.candidates = remaining
    record.rejected_candidates = rejected
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action="import.salesnav_domain_candidate_rejected",
        entity_type=_ENTITY_TYPE,
        entity_id=str(record.id),
        new_state=record.confirmation_status.value,
        reason="operator rejected a company-domain candidate",
        context={
            "owner": record.owner_label,
            "capture_id": str(record.capture_id) if record.capture_id else None,
            "company_key": record.company_key,
            "rejected_domain": normalized,
            "remaining_candidates": len(remaining),
        },
    )
    return record


# --- Domain overlay (applied at preview/confirm, raw rows untouched) ---------


def domain_overlay(session: Session, batch_id: uuid.UUID) -> dict[str, str]:
    """Map ``company_key -> confirmed_domain`` for CONFIRMED companies only.

    Unconfirmed and explicitly-unresolved companies are absent, so their rows
    receive no domain and stay truthfully rejected.
    """

    overlay: dict[str, str] = {}
    for record in session.scalars(
        select(SalesNavCompanyEnrichment).where(
            SalesNavCompanyEnrichment.batch_id == batch_id,
            SalesNavCompanyEnrichment.confirmation_status == EnrichmentConfirmationStatus.CONFIRMED,
        )
    ).all():
        if record.confirmed_domain:
            overlay[record.company_key] = record.confirmed_domain
    return overlay


def apply_overlay_to_source(
    source: dict[str, Any], overlay: dict[str, str] | None
) -> dict[str, Any]:
    """Inject a confirmed domain into a mapped row's source view (never raw).

    Only fills ``company_domain`` when the row does not already carry one, so an
    explicit domain in the data always wins. Returns the same (mutated) dict for
    convenient chaining. A row whose company has no confirmed domain is unchanged
    and therefore still rejected for a missing domain.
    """

    if not overlay:
        return source
    if norm.collapse_whitespace(_as_optional_str(source.get("company_domain"))):
        return source  # already has a domain; do not override
    key = company_key(_as_optional_str(source.get("company_name")))
    if key and key in overlay:
        source["company_domain"] = overlay[key]
    return source


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


# --- Operator view model -----------------------------------------------------


@dataclass
class CompanyView:
    """Everything the enrichment page shows for one company (from real records)."""

    record: SalesNavCompanyEnrichment
    candidates: list[dict[str, Any]]


@dataclass
class EnrichmentView:
    """The enrichment step's state for a staged batch."""

    companies: list[CompanyView] = field(default_factory=list)
    total: int = 0
    confirmed: int = 0
    unresolved: int = 0
    pending: int = 0
    looked_up: int = 0

    @property
    def all_decided(self) -> bool:
        """True when every company is either confirmed or explicitly unresolved."""

        return self.total > 0 and (self.confirmed + self.unresolved) == self.total


def build_view(
    session: Session,
    *,
    batch: ImportBatch,
    rows: list[ImportRow],
    column_mapping: dict[str, str] | None,
) -> EnrichmentView:
    """Ensure records exist and assemble the operator view for the batch."""

    records = ensure_records(session, batch=batch, rows=rows, column_mapping=column_mapping)
    view = EnrichmentView(total=len(records))
    for record in records:
        view.companies.append(CompanyView(record=record, candidates=list(record.candidates or [])))
        if record.confirmation_status is EnrichmentConfirmationStatus.CONFIRMED:
            view.confirmed += 1
        elif record.confirmation_status is EnrichmentConfirmationStatus.UNRESOLVED:
            view.unresolved += 1
        else:
            view.pending += 1
        if record.lookup_status is not EnrichmentLookupStatus.NOT_STARTED:
            view.looked_up += 1
    return view
