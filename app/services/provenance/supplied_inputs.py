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
  the company's domain. If they did, automatic company-domain resolution has
  nothing to establish and is not attempted.

The domain can arrive two ways, and the record keeps them apart. The operator
may type a website, or they may type a corporate address and nothing else —
``john@acme.com`` names the employer as plainly as a website cell would, and
:func:`derive_company_domain` reads it. A stated website always outranks a
derived one, and a public mailbox derives nothing at all: ``john@gmail.com`` says
nothing about an employer, and a system that read it as one would file the
prospect under Google and then write to them about Google.

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
from app.services.imports.apollo import is_public_email_domain
from app.services.imports.normalization import (
    is_valid_email,
    is_valid_hostname,
    normalize_domain,
    normalize_email,
    normalize_email_domain,
)

#: The key inside ``CampaignContactSource.source_context`` holding this record,
#: and the version of its shape. Versioned because a reader a year from now must
#: be able to tell an older record from a newer one without guessing.
CONTEXT_KEY = "operator_supplied_inputs"
#: ``/2`` added ``company_domain.source`` and ``company_domain.derived_from_email``.
#: A ``/1`` record stays readable: the reader below decides on ``usable`` and
#: ``normalized``, which mean exactly what they always did, and a missing
#: ``source`` is reported as the website origin — which is the only origin a
#: ``/1`` record could have had.
SCHEMA_VERSION = "operator-supplied-inputs/2"

#: The Email outcome and the Company Agent's lineage both report *why* a step was
#: not performed. Naming the two reasons here keeps a screen, an event and a job
#: result from spelling them three slightly different ways.
EMAIL_DERIVATION = "operator_supplied_intake_no_discovery"
DOMAIN_REASON = "operator_supplied_domain"
#: Why the Company Agent did not run automatic resolution when the domain came
#: from the address rather than from a website cell. A separate code from
#: :data:`DOMAIN_REASON` because the two are separate facts: one is a website the
#: operator typed, the other is an inference this system drew from an address
#: they typed. Both skip resolution; only one of them was stated outright.
DOMAIN_DERIVED_REASON = "derived_from_operator_supplied_email"

#: Where an operator-given company domain came from. Stored verbatim in the
#: provenance record and echoed by the Company Agent, so one vocabulary covers
#: the JSON, the stage reason and the report.
DOMAIN_SOURCE_WEBSITE = "operator_supplied_website"
DOMAIN_SOURCE_EMAIL = DOMAIN_DERIVED_REASON


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
    """A company domain this Campaign was handed, and how it was handed over."""

    normalized: str
    raw: str
    source_type: str
    source_id: uuid.UUID
    #: :data:`DOMAIN_SOURCE_WEBSITE` when the operator typed a website, or
    #: :data:`DOMAIN_SOURCE_EMAIL` when it was read off the address they typed.
    origin: str = DOMAIN_SOURCE_WEBSITE
    #: The address the domain was read from. ``None`` for a typed website.
    derived_from_email: str | None = None

    @property
    def derived(self) -> bool:
        return self.origin == DOMAIN_SOURCE_EMAIL


def derive_company_domain(email: str | None) -> str | None:
    """The employer domain an operator-supplied address establishes, or ``None``.

    ``john@acme.com`` says two things, and only one of them is about the mailbox.
    The other is *which company this person works at* — the single question
    automatic company-domain resolution exists to answer. When the operator has
    already answered it in the address, resolution has nothing left to establish.

    **A public mailbox answers neither question.** ``john@gmail.com`` says
    nothing whatever about an employer, and a system that read it as one would
    file the prospect under Google, research Google, and write to them about
    Google. So the domain is put through the same
    :func:`~app.services.imports.apollo.is_public_email_domain` set the file
    import has always used for exactly this decision — one list, checked in one
    place, rather than a second copy that could drift from it. ``None`` here is
    not a failure: it means the Company Agent resolves the domain as it would for
    any other contact.

    Normalization is the canonical pair and nothing new. ``normalize_email`` has
    already put the address into its IDNA, lower-cased, syntactically-valid form
    by the time a caller has one; ``normalize_email_domain`` re-derives the host
    half from it, and ``normalize_domain`` then applies the *same* reading a typed
    website gets — notably stripping a leading ``www.`` — so ``john@WWW.Acme.COM``
    and a website cell reading ``https://www.acme.com/about`` produce one string,
    ``acme.com``, and therefore one permanent Company rather than two.

    The public-mailbox check deliberately runs **after** that stripping, on the
    value that would actually be stored. Checking first would let ``www.gmail.com``
    through the set and out the other side as ``gmail.com``.
    """

    normalized = normalize_email(email)
    if not normalized or not is_valid_email(normalized):
        return None
    host = normalize_email_domain(normalized.rpartition("@")[2])
    if host is None:
        return None
    domain = normalize_domain(host)
    if not domain or not is_valid_hostname(domain):
        return None
    if is_public_email_domain(domain):
        return None
    return domain


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

    supplied_email_value: str | None = None
    if email_raw is not None and email_raw.strip():
        normalized = normalize_email(email_raw)
        usable = bool(normalized) and is_valid_email(normalized or "")
        supplied_email_value = normalized if usable else None
        payload["email"] = {
            "raw": email_raw.strip()[:512],
            "normalized": supplied_email_value,
            "usable": usable,
            # Said in the record itself, not only in this module's docstring: a
            # later reader holding nothing but the JSON still learns that nobody
            # discovered and nobody verified this address.
            "discovered": False,
            "verified": False,
        }

    website_typed = domain_raw is not None and bool(domain_raw.strip())
    website_domain = normalize_domain(domain_raw) if website_typed else None
    if website_domain is not None and not is_valid_hostname(website_domain):
        website_domain = None
    derived_domain = derive_company_domain(supplied_email_value)

    if website_typed or derived_domain is not None:
        # Precedence, stated once. An explicit website is the operator saying
        # which company this is; a derived domain is this system reading it off
        # an address. The stated fact outranks the inferred one, always — a
        # person at ``john@subsidiary.com`` whose employer the operator has
        # written down as ``parentcompany.com`` works at the parent, and the
        # address is not evidence to the contrary.
        #
        # A website cell that could not be read is not a stated fact at all, so
        # it does not outrank anything; the derived domain is used and the
        # unreadable cell is kept verbatim below, where the operator asking "why
        # did it use a different domain than I typed?" will find it.
        chosen = website_domain or derived_domain
        payload["company_domain"] = {
            "raw": domain_raw.strip()[:512] if website_typed and domain_raw else None,
            "normalized": chosen,
            "usable": chosen is not None,
            "source": (
                DOMAIN_SOURCE_WEBSITE
                if chosen is not None and chosen == website_domain
                else DOMAIN_SOURCE_EMAIL
                if chosen is not None
                else None
            ),
            # Present only when the domain was read off the address, so the exact
            # value it was read from is recoverable without re-deriving it.
            "derived_from_email": (
                supplied_email_value
                if chosen is not None and chosen == derived_domain and chosen != website_domain
                else None
            ),
            # A domain answers "which company is this", never "what matters about
            # this company". Company Intelligence and Research still run, and no
            # resolver, provider or model was invoked to obtain this value.
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
        origin = domain.get("source")
        derived = domain.get("derived_from_email")
        return SuppliedDomain(
            normalized=normalized,
            raw=raw if isinstance(raw, str) else normalized,
            source_type=source.source_type,
            source_id=source.id,
            # A record written before ``source`` existed can only have come from
            # a website cell, so that is what it is reported as.
            origin=origin if isinstance(origin, str) else DOMAIN_SOURCE_WEBSITE,
            derived_from_email=derived if isinstance(derived, str) else None,
        )
    return None


__all__ = [
    "CONTEXT_KEY",
    "DOMAIN_DERIVED_REASON",
    "DOMAIN_REASON",
    "DOMAIN_SOURCE_EMAIL",
    "DOMAIN_SOURCE_WEBSITE",
    "EMAIL_DERIVATION",
    "SCHEMA_VERSION",
    "SuppliedDomain",
    "SuppliedEmail",
    "build_context",
    "derive_company_domain",
    "supplied_domain",
    "supplied_email",
]
