"""Contact inputs the operator supplied at intake, and what they satisfy.

The fact this module records is narrow and easy to state wrongly, so it is
stated once, here:

    *An operator handed this Campaign an address (or a website) for this person,
    so the pipeline does not have to go and find one.*

That is a statement about **where a value came from**. It is emphatically not a
statement about the value being correct, the mailbox existing, or a provider
having been asked. A supplied address is not discovered, not verified, not
externally corroborated, and never becomes any of those by being used — which is
why nothing here writes an
:class:`~app.models.email_evidence.ExactEmailVerification` row, sets a
verification result, or borrows the discovery path's vocabulary.

Why this lives in the enrolment provenance and not in a new table
----------------------------------------------------------------

:class:`~app.models.pipeline.CampaignContactSource` already *is* the durable,
append-only answer to "how did this Contact enter this Campaign". It is written
by ``campaign_contacts.enrol_contact`` — the one entry point every acquisition
surface goes through — it is keyed by an idempotency key so a re-submission does
not duplicate it, and it already carries operator-supplied intake values in
``source_context`` (the submitted company name, job title, LinkedIn URL and the
free-text context, each labelled as the operator's). "The operator also supplied
the address" is the same kind of fact, recorded in the same place, by the same
service, at the same moment.

The alternative — widening
:class:`~app.models.imported_email.ImportedContactEmail` — was rejected
deliberately. That table means "one address slot of one row of one uploaded
file": ``import_batch_id``, ``import_row_id``, ``source_file_checksum``,
``source_row_number`` and ``source_schema`` are all ``NOT NULL`` and all of them
would have had to become nullable to admit a spreadsheet row that has no file
and no batch. Five constraints weakened, and a table whose name no longer
describes its contents, to store a fact the provenance record already had a
place for. The file-import path therefore keeps its own richer evidence
*unchanged*; this is the source-agnostic layer beneath it.

What the pipeline does with it
------------------------------

Two reads, both from durable state, both restart-safe:

* :func:`supplied_email` — the Email Agent asks whether this Campaign was given
  this person's address. If it was, discovery is satisfied without generating a
  candidate, and Verification is satisfied without calling a provider.
* :func:`supplied_domain` — the Company Agent asks whether the operator named
  the company's website. If they did, automatic company-domain resolution has
  nothing to establish and is not attempted.

Both re-derive their answer from the database every time they are called, so a
worker restart, a retry, or a re-enrolment reaches the same decision as the
execution that was interrupted. Neither ever reads a request, a cache, or a flag
set earlier in the same process.

The equality guard
------------------

:func:`supplied_email` returns an address only when it still equals the
permanent Contact's own normalized ``email``. That guard is what keeps the
record honest after the fact: an operator correction, a merge, or a
better-sourced address changes ``contacts.email``, and from that moment the
supplied value describes something the Campaign is no longer using — so it stops
satisfying anything and ordinary discovery resumes. A spreadsheet cell never
gets to outrank a later, stronger fact merely by having been written first.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign import CampaignContact
from app.models.contact import Contact
from app.models.pipeline import CampaignContactSource
from app.services.imports.normalization import (
    is_valid_email,
    is_valid_hostname,
    normalize_domain,
    normalize_email,
)

#: The key inside ``CampaignContactSource.source_context`` holding this record,
#: and the version of its shape. Versioned because a reader a year from now must
#: be able to tell an older record from a newer one without guessing.
CONTEXT_KEY = "operator_supplied_inputs"
SCHEMA_VERSION = "operator-supplied-inputs/1"

#: The Email outcome and the Company Agent's lineage both report *why* a step was
#: not performed. Naming the two reasons here keeps a screen, an event and a job
#: result from spelling them three slightly different ways.
EMAIL_DERIVATION = "operator_supplied_intake_no_discovery"
DOMAIN_REASON = "operator_supplied_domain"


@dataclass(frozen=True)
class SuppliedEmail:
    """An address this Campaign was handed, with nothing claimed about it."""

    normalized: str
    raw: str
    #: Which acquisition surface supplied it — ``google_sheets``, ``import``.
    source_type: str
    #: The provenance row it was read from, so an operator can trace it back.
    source_id: uuid.UUID

    @property
    def verification_performed(self) -> bool:
        """Always ``False``. A supplied address has no verification behind it.

        Present as a property rather than as a comment so that any caller tempted
        to render "verified" has to read the word ``False`` first.
        """

        return False


@dataclass(frozen=True)
class SuppliedDomain:
    """A company website this Campaign was handed."""

    normalized: str
    raw: str
    source_type: str
    source_id: uuid.UUID


def build_context(
    *,
    email_raw: str | None = None,
    domain_raw: str | None = None,
) -> dict[str, Any]:
    """The ``source_context`` fragment for one enrolment, or ``{}`` for nothing.

    Pure: no session, no clock. Callers merge the result into whatever else they
    already record about the row, so a surface adds this without restating its
    own provenance shape.

    A value that cannot be normalized into something usable is recorded with
    ``usable: false`` rather than dropped. The operator typed it, the record is
    what they will read when they ask why nothing happened, and "you gave us
    something we could not read" is a better answer than silence. ``usable:
    false`` is exactly what the two readers below refuse to act on.
    """

    payload: dict[str, Any] = {}

    if email_raw is not None and email_raw.strip():
        normalized = normalize_email(email_raw)
        usable = bool(normalized) and is_valid_email(normalized or "")
        payload["email"] = {
            "raw": email_raw.strip()[:512],
            "normalized": normalized if usable else None,
            "usable": usable,
            # Said in the record itself, not only in this module's docstring: a
            # later reader holding nothing but the JSON still learns that nobody
            # discovered and nobody verified this address.
            "discovered": False,
            "verified": False,
        }

    if domain_raw is not None and domain_raw.strip():
        normalized_domain = normalize_domain(domain_raw)
        usable_domain = bool(normalized_domain) and is_valid_hostname(normalized_domain or "")
        payload["company_domain"] = {
            "raw": domain_raw.strip()[:512],
            "normalized": normalized_domain if usable_domain else None,
            "usable": usable_domain,
            # A website answers "which company is this", never "what matters
            # about this company". Company Intelligence and Research still run.
            "resolved_by_agent": False,
        }

    if not payload:
        return {}
    payload["schema"] = SCHEMA_VERSION
    return {CONTEXT_KEY: payload}


def _records(session: Session, *, campaign_contact_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every supplied-input record on this membership, oldest first.

    Plural because ``CampaignContactSource`` is append-only: the same person may
    be presented again under a new submission generation, and each presentation
    leaves its own row. Oldest first so the earliest assertion is preferred,
    which matters only in the rare case where two rows disagree — and where they
    disagree, the equality guard in :func:`supplied_email` decides anyway.
    """

    rows = session.scalars(
        select(CampaignContactSource)
        .where(CampaignContactSource.campaign_contact_id == campaign_contact_id)
        .order_by(CampaignContactSource.recorded_at.asc(), CampaignContactSource.id.asc())
    ).all()
    found: list[dict[str, Any]] = []
    for row in rows:
        payload = (row.source_context or {}).get(CONTEXT_KEY)
        if isinstance(payload, dict):
            found.append({"source": row, "payload": payload})
    return found


def supplied_email(
    session: Session,
    *,
    membership: CampaignContact | None,
    contact: Contact,
) -> SuppliedEmail | None:
    """The operator-supplied address this Campaign uses for this person.

    ``None`` — the ordinary answer for every contact acquired any other way —
    means the Email Agent proceeds exactly as it always has. Nothing about the
    discovery path is weakened by this function existing; it either finds a
    recorded operator assertion that still matches the permanent record, or it
    gets out of the way.

    Three ways to get ``None``, all deliberate:

    * no supplied-input record on the membership;
    * a record whose address was not usable — a blank or malformed cell never
      activates the fast path, it simply leaves the pipeline to do its job;
    * a record whose address no longer equals ``contacts.email`` (see the module
      docstring on the equality guard).
    """

    if membership is None:
        return None
    current = normalize_email(contact.email)
    if not current:
        return None
    for entry in _records(session, campaign_contact_id=membership.id):
        email = entry["payload"].get("email")
        if not isinstance(email, dict) or email.get("usable") is not True:
            continue
        normalized = normalize_email(email.get("normalized"))
        if not normalized or not is_valid_email(normalized):
            continue
        if normalized != current:
            continue
        source = entry["source"]
        raw = email.get("raw")
        return SuppliedEmail(
            normalized=normalized,
            raw=raw if isinstance(raw, str) else normalized,
            source_type=source.source_type,
            source_id=source.id,
        )
    return None


def supplied_domain(
    session: Session,
    *,
    membership: CampaignContact | None,
    contact: Contact,
) -> SuppliedDomain | None:
    """The operator-supplied company website behind this Contact's domain.

    Read for explanation rather than for control. The Company Agent already skips
    automatic domain resolution whenever the Contact carries a domain — it has
    nothing to establish — so this does not change what runs. What it changes is
    what the record *says*: "resolution was not attempted because the operator
    named the website" and "resolution was not attempted because there was no
    company name to resolve from" are different facts about a Contact, and an
    operator reading the stage acts on them differently.

    Guarded the same way as the address: the record must still agree with the
    Contact's own ``company_domain``, or it is describing a domain this Contact
    no longer uses.
    """

    if membership is None:
        return None
    current = normalize_domain(contact.company_domain)
    if not current:
        return None
    for entry in _records(session, campaign_contact_id=membership.id):
        domain = entry["payload"].get("company_domain")
        if not isinstance(domain, dict) or domain.get("usable") is not True:
            continue
        normalized = normalize_domain(domain.get("normalized"))
        if not normalized or not is_valid_hostname(normalized):
            continue
        if normalized != current:
            continue
        source = entry["source"]
        raw = domain.get("raw")
        return SuppliedDomain(
            normalized=normalized,
            raw=raw if isinstance(raw, str) else normalized,
            source_type=source.source_type,
            source_id=source.id,
        )
    return None


__all__ = [
    "CONTEXT_KEY",
    "DOMAIN_REASON",
    "EMAIL_DERIVATION",
    "SCHEMA_VERSION",
    "SuppliedDomain",
    "SuppliedEmail",
    "build_context",
    "supplied_domain",
    "supplied_email",
]
