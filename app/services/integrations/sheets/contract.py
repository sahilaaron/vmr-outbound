"""The wire contract between the spreadsheet and the product, and nothing else.

Pure: no session, no clock, no network. Everything here is a rule about the
*shape* of what a sheet may send and what it gets back, so the refusals can be
proved without a database and the same rules can be restated in the add-on's own
tests without importing a second copy of them.

Two ideas carry most of the weight.

**Row identity is the client's, not the spreadsheet's.** A row number is not an
identity: inserting a row above it renames it, sorting renames all of them, and
a result written back by position lands on the wrong person. So every row
carries a ``client_row_id`` the add-on minted once and keeps in a hidden column,
and every server-side key is derived from that and never from a position.

**The submission key is derived, not sent.** The add-on cannot be trusted to
mint a globally unique idempotency key, and it should not have to: the key is a
pure function of the install, the spreadsheet, the tab, the row, the Campaign and
the submission generation. Two clicks of the same button produce the same key by
construction, and the same key is what the enrolment service already uses to
refuse duplicate work.
"""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass
from typing import Any

#: The add-on's own contract version, echoed on every response so a mismatched
#: pair is visible in one field rather than inferred from behaviour.
SCHEMA_VERSION = "google-sheets-batch/1"

#: Identifier fields the client mints. Long enough for a UUID, short enough that
#: a pasted essay is refused rather than stored.
MAX_CLIENT_ID_CHARS = 128
MAX_NAME_CHARS = 255
MAX_COMPANY_CHARS = 512
MAX_TITLE_CHARS = 255
MAX_URL_CHARS = 512

#: Client-minted identifiers are opaque, but not arbitrary: allowing separators
#: and whitespace would let a client construct two ids that differ only in a
#: character the key derivation collapses.
_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_LINKEDIN_HOST_PATTERN = re.compile(r"^https://([a-z0-9-]+\.)?linkedin\.com/", re.IGNORECASE)


class RowStatus(enum.StrEnum):
    """What the operator sees in the sheet's status column.

    Deliberately four words, and deliberately not the Agent stage names. A
    salesperson reading a spreadsheet needs to know whether to wait, whether to
    act, or whether a row is finished — not which of nine Agents holds it. The
    stage detail still exists and is still authoritative; it is simply not this
    surface's vocabulary.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    COULD_NOT_PREPARE = "could_not_prepare"


class RowContractError(Exception):
    """A row this contract will not accept, stated in operator language.

    The message is written to be shown in a spreadsheet cell: it says what is
    wrong with *that row*, names no internal identifier, and never quotes a
    credential or a provider response.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class BatchContractError(Exception):
    """A submission this contract will not accept at all."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SheetLocation:
    """Which sheet, in which spreadsheet, on which install.

    All three take part in the row key. The install id is what keeps two people
    working the same shared spreadsheet from colliding on each other's rows, and
    the tab id is what keeps a row copied to a second tab from replaying the
    first tab's result.
    """

    installation_id: str
    spreadsheet_id: str
    sheet_id: str

    @property
    def reference(self) -> str:
        return f"{self.spreadsheet_id}/{self.sheet_id}"


@dataclass(frozen=True)
class SubmittedRow:
    """One validated prospect row, normalized and bounded."""

    client_row_id: str
    first_name: str
    last_name: str
    company_name: str
    job_title: str | None = None
    linkedin_url: str | None = None
    context: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


def _text(value: Any) -> str:
    """Collapse a cell to a single-spaced string, or the empty string."""

    if value is None:
        return ""
    if not isinstance(value, str):
        if isinstance(value, bool) or not isinstance(value, int | float):
            return ""
        value = str(value)
    # A spreadsheet cell can carry newlines, tabs and non-breaking spaces that a
    # human reading it cannot see. Collapse them so "Acme  Ltd" and "Acme Ltd"
    # are the same company name rather than two.
    return " ".join(value.replace(" ", " ").split())


def _bounded(value: str, *, limit: int, field: str, code: str) -> str:
    if len(value) > limit:
        raise RowContractError(
            f"{field} is longer than {limit} characters; shorten it in the sheet",
            code=code,
        )
    return value


def normalize_client_id(value: Any, *, field: str) -> str:
    text = _text(value)
    if not text:
        raise BatchContractError(f"{field} is required", code="missing_client_identifier")
    if not _CLIENT_ID_PATTERN.match(text):
        raise BatchContractError(
            f"{field} may contain only letters, digits and the characters . _ : -",
            code="invalid_client_identifier",
        )
    return text


def normalize_linkedin_url(value: Any) -> str | None:
    """Accept only an ``https`` LinkedIn URL, or nothing.

    Optional input, so an unusable value is refused rather than stored: a wrong
    profile URL is worse than none, because an exact URL is the one signal
    allowed to match a row to an existing permanent Contact.
    """

    text = _text(value)
    if not text:
        return None
    text = _bounded(text, limit=MAX_URL_CHARS, field="LinkedIn URL", code="linkedin_url_too_long")
    if not _LINKEDIN_HOST_PATTERN.match(text):
        raise RowContractError(
            "the LinkedIn URL must be an https://www.linkedin.com/... address",
            code="linkedin_url_unusable",
        )
    return text


@dataclass(frozen=True)
class RejectedRow:
    """A row the contract refuses, kept in place so the sheet still hears about it."""

    client_row_id: str
    reason: str
    code: str


def parse_rows(items: list[Any], *, max_context_chars: int) -> list[SubmittedRow | RejectedRow]:
    """Parse a whole batch, refusing bad rows individually and order preserved.

    The split between the two exception types is the whole design here, and it is
    a split between *whose* mistake it is.

    A ``BatchContractError`` is the add-on's mistake — a row that is not an
    object, a missing or malformed ``client_row_id``, the same id twice. None of
    those can be reported against a row, because there is no row identity to
    report them against, so the request is refused whole and the operator sees
    one message rather than a column of nonsense.

    A ``RowContractError`` is the spreadsheet's mistake — a blank surname, a URL
    that is not LinkedIn, an essay in the context column. Those belong to exactly
    one row, they are exactly what the status column exists to say, and refusing
    the request over one of them would cost the operator every other row in the
    selection.
    """

    parsed: list[SubmittedRow | RejectedRow] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise BatchContractError("each row must be a JSON object", code="row_not_an_object")
        client_row_id = normalize_client_id(item.get("client_row_id"), field="client_row_id")
        if client_row_id in seen:
            raise BatchContractError(
                f"client_row_id {client_row_id} appears more than once in this request",
                code="duplicate_client_row_id",
            )
        seen.add(client_row_id)
        try:
            parsed.append(parse_row(item, max_context_chars=max_context_chars))
        except RowContractError as exc:
            parsed.append(RejectedRow(client_row_id=client_row_id, reason=str(exc), code=exc.code))
    return parsed


def parse_row(payload: Any, *, max_context_chars: int) -> SubmittedRow:
    """Validate and normalize one submitted row, or refuse it by name.

    The three required fields are exactly the three the product cannot proceed
    without: a first name and a last name, because a Contact is never created by
    guessing a surname, and a company name, because there is nothing to resolve a
    domain from without one. Everything else is optional and its absence must
    never stop a row.
    """

    if not isinstance(payload, dict):
        raise BatchContractError("each row must be a JSON object", code="row_not_an_object")

    client_row_id = normalize_client_id(payload.get("client_row_id"), field="client_row_id")
    if len(client_row_id) > MAX_CLIENT_ID_CHARS:  # pragma: no cover - pattern bounds it
        raise BatchContractError("client_row_id is too long", code="invalid_client_identifier")

    first_name = _bounded(
        _text(payload.get("first_name")),
        limit=MAX_NAME_CHARS,
        field="First name",
        code="first_name_too_long",
    )
    last_name = _bounded(
        _text(payload.get("last_name")),
        limit=MAX_NAME_CHARS,
        field="Last name",
        code="last_name_too_long",
    )
    company_name = _bounded(
        _text(payload.get("company_name")),
        limit=MAX_COMPANY_CHARS,
        field="Company name",
        code="company_name_too_long",
    )

    missing = [
        label
        for label, value in (
            ("First Name", first_name),
            ("Last Name", last_name),
            ("Company Name", company_name),
        )
        if not value
    ]
    if missing:
        raise RowContractError(
            "this row is missing " + ", ".join(missing),
            code="missing_required_field",
        )

    job_title = (
        _bounded(
            _text(payload.get("job_title")),
            limit=MAX_TITLE_CHARS,
            field="Job title",
            code="job_title_too_long",
        )
        or None
    )
    context = _text(payload.get("context")) or None
    if context is not None and len(context) > max_context_chars:
        raise RowContractError(
            f"the context for this row is longer than {max_context_chars} characters",
            code="context_too_long",
        )

    return SubmittedRow(
        client_row_id=client_row_id,
        first_name=first_name,
        last_name=last_name,
        company_name=company_name,
        job_title=job_title,
        linkedin_url=normalize_linkedin_url(payload.get("linkedin_url")),
        context=context,
    )


def batch_id(location: SheetLocation, *, campaign_id: str, generation: int) -> str:
    """A stable correlation id for one submission of one tab into one Campaign.

    Derived rather than minted so that a lost response is recoverable: the add-on
    can re-derive the same id from what it already has on disk and ask for the
    same results, without the server keeping a batch row to look it up in.
    """

    return "gsb_" + _digest(
        location.installation_id,
        location.spreadsheet_id,
        location.sheet_id,
        campaign_id,
        f"g{generation}",
    )


def row_idempotency_key(
    location: SheetLocation,
    *,
    campaign_id: str,
    client_row_id: str,
    generation: int,
) -> str:
    """The enrolment idempotency key for one row.

    Fed straight to ``campaign_contacts.enrol_contact(idempotency_key=...)``,
    where a repeat presentation of the same key with the same intent returns the
    existing membership and creates nothing. The prefix is human-readable on
    purpose: this value appears in provenance an operator may read a year later,
    and "which sheet did this contact come from" should be answerable from it.
    """

    return "google_sheets:" + _digest(
        location.installation_id,
        location.spreadsheet_id,
        location.sheet_id,
        campaign_id,
        client_row_id,
        f"g{generation}",
    )


def _digest(*parts: str) -> str:
    """A collision-resistant join of values that may each contain anything.

    Length-prefixed before hashing so that ``("ab", "c")`` and ``("a", "bc")``
    cannot produce the same key — the failure mode a plain separator has whenever
    the separator can appear inside a part.
    """

    payload = "".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_CLIENT_ID_CHARS",
    "SCHEMA_VERSION",
    "BatchContractError",
    "RejectedRow",
    "RowContractError",
    "RowStatus",
    "SheetLocation",
    "SubmittedRow",
    "batch_id",
    "normalize_client_id",
    "normalize_linkedin_url",
    "parse_row",
    "parse_rows",
    "row_idempotency_key",
]
