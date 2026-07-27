"""Raw research submissions and the immutable dossiers read from them (APP-003).

Two operations, deliberately separate.

:func:`submit` stores what arrived, verbatim, and returns. It does not parse,
validate the shape, populate a company field or select anything. A submission is
a record that something was said about this company at a moment in time.

:func:`interpret` turns one submission into one immutable
:class:`~app.models.company_dossier.CompanyDossierVersion` across the nine
sections. The same submission may be interpreted again later — a better
extractor, a corrected bug, a second opinion — and that produces a new version
beside the old one. Nothing is overwritten and nothing is deleted.

:func:`select_current` moves the "this is the interpretation we are working
from" marker. It supersedes rather than deletes, so the reasoning behind an
earlier reading stays inspectable after a newer one takes over.

**Provider neutrality.** ``producer`` and ``interpreter`` are opaque strings and
nothing here branches on their value. This module does not know whether a
payload came from a crawler, a model, a vendor API, or an operator pasting JSON,
and swapping any of those must not require a change in this file.

**No research engine lives here.** APP-004 owns producing content. This module
owns receiving it safely.

**Submitted text is untrusted evidence.** Everything in a payload or a section
originated outside this system. It is displayed, quoted and cited — never obeyed.
Nothing in this module interprets stored text as an instruction, and no caller
may either.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_dossier import CompanyDossierVersion, CompanyResearchSubmission
from app.models.enums import DossierSection, ResearchState
from app.services.audit import record_audit_event

DOSSIER_ACTOR = "system:company-dossier"

# The section columns, in the order the workspace displays them. Derived from
# the enum so the storage boundary and the display boundary cannot drift.
SECTION_COLUMNS: tuple[str, ...] = tuple(section.value for section in DossierSection)


class DossierError(ValueError):
    """A submission or interpretation that cannot be stored as asked."""


@dataclass(frozen=True)
class DossierSummary:
    """One version, projected for display."""

    version: CompanyDossierVersion
    submission: CompanyResearchSubmission
    sections_present: tuple[str, ...]
    sections_absent: tuple[str, ...]
    warning_count: int


def content_hash(payload: dict[str, Any]) -> str:
    """A stable hash of a payload, for idempotent resubmission.

    Sorted keys and a fixed separator so the same content hashes the same
    whatever order the producer happened to serialize it in.
    """

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def submit(
    session: Session,
    *,
    company: Company,
    producer: str,
    payload: dict[str, Any],
    producer_version: str | None = None,
    submitted_by: str | None = None,
    request_context: dict[str, Any] | None = None,
) -> tuple[CompanyResearchSubmission, bool]:
    """Store a raw research payload. Returns ``(submission, created)``.

    Resubmitting identical content for the same company returns the existing
    row rather than a duplicate, so a producer that reruns and finds nothing new
    does not grow the table. ``created`` tells the caller which happened.

    Storing a submission changes no canonical company field and selects no
    dossier. It is a landing zone, nothing more.
    """

    if not producer or not producer.strip():
        raise DossierError("a submission must name its producer")

    digest = content_hash(payload)
    existing = session.scalars(
        select(CompanyResearchSubmission).where(
            CompanyResearchSubmission.company_id == company.id,
            CompanyResearchSubmission.content_hash == digest,
        )
    ).first()
    if existing is not None:
        return existing, False

    submission = CompanyResearchSubmission(
        company_id=company.id,
        producer=producer.strip(),
        producer_version=producer_version,
        submitted_by=submitted_by,
        payload=payload,
        content_hash=digest,
        request_context=request_context,
    )
    session.add(submission)
    session.flush()
    record_audit_event(
        session,
        actor=submitted_by or DOSSIER_ACTOR,
        action="company.research_submitted",
        entity_type="company",
        entity_id=str(company.id),
        reason=f"raw research payload received from {submission.producer}",
        context={"submission_id": str(submission.id), "content_hash": digest},
    )
    return submission, True


def interpret(
    session: Session,
    *,
    company: Company,
    submission: CompanyResearchSubmission,
    interpreter: str,
    sections: dict[str, Any] | None = None,
    warnings: list[Any] | None = None,
    interpreter_version: str | None = None,
    created_by: str | None = None,
    note: str | None = None,
    make_current: bool = True,
) -> CompanyDossierVersion:
    """Store one immutable reading of one submission.

    ``sections`` is keyed by :class:`~app.models.enums.DossierSection` values. A
    key that is absent leaves that section NULL, which says "this version did not
    address it" — different from a present-but-empty section, which says "it
    looked and found nothing". Preserving that difference is the whole reason the
    sections are columns rather than a blob.

    An unknown key is rejected rather than ignored. Silently dropping a section a
    producer thought it was supplying would lose data and hide the mismatch.
    """

    if submission.company_id != company.id:
        raise DossierError("a dossier version must interpret a submission about the same company")
    if not interpreter or not interpreter.strip():
        raise DossierError("a dossier version must name its interpreter")

    payload = sections or {}
    unknown = sorted(set(payload) - set(SECTION_COLUMNS))
    if unknown:
        raise DossierError(
            "unknown dossier section(s): "
            + ", ".join(unknown)
            + f". The boundary is closed; known sections are {', '.join(SECTION_COLUMNS)}"
        )

    next_number = (
        session.scalar(
            select(func.coalesce(func.max(CompanyDossierVersion.version_number), 0)).where(
                CompanyDossierVersion.company_id == company.id
            )
        )
        or 0
    ) + 1

    version = CompanyDossierVersion(
        company_id=company.id,
        submission_id=submission.id,
        version_number=next_number,
        interpreter=interpreter.strip(),
        interpreter_version=interpreter_version,
        created_by=created_by,
        warnings=warnings,
        note=note,
        **{name: payload.get(name) for name in SECTION_COLUMNS},
    )
    session.add(version)
    session.flush()

    if make_current:
        select_current(session, company=company, version=version, actor=created_by or DOSSIER_ACTOR)
    else:
        _refresh_research_state(session, company=company)
    return version


def select_current(
    session: Session,
    *,
    company: Company,
    version: CompanyDossierVersion,
    actor: str = DOSSIER_ACTOR,
    note: str | None = None,
) -> CompanyDossierVersion:
    """Make one version the current interpretation, superseding the previous.

    The previous current version is marked superseded, not deleted. An operator
    who chose a reading and later changed their mind leaves both readings and the
    order they were in — which is what makes the change reviewable rather than
    merely done.
    """

    if version.company_id != company.id:
        raise DossierError("cannot select a dossier version belonging to another company")

    previous = session.scalars(
        select(CompanyDossierVersion).where(
            CompanyDossierVersion.company_id == company.id,
            CompanyDossierVersion.is_current.is_(True),
        )
    ).first()
    if previous is not None and previous.id == version.id:
        return version

    # Clear first and flush: the partial unique index permits exactly one
    # current version per company at any instant.
    if previous is not None:
        previous.is_current = False
        previous.superseded_at = func.now()
        session.flush()

    version.is_current = True
    version.superseded_at = None
    if note is not None:
        version.note = note
    session.flush()

    _refresh_research_state(session, company=company)
    record_audit_event(
        session,
        actor=actor,
        action="company.dossier_selected",
        entity_type="company",
        entity_id=str(company.id),
        previous_state=str(previous.version_number) if previous is not None else None,
        new_state=str(version.version_number),
        reason="current dossier interpretation changed",
        context={
            "version_id": str(version.id),
            "submission_id": str(version.submission_id),
            "interpreter": version.interpreter,
        },
    )
    return version


def _refresh_research_state(session: Session, *, company: Company) -> None:
    """Derive the company's research state from its current dossier.

    Only three states are reachable from what this module can observe. QUEUED,
    RUNNING, FAILED and STALE describe a research *run*, and no engine exists to
    report one — APP-004 owns those. Claiming them here would be inventing a
    status nothing measured.
    """

    current = session.scalars(
        select(CompanyDossierVersion).where(
            CompanyDossierVersion.company_id == company.id,
            CompanyDossierVersion.is_current.is_(True),
        )
    ).first()
    if current is None:
        company.research_state = ResearchState.NOT_REQUESTED
        company.last_researched_at = None
        session.flush()
        return

    company.research_state = (
        ResearchState.COMPLETED_WITH_WARNINGS if current.warnings else ResearchState.COMPLETED
    )
    company.last_researched_at = current.created_at
    session.flush()


def current_version(session: Session, *, company_id: uuid.UUID) -> CompanyDossierVersion | None:
    """The selected interpretation, or None when nothing has been selected."""

    return session.scalars(
        select(CompanyDossierVersion).where(
            CompanyDossierVersion.company_id == company_id,
            CompanyDossierVersion.is_current.is_(True),
        )
    ).first()


def list_versions(session: Session, *, company_id: uuid.UUID) -> list[DossierSummary]:
    """Every version for a company, newest first, each with its raw submission."""

    rows = list(
        session.execute(
            select(CompanyDossierVersion, CompanyResearchSubmission)
            .join(
                CompanyResearchSubmission,
                CompanyResearchSubmission.id == CompanyDossierVersion.submission_id,
            )
            .where(CompanyDossierVersion.company_id == company_id)
            .order_by(CompanyDossierVersion.version_number.desc())
        )
    )
    summaries: list[DossierSummary] = []
    for version, submission in rows:
        present = tuple(n for n in SECTION_COLUMNS if getattr(version, n) is not None)
        summaries.append(
            DossierSummary(
                version=version,
                submission=submission,
                sections_present=present,
                sections_absent=tuple(n for n in SECTION_COLUMNS if n not in present),
                warning_count=len(version.warnings or []),
            )
        )
    return summaries
