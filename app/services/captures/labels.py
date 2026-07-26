"""Backend-owned contact labels (DAT-013).

Labels classify **permanent contacts**. They are not campaigns, not audiences,
and not an eligibility signal: applying a label can never make a contact
outreach-eligible, unsuppress it, or move it through the workflow.

The extension may only *request* a label by name. This module owns the
canonical registry: it derives a deterministic slug, finds or creates the label
row, and assigns it to a contact idempotently. A suppressed contact is never
touched — the requested names stay on the capture as evidence instead.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.contact_capture import ContactLabel, ContactLabelAssignment

# Bounds mirror the wire contract (contact-capture.schema.json).
MAX_LABELS_PER_SUBMISSION = 25
MAX_LABEL_LENGTH = 64
MAX_SLUG_LENGTH = 96

LABEL_SOURCE = "linkedin-contact-capture"

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify_label(name: str) -> str | None:
    """Deterministic identity key for a label name.

    ``"Venture Capital"``, ``"venture  capital"`` and ``"Venture-Capital"`` all
    resolve to ``venture-capital``, so the operator cannot accidentally create
    three registry rows for one idea. Returns ``None`` when nothing usable
    remains (the caller rejects or ignores it; it never invents a slug).
    """

    if not isinstance(name, str):
        return None
    folded = unicodedata.normalize("NFKD", name)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii").casefold()
    slug = _NON_SLUG.sub("-", ascii_only).strip("-")
    if not slug:
        return None
    return slug[:MAX_SLUG_LENGTH].strip("-") or None


def normalize_requested_labels(raw: object) -> list[str]:
    """Clean, de-duplicate and bound a requested label list from the wire.

    Preserves the operator's first spelling of each distinct label, drops blanks
    and unusable names, and never exceeds the contract's ceiling.
    """

    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = " ".join(item.split())[:MAX_LABEL_LENGTH].strip()
        if not name:
            continue
        slug = slugify_label(name)
        if slug is None or slug in seen:
            continue
        seen.add(slug)
        out.append(name)
        if len(out) >= MAX_LABELS_PER_SUBMISSION:
            break
    return out


@dataclass
class ResolvedLabels:
    """Canonical labels resolved (or created) for one submission."""

    labels: list[ContactLabel] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [label.name for label in self.labels]


def resolve_labels(
    session: Session, names: list[str], *, created_by: str = LABEL_SOURCE
) -> ResolvedLabels:
    """Find or create the canonical label rows for ``names``.

    Creation is deliberate and backend-side. A concurrent creator racing on the
    unique slug is recovered by re-selecting the winner rather than failing the
    whole submission.
    """

    resolved = ResolvedLabels()
    for name in names:
        slug = slugify_label(name)
        if slug is None:
            continue
        existing = session.scalars(
            select(ContactLabel).where(ContactLabel.slug == slug)
        ).one_or_none()
        if existing is not None:
            resolved.labels.append(existing)
            continue
        label = ContactLabel(slug=slug, name=name, created_by=created_by)
        session.add(label)
        try:
            with session.begin_nested():
                session.flush()
        except IntegrityError:
            winner = session.scalars(
                select(ContactLabel).where(ContactLabel.slug == slug)
            ).one_or_none()
            if winner is None:
                raise
            resolved.labels.append(winner)
            continue
        resolved.labels.append(label)
    return resolved


def assign_labels(
    session: Session,
    *,
    contact_id: uuid.UUID,
    labels: list[ContactLabel],
    capture_id: uuid.UUID | None,
    source: str = LABEL_SOURCE,
) -> list[str]:
    """Apply labels to one permanent contact. Idempotent and additive.

    Returns the names newly applied by this call (an already-present label is
    not reported twice). Existing labels are never removed: a capture adds
    classification, it never replaces the operator's earlier judgement.
    """

    if not labels:
        return []
    existing_ids = set(
        session.scalars(
            select(ContactLabelAssignment.label_id).where(
                ContactLabelAssignment.contact_id == contact_id
            )
        )
    )
    applied: list[str] = []
    for label in labels:
        if label.id in existing_ids:
            continue
        session.add(
            ContactLabelAssignment(
                contact_id=contact_id,
                label_id=label.id,
                source=source,
                capture_id=capture_id,
            )
        )
        existing_ids.add(label.id)
        applied.append(label.name)
    if applied:
        session.flush()
    return applied


def list_labels(session: Session) -> list[ContactLabel]:
    """Every known label, alphabetically — the extension's reuse list."""

    return list(session.scalars(select(ContactLabel).order_by(ContactLabel.slug)))
