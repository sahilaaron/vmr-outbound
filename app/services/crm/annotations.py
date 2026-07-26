"""Operator annotations: labels and notes, on a contact or a pending capture.

Two write paths, both campaign-free and both audited. They exist because the
capture path's equivalents (:mod:`app.services.captures.labels`) only ever act
on a permanent contact during intake — an operator working in the CRM needs to
annotate a person who is *not yet* canonical, and to correct a label after the
fact.

What these writes deliberately cannot do:

* make anyone outreach-eligible — a label is a classification, not a gate;
* unsuppress anyone, or bypass the suppression ledger;
* rewrite history — a note is appended, never edited or deleted, and removing a
  label is itself recorded.

Anchors. A subject is either a canonical contact or a pending capture, and every
function here takes exactly one of them. That mirrors the schema after the
APP-002 migration: ``contact_label_assignments.contact_id`` and
``contact_capture_notes.capture_id`` are both nullable, with an inclusive-OR
check requiring at least one anchor.

On ``capture_id`` for labels: on a **contact-anchored** row it is *provenance* —
which capture produced this label — and may be set alongside ``contact_id``. On
a **capture-anchored** row (``contact_id IS NULL``) it is the anchor itself.
The two uses are distinguished by whether ``contact_id`` is null, which is
exactly what the partial unique indexes key on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.contact import Contact
from app.models.contact_capture import (
    NOTE_SCOPE_CONTACT,
    ContactCaptureNote,
    ContactLabel,
    ContactLabelAssignment,
)
from app.models.linkedin_profile import LinkedInProfileSnapshot
from app.services.audit import record_audit_event
from app.services.captures.labels import MAX_LABEL_LENGTH, resolve_labels, slugify_label

#: Marks an annotation an operator made in the CRM, as opposed to one the
#: capture pipeline applied during intake. Kept distinct so provenance stays
#: readable: "who decided this" is a question the operator will ask later.
OPERATOR_SOURCE = "operator"

MAX_NOTE_LENGTH = 4000


class AnnotationError(ValueError):
    """A refused annotation, with a message safe to show the operator."""


class SubjectNotFound(AnnotationError):
    """Neither a contact nor a capture with that id exists."""


@dataclass(frozen=True)
class Subject:
    """Whichever record an annotation is attached to.

    Resolving the subject once, up front, means every write below shares the
    same notion of "who" and the same not-found behaviour.
    """

    contact: Contact | None = None
    capture: LinkedInProfileSnapshot | None = None

    @property
    def is_contact(self) -> bool:
        return self.contact is not None

    @property
    def entity_type(self) -> str:
        return "contact" if self.is_contact else "capture"

    @property
    def entity_id(self) -> str:
        record = self.contact or self.capture
        assert record is not None  # one anchor is guaranteed by resolve_subject
        return str(record.id)

    @property
    def contact_anchor(self) -> uuid.UUID | None:
        """The value to write into an anchor's ``contact_id`` column."""

        return self.contact.id if self.contact is not None else None

    @property
    def capture_anchor(self) -> uuid.UUID | None:
        """The value to write into an anchor's ``capture_id`` column.

        ``None`` for a canonical contact: an operator annotation made in the CRM
        has no originating capture, and inventing one would corrupt provenance.
        """

        return self.capture.id if self.capture is not None else None


def resolve_subject(
    session: Session,
    *,
    contact_id: uuid.UUID | None = None,
    capture_id: uuid.UUID | None = None,
) -> Subject:
    """Load the annotation subject, or raise.

    Exactly one identifier must be supplied. Passing both would leave the
    anchor ambiguous, and the schema's partial unique indexes depend on that
    ambiguity never arising.
    """

    if (contact_id is None) == (capture_id is None):
        raise AnnotationError("Provide exactly one of contact_id or capture_id.")

    if contact_id is not None:
        contact = session.get(Contact, contact_id)
        if contact is None:
            raise SubjectNotFound("That contact does not exist.")
        return Subject(contact=contact)

    capture = session.get(LinkedInProfileSnapshot, capture_id)
    if capture is None:
        raise SubjectNotFound("That capture does not exist.")
    return Subject(capture=capture)


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------


def _assignment_for(
    session: Session, subject: Subject, label_id: uuid.UUID
) -> ContactLabelAssignment | None:
    """The existing assignment of this label to this subject, if any.

    The capture-anchored branch filters on ``contact_id IS NULL`` as well as the
    capture, so a contact-anchored row that merely *records* this capture as
    provenance is not mistaken for a capture-anchored assignment.
    """

    query = select(ContactLabelAssignment).where(ContactLabelAssignment.label_id == label_id)
    if subject.is_contact:
        assert subject.contact is not None
        query = query.where(ContactLabelAssignment.contact_id == subject.contact.id)
    else:
        assert subject.capture is not None
        query = query.where(
            ContactLabelAssignment.contact_id.is_(None),
            ContactLabelAssignment.capture_id == subject.capture.id,
        )
    return session.scalars(query).one_or_none()


def add_label(
    session: Session,
    subject: Subject,
    *,
    name: str,
    actor: str = OPERATOR_SOURCE,
    allow_create: bool = True,
) -> tuple[ContactLabel, bool]:
    """Apply a label to the subject. Returns ``(label, newly_applied)``.

    Idempotent: re-applying an existing label is a no-op that reports
    ``newly_applied=False`` rather than raising, because the operator's intent
    ("this person is X") is already satisfied and a duplicate-key error would be
    a worse answer than success.

    ``allow_create=False`` refuses to mint a new registry entry, so a caller can
    offer "pick from existing labels" without letting a typo create a near
    duplicate.
    """

    cleaned = (name or "").strip()
    if not cleaned:
        raise AnnotationError("A label needs a name.")
    if len(cleaned) > MAX_LABEL_LENGTH:
        raise AnnotationError(f"A label name may be at most {MAX_LABEL_LENGTH} characters.")

    slug = slugify_label(cleaned)
    if slug is None:
        raise AnnotationError(f"{cleaned!r} does not contain any usable characters for a label.")

    label = session.scalars(select(ContactLabel).where(ContactLabel.slug == slug)).one_or_none()
    if label is None:
        if not allow_create:
            raise AnnotationError(f"No label named {cleaned!r} exists.")
        # resolve_labels owns creation, including the concurrent-creator race.
        resolved = resolve_labels(session, [cleaned], created_by=actor)
        if not resolved.labels:
            raise AnnotationError(f"{cleaned!r} could not be turned into a label.")
        label = resolved.labels[0]

    if _assignment_for(session, subject, label.id) is not None:
        return label, False

    session.add(
        ContactLabelAssignment(
            contact_id=subject.contact_anchor,
            capture_id=subject.capture_anchor,
            label_id=label.id,
            source=actor,
        )
    )
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="contact_label_added",
        entity_type=subject.entity_type,
        entity_id=subject.entity_id,
        new_state=label.slug,
        context={"label": label.name, "slug": label.slug},
    )
    return label, True


def remove_label(
    session: Session,
    subject: Subject,
    *,
    slug: str,
    actor: str = OPERATOR_SOURCE,
) -> bool:
    """Remove a label from the subject. Returns whether anything was removed.

    Removing a classification is recorded: the audit trail should show that
    someone decided this person is *not* X, which is a different fact from the
    label never having been applied.
    """

    label = session.scalars(
        select(ContactLabel).where(ContactLabel.slug == (slug or "").strip())
    ).one_or_none()
    if label is None:
        return False

    assignment = _assignment_for(session, subject, label.id)
    if assignment is None:
        return False

    session.delete(assignment)
    session.flush()
    record_audit_event(
        session,
        actor=actor,
        action="contact_label_removed",
        entity_type=subject.entity_type,
        entity_id=subject.entity_id,
        previous_state=label.slug,
        context={"label": label.name, "slug": label.slug},
    )
    return True


def labels_for(session: Session, subject: Subject) -> list[ContactLabel]:
    """Every label on this subject, alphabetically."""

    query = (
        select(ContactLabel)
        .join(ContactLabelAssignment, ContactLabelAssignment.label_id == ContactLabel.id)
        .order_by(ContactLabel.name.asc())
    )
    if subject.is_contact:
        assert subject.contact is not None
        query = query.where(ContactLabelAssignment.contact_id == subject.contact.id)
    else:
        assert subject.capture is not None
        query = query.where(
            ContactLabelAssignment.contact_id.is_(None),
            ContactLabelAssignment.capture_id == subject.capture.id,
        )
    return list(session.scalars(query).all())


# --------------------------------------------------------------------------
# Notes
# --------------------------------------------------------------------------


def add_note(
    session: Session,
    subject: Subject,
    *,
    text: str,
    author: str = OPERATOR_SOURCE,
) -> ContactCaptureNote:
    """Append an operator note. Never updates or replaces an earlier one.

    There is no ``edit_note`` and no ``delete_note`` in this module, by design.
    A correction is a new note; the mistaken one stays visible, because an
    operator reading the history later needs to see what was believed at the
    time and when it changed.
    """

    cleaned = (text or "").strip()
    if not cleaned:
        raise AnnotationError("A note needs some text.")
    if len(cleaned) > MAX_NOTE_LENGTH:
        raise AnnotationError(f"A note may be at most {MAX_NOTE_LENGTH} characters.")

    note = ContactCaptureNote(
        contact_id=subject.contact_anchor,
        capture_id=subject.capture_anchor,
        scope=NOTE_SCOPE_CONTACT,
        note_text=cleaned,
        author=author,
    )
    session.add(note)
    session.flush()
    record_audit_event(
        session,
        actor=author,
        action="contact_note_added",
        entity_type=subject.entity_type,
        entity_id=subject.entity_id,
        context={"note_id": str(note.id), "length": len(cleaned)},
    )
    return note


def notes_for(session: Session, subject: Subject) -> list[ContactCaptureNote]:
    """Every note on this subject, oldest first.

    Oldest first because these read as a running log; newest-first would make a
    correction appear above the thing it corrects.

    For a canonical contact this deliberately includes notes anchored to the
    captures that resolved to it: a note written during intake is about this
    person, and hiding it after promotion would lose operator context exactly
    when it becomes most useful.
    """

    if subject.is_contact:
        assert subject.contact is not None
        capture_ids = select(LinkedInProfileSnapshot.id).where(
            LinkedInProfileSnapshot.matched_contact_id == subject.contact.id
        )
        query = select(ContactCaptureNote).where(
            (ContactCaptureNote.contact_id == subject.contact.id)
            | (ContactCaptureNote.capture_id.in_(capture_ids))
        )
    else:
        assert subject.capture is not None
        query = select(ContactCaptureNote).where(
            ContactCaptureNote.capture_id == subject.capture.id
        )
    return list(session.scalars(query.order_by(ContactCaptureNote.created_at.asc())).all())


def all_labels(session: Session) -> list[ContactLabel]:
    """The whole registry, for a picker. Alphabetical."""

    return list(session.scalars(select(ContactLabel).order_by(ContactLabel.name.asc())).all())
