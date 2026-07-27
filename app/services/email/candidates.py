"""Candidate generation, ranking, and selection (EML-004 / EML-005 / EML-006).

Ties the normalization and pattern engines to the database: it materializes a
contact's ranked candidate set, reorders it using internal domain-pattern
evidence (never marking any address valid), and selects exactly one candidate to
verify with a recorded, human-readable reason. Ambiguous or unrenderable names
produce no selection and are reported for review.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.email_evidence import DomainPatternObservation
from app.models.enums import EmailCandidateSource
from app.services.email.normalization import build_identity
from app.services.email.patterns import engine_version, generate_local_parts
from app.services.imports.normalization import is_valid_email, normalize_email
from app.services.resolution import gates

# Domain-pattern observations older than this contribute no ranking boost: stale
# pattern evidence must not keep steering selection (EML-004 "fresh ... evidence").
_PATTERN_FRESHNESS_DAYS = 180


@dataclass(frozen=True)
class RankedCandidate:
    """A candidate address with its transparent ranking evidence."""

    email: str
    local_part: str
    domain: str
    source: EmailCandidateSource
    pattern: str | None
    rank_score: float
    rank_reason: str


@dataclass
class CandidateGenerationResult:
    """Outcome of (re)generating a contact's candidate set."""

    candidates: list[EmailCandidate] = field(default_factory=list)
    selected: EmailCandidate | None = None
    needs_review: bool = False
    review_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(UTC)


def rank_candidates(
    *,
    imported_email: str | None,
    identity_first: str | None,
    identity_last: str | None,
    domain: str | None,
    pattern_confidence: dict[str, float] | None = None,
) -> list[RankedCandidate]:
    """Pure ranking: build and order candidates from names, domain, and evidence.

    ``pattern_confidence`` maps a pattern string (e.g. ``"{first}.{last}"``) to a
    fresh confidence in ``[0, 1]`` derived from domain-pattern observations. A
    matching observation lowers a candidate's score (earlier position) but can
    never change its verification result — ranking only reorders what to check
    first. Lower ``rank_score`` sorts earlier.
    """

    pattern_confidence = pattern_confidence or {}
    ranked: list[RankedCandidate] = []
    seen: set[str] = set()

    # An imported exact address is always the first thing to confirm.
    if imported_email:
        norm = normalize_email(imported_email)
        if norm and is_valid_email(norm) and "@" in norm:
            local, _, dom = norm.partition("@")
            ranked.append(
                RankedCandidate(
                    email=norm,
                    local_part=local,
                    domain=dom,
                    source=EmailCandidateSource.IMPORTED,
                    pattern=None,
                    rank_score=-1000.0,
                    rank_reason="imported exact address — verify the address already on file first",
                )
            )
            seen.add(norm)

    if domain:
        identity = build_identity(identity_first, identity_last)
        if identity.renderable:
            for pc in generate_local_parts(identity):
                email = f"{pc.local_part}@{domain}"
                if email in seen:
                    continue
                seen.add(email)
                score = float(pc.base_rank)
                reason = f"pattern {pc.pattern} (base priority {pc.base_rank})"
                conf = pattern_confidence.get(pc.pattern)
                if conf:
                    boost = 5.0 * conf
                    score -= boost
                    reason += (
                        f"; boosted by fresh domain-pattern evidence "
                        f"(confidence {conf:.2f}) — reorder only, not a validity signal"
                    )
                ranked.append(
                    RankedCandidate(
                        email=email,
                        local_part=pc.local_part,
                        domain=domain,
                        source=EmailCandidateSource.GENERATED,
                        pattern=pc.pattern,
                        rank_score=score,
                        rank_reason=reason,
                    )
                )

    # Stable sort by score; imported (very low score) leads, then evidence-boosted
    # patterns, then base priority. Ties keep generation order (already stable).
    ranked.sort(key=lambda c: c.rank_score)
    return ranked


def _fresh_pattern_confidence(session: Session, domain: str) -> dict[str, float]:
    cutoff = _now() - timedelta(days=_PATTERN_FRESHNESS_DAYS)
    rows = session.scalars(
        select(DomainPatternObservation).where(
            DomainPatternObservation.domain == domain,
            DomainPatternObservation.observed_at >= cutoff,
        )
    ).all()
    out: dict[str, float] = {}
    for row in rows:
        conf = row.confidence if row.confidence is not None else 0.5
        # Keep the strongest fresh observation per pattern.
        if row.pattern not in out or conf > out[row.pattern]:
            out[row.pattern] = max(0.0, min(1.0, conf))
    return out


def generate_candidates(session: Session, contact: Contact) -> CandidateGenerationResult:
    """(Re)generate, rank, persist, and select candidates for *contact*.

    Idempotent: existing candidates for the contact are replaced so regeneration
    never accumulates duplicates. Exactly one candidate is selected when possible;
    an unrenderable name with no imported address is reported for review.
    """

    result = CandidateGenerationResult()

    # Email discovery is one of the stages a provisional company domain does NOT
    # authorize (DAT-017A). Checked here rather than in the route that calls it,
    # because generating addresses at a domain nobody has confirmed is the
    # failure the rule exists to prevent — and a rule that only a route enforces
    # is one refactor away from not being enforced at all. Refused, not raised:
    # the caller already renders ``needs_review`` with its reason.
    gate = gates.authorize_contact(
        session, contact=contact, stage=gates.DownstreamStage.EMAIL_DISCOVERY
    )
    if gate.blocked:
        result.needs_review = True
        result.review_reason = gate.reason
        return result

    # Replace any prior candidate set for a deterministic regenerate.
    for existing in session.scalars(
        select(EmailCandidate).where(EmailCandidate.contact_id == contact.id)
    ).all():
        session.delete(existing)
    session.flush()

    domain = contact.company_domain or None
    pattern_confidence = _fresh_pattern_confidence(session, domain) if domain else {}

    ranked = rank_candidates(
        imported_email=contact.email,
        identity_first=contact.first_name,
        identity_last=contact.last_name,
        domain=domain,
        pattern_confidence=pattern_confidence,
    )

    identity = build_identity(contact.first_name, contact.last_name)
    result.warnings.extend(identity.warnings)

    if not ranked:
        result.needs_review = True
        if not domain:
            result.review_reason = "no confirmed company domain; cannot generate candidates"
        elif not identity.renderable:
            result.review_reason = identity.reason or "name is not renderable to an address"
        else:
            result.review_reason = "no candidate addresses could be generated"
        return result

    version = engine_version()
    rows: list[EmailCandidate] = []
    for rank, rc in enumerate(ranked):
        row = EmailCandidate(
            contact_id=contact.id,
            email=rc.email,
            source=rc.source,
            pattern=rc.pattern,
            local_part=rc.local_part,
            domain=rc.domain,
            engine_version=version,
            rank=rank,
            rank_score=rc.rank_score,
            rank_reason=rc.rank_reason,
            selected=False,
        )
        session.add(row)
        rows.append(row)
    session.flush()

    # Select the top-ranked candidate with a transparent reason.
    top = rows[0]
    top.selected = True
    if top.source == EmailCandidateSource.IMPORTED:
        top.selection_reason = (
            "imported exact address is confirmed first before any generated guess"
        )
    else:
        boosted = (
            "with fresh domain-pattern support"
            if pattern_confidence.get(top.pattern or "")
            else "by base pattern priority"
        )
        top.selection_reason = f"highest-ranked generated candidate {boosted}"
    session.flush()

    result.candidates = rows
    result.selected = top
    return result


def get_candidates(session: Session, contact_id: uuid.UUID) -> list[EmailCandidate]:
    """Return a contact's candidates ordered by rank (for display)."""

    return list(
        session.scalars(
            select(EmailCandidate)
            .where(EmailCandidate.contact_id == contact_id)
            .order_by(EmailCandidate.rank)
        ).all()
    )


def get_selected_candidate(session: Session, contact_id: uuid.UUID) -> EmailCandidate | None:
    return session.scalars(
        select(EmailCandidate).where(
            EmailCandidate.contact_id == contact_id,
            EmailCandidate.selected.is_(True),
        )
    ).first()
