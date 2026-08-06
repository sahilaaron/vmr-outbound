"""The Apollo contact-export schema, recognized by header name (IMP-001).

Everything here is pure: it turns one verbatim spreadsheet row into a structured,
normalized reading of it, and says what it could not read. It touches no
database, calls no provider, and decides nothing about identity — resolution
lives in :mod:`app.services.imports.campaign_import`.

Three rules shape the module.

**Recognition is by name, never by position.** An Apollo export runs to seventy-odd
columns and the order is not stable between exports, so a positional reader would
be wrong the first time somebody re-ordered a spreadsheet. Headers are matched
through a canonical form (case, spacing, underscores and punctuation folded) plus
an explicit alias table, so ``Person Linkedin Url``, ``person_linkedin_url`` and
``Person LinkedIn URL`` are one column and nothing has to be mapped by hand.

**An unrecognized column is never guessed into a canonical field.** It is carried
verbatim in a bounded extras payload, where it is visible and inert. The failure
this prevents is a column called ``Contact Email`` silently overwriting the
``Email`` the operator meant, which is exactly the kind of quiet wrong answer no
downstream stage could detect.

**Normalization folds case, never meaning.** ``Valid`` and ``valid`` compare
equal; ``Catch-all`` and ``catch-all`` compare equal. The original wording is
kept beside the folded one every time, because the folded value is our reading
and the raw value is what the vendor actually said.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.services.imports import normalization as norm

#: The one schema this module reads. Stored on the batch and on every piece of
#: imported-email evidence, so a later reader never has to infer which contract a
#: historical row was interpreted under.
APOLLO_SCHEMA_ID = "apollo_contact_export_v1"

#: Bumped when the reading of a cell changes in a way that would produce a
#: different canonical value from the same file.
APOLLO_READER_VERSION = "apollo-reader-1"

#: How much unrecognized source data is carried per row. A vendor export can
#: carry very wide free-text columns (Keywords and Technologies routinely run to
#: kilobytes), and an unbounded payload would put the size of somebody else's
#: spreadsheet directly into our row storage.
MAX_EXTRA_COLUMNS = 40
MAX_EXTRA_VALUE_CHARS = 500

#: Cells beginning with these are interpreted as formulas by Excel, Google Sheets
#: and LibreOffice when a CSV is opened. Nothing here executes them — openpyxl is
#: opened with ``data_only`` and never evaluates — but a value that came *in* as
#: ``=cmd|'/c calc'!A0`` must not go back *out* as one.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_CANON_STRIP = re.compile(r"[^a-z0-9# ]+")
_WS = re.compile(r"\s+")


def canonical_header(name: str | None) -> str:
    """Fold one header into the form aliases are matched in.

    Lower-cases, turns underscores and hyphens into spaces, drops remaining
    punctuation, and collapses whitespace. ``#`` survives because ``# Employees``
    is a real Apollo header whose only distinguishing mark is that character.
    """

    if not name:
        return ""
    lowered = name.strip().lower().replace("_", " ").replace("-", " ")
    cleaned = _CANON_STRIP.sub("", lowered)
    return _WS.sub(" ", cleaned).strip()


#: Canonical header -> canonical field. Several spellings may target one field;
#: the FIRST header in the file that claims a field wins, so a later duplicate
#: column can never silently overwrite an earlier one.
HEADER_ALIASES: dict[str, str] = {
    # --- Core contact ---
    "first name": "first_name",
    "firstname": "first_name",
    "given name": "first_name",
    "last name": "last_name",
    "lastname": "last_name",
    "surname": "last_name",
    "family name": "last_name",
    "title": "title",
    "job title": "title",
    "seniority": "seniority",
    "departments": "departments",
    "sub departments": "sub_departments",
    "person linkedin url": "person_linkedin_url",
    "person linkedin": "person_linkedin_url",
    "linkedin url": "person_linkedin_url",
    "city": "city",
    "state": "state",
    "country": "country",
    "phone": "phone",
    "mobile phone": "mobile_phone",
    "corporate phone": "corporate_phone",
    "work direct phone": "corporate_phone",
    # --- Primary email ---
    "email": "email",
    # ``canonical_header`` turns a hyphen into a space, so "E-Mail" arrives here
    # as "e mail". Both spellings are listed because the folding is not obvious
    # from the alias table alone.
    "e mail": "email",
    "email address": "email",
    "work email": "email",
    "result": "email_result",
    "email status": "email_status",
    "primary email source": "primary_email_source",
    "primary email verification source": "primary_email_verification_source",
    "primary email catch all status": "primary_email_catch_all_status",
    "primary email last verified at": "primary_email_last_verified_at",
    # --- Secondary email ---
    "secondary email": "secondary_email",
    "secondary email source": "secondary_email_source",
    "secondary email status": "secondary_email_status",
    "secondary email verification source": "secondary_email_verification_source",
    "secondary email last verified at": "secondary_email_last_verified_at",
    # --- Tertiary email ---
    "tertiary email": "tertiary_email",
    "tertiary email source": "tertiary_email_source",
    "tertiary email status": "tertiary_email_status",
    "tertiary email verification source": "tertiary_email_verification_source",
    "tertiary email last verified at": "tertiary_email_last_verified_at",
    # --- Company ---
    "company name": "company_name",
    "company": "company_name",
    "account name": "company_name",
    "company name for emails": "company_name_for_emails",
    "website": "website",
    "company website": "website",
    "company linkedin url": "company_linkedin_url",
    "company linkedin": "company_linkedin_url",
    "company address": "company_address",
    "company city": "company_city",
    "company state": "company_state",
    "company country": "company_country",
    "# employees": "employee_count",
    "employees": "employee_count",
    "num employees": "employee_count",
    "employee count": "employee_count",
    "industry": "industry",
    "keywords": "keywords",
    "technologies": "technologies",
    "annual revenue": "annual_revenue",
    # --- Apollo source identifiers ---
    "apollo contact id": "apollo_contact_id",
    "contact id": "apollo_contact_id",
    "apollo account id": "apollo_account_id",
    "account id": "apollo_account_id",
    "apollo record id": "apollo_record_id",
    "record id": "apollo_record_id",
}

#: Present in every recognized file. Without all four a row cannot resolve the
#: person, the employer and the address the whole import path depends on, so the
#: file is refused rather than half-read.
REQUIRED_FIELDS: tuple[str, ...] = ("first_name", "last_name", "company_name", "email")

#: Headers that only an Apollo export tends to carry. They are not required —
#: recognition rests on :data:`REQUIRED_FIELDS` — but counting them lets the
#: preview tell the operator whether this is an Apollo export or merely a file
#: that happens to fit the same contract.
DISTINCTIVE_FIELDS: frozenset[str] = frozenset(
    {
        "apollo_contact_id",
        "apollo_account_id",
        "apollo_record_id",
        "primary_email_source",
        "primary_email_verification_source",
        "primary_email_catch_all_status",
        "primary_email_last_verified_at",
        "email_status",
        "seniority",
        "departments",
        "sub_departments",
        "company_name_for_emails",
        "secondary_email",
        "tertiary_email",
    }
)

#: Public mailbox providers. A person at one of these is not thereby an employee
#: of anything, so the domain can never establish company identity (IMP-001 §14).
#: A short, explicit list of the providers actually seen in these exports —
#: deliberately not a heuristic, because "looks personal" is not a fact.
PUBLIC_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "hotmail.co.uk",
        "live.com",
        "msn.com",
        "yahoo.com",
        "yahoo.co.uk",
        "yahoo.co.in",
        "ymail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "gmx.com",
        "gmx.de",
        "protonmail.com",
        "proton.me",
        "mail.com",
        "zoho.com",
        "yandex.com",
        "yandex.ru",
        "qq.com",
        "163.com",
        "126.com",
        "rediffmail.com",
        "comcast.net",
        "verizon.net",
        "btinternet.com",
        "sbcglobal.net",
    }
)


def is_public_email_domain(domain: str | None) -> bool:
    """Whether *domain* is a public mailbox provider rather than an employer."""

    return domain is not None and domain in PUBLIC_EMAIL_DOMAINS


def neutralize_formula(value: str | None) -> str | None:
    """Return *value* rendered safe to place in a CSV cell.

    Nothing in this system evaluates a formula — the workbook reader never does,
    and a rendered page escapes HTML — but a value exported back out to CSV is
    opened by a spreadsheet application that *does*. Prefixing with an apostrophe
    is the conventional neutralization: the text is preserved exactly, and the
    receiving application treats it as text rather than as an expression.
    """

    if value is None:
        return None
    if value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def looks_like_formula(value: str | None) -> bool:
    """Whether a cell would be interpreted as a formula by a spreadsheet app."""

    return value is not None and value.startswith(_FORMULA_PREFIXES)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaDetection:
    """What a file's header row was recognized as."""

    #: ``apollo_contact_export_v1`` when recognized, else ``None``.
    schema_id: str | None
    #: Canonical field -> the exact source column that supplies it.
    field_columns: dict[str, str]
    #: Source columns that matched no canonical field, in file order.
    unmapped_columns: tuple[str, ...]
    #: Required canonical fields with no source column. Empty when recognized.
    missing_required: tuple[str, ...]
    #: Source columns that claimed a field already claimed by an earlier column.
    duplicate_columns: tuple[tuple[str, str], ...] = ()
    #: How many Apollo-distinctive headers were present.
    distinctive_count: int = 0

    @property
    def recognized(self) -> bool:
        return self.schema_id is not None

    @property
    def is_apollo_export(self) -> bool:
        """Whether this looks like a genuine Apollo export rather than a file
        that merely satisfies the same minimum contract."""

        return self.recognized and self.distinctive_count >= 3

    def column_for(self, canonical_field: str) -> str | None:
        return self.field_columns.get(canonical_field)


def detect_schema(header: list[str]) -> SchemaDetection:
    """Recognize *header* as the Apollo contact export, or say what is missing.

    Order-independent by construction, and extra columns are tolerated: the
    header is walked once, each column claims at most one canonical field, and
    the first claimant of a field keeps it.
    """

    field_columns: dict[str, str] = {}
    unmapped: list[str] = []
    duplicates: list[tuple[str, str]] = []

    for column in header:
        if not column or not column.strip():
            continue
        canonical = HEADER_ALIASES.get(canonical_header(column))
        if canonical is None:
            unmapped.append(column)
            continue
        if canonical in field_columns:
            # A second column claiming a taken field is reported, never applied.
            # Silently preferring either one would make the import's reading of
            # the file depend on column order, which is the thing this module
            # exists to avoid.
            duplicates.append((column, canonical))
            unmapped.append(column)
            continue
        field_columns[canonical] = column

    missing = tuple(f for f in REQUIRED_FIELDS if f not in field_columns)
    distinctive = sum(1 for f in field_columns if f in DISTINCTIVE_FIELDS)
    return SchemaDetection(
        schema_id=None if missing else APOLLO_SCHEMA_ID,
        field_columns=field_columns,
        unmapped_columns=tuple(unmapped),
        missing_required=missing,
        duplicate_columns=tuple(duplicates),
        distinctive_count=distinctive,
    )


def missing_header_message(detection: SchemaDetection) -> str:
    """One actionable sentence naming the headers the file has to add."""

    labels = {
        "first_name": "First Name",
        "last_name": "Last Name",
        "company_name": "Company Name",
        "email": "Email",
    }
    names = ", ".join(labels.get(f, f) for f in detection.missing_required)
    return (
        f"This file is missing the required header(s): {names}. "
        "The importer recognizes an Apollo contact export by its header names, so "
        "the header row must name First Name, Last Name, Company Name and Email "
        "(in any order; extra columns are fine)."
    )


# ---------------------------------------------------------------------------
# Row reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportedAddress:
    """One address cell plus the vendor metadata that travelled with it."""

    slot: str  # "primary" | "secondary" | "tertiary"
    raw: str
    normalized: str | None
    is_valid_syntax: bool
    domain: str | None
    provider_source: str | None = None
    provider_status_raw: str | None = None
    provider_status_normalized: str | None = None
    provider_verification_source: str | None = None
    provider_catch_all_raw: str | None = None
    provider_catch_all_normalized: str | None = None
    provider_last_verified_raw: str | None = None
    provider_last_verified_at: datetime | None = None

    @property
    def is_public_domain(self) -> bool:
        return is_public_email_domain(self.domain)


@dataclass
class ApolloRow:
    """One source row read through the Apollo contract."""

    row_number: int
    sheet_index: int = 0
    sheet_name: str | None = None

    # --- Person ---
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    seniority: str | None = None
    departments: str | None = None
    sub_departments: str | None = None
    person_linkedin_url: str | None = None
    person_linkedin_identity: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    phone: str | None = None
    mobile_phone: str | None = None
    corporate_phone: str | None = None

    # --- Company ---
    company_name: str | None = None
    company_name_for_emails: str | None = None
    website_raw: str | None = None
    website_domain: str | None = None
    company_linkedin_url: str | None = None
    company_linkedin_identity: str | None = None
    company_address: str | None = None
    company_city: str | None = None
    company_state: str | None = None
    company_country: str | None = None
    employee_count: str | None = None
    industry: str | None = None
    keywords: str | None = None
    technologies: str | None = None
    annual_revenue: str | None = None

    # --- Source identifiers ---
    apollo_contact_id: str | None = None
    apollo_account_id: str | None = None
    apollo_record_id: str | None = None

    # --- Addresses ---
    primary: ImportedAddress | None = None
    secondary: ImportedAddress | None = None
    tertiary: ImportedAddress | None = None

    #: Bounded, verbatim carry-over of columns with no canonical field.
    extras: dict[str, str] = field(default_factory=dict)
    #: ``(code, message)`` observations that never block the row on their own.
    warnings: list[tuple[str, str]] = field(default_factory=list)

    @property
    def addresses(self) -> tuple[ImportedAddress, ...]:
        return tuple(a for a in (self.primary, self.secondary, self.tertiary) if a is not None)

    @property
    def has_person_identity(self) -> bool:
        """Whether the row names a person well enough to create one.

        Both name parts, because every downstream stage that writes to a human
        needs them and because ``natural_key`` — the repository's existing
        email-less identity fingerprint — is built from them.
        """

        return bool(self.first_name and self.last_name)

    @property
    def company_signals(self) -> dict[str, str | None]:
        """The company-identity evidence this row offers, by signal name."""

        return {
            "apollo_account_id": self.apollo_account_id,
            "website_domain": self.website_domain,
            "company_linkedin": self.company_linkedin_identity,
            "email_domain": self.primary.domain if self.primary else None,
            "company_name": self.company_name,
        }


def _cell(raw: dict[str, str], detection: SchemaDetection, canonical_field: str) -> str | None:
    column = detection.column_for(canonical_field)
    if column is None:
        return None
    return norm.collapse_whitespace(raw.get(column))


_TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
)


def parse_provider_timestamp(value: str | None) -> datetime | None:
    """Parse a vendor's last-verified timestamp, or return None.

    Lenient on format and strict about the result: anything unparseable yields
    ``None`` and the raw text is retained separately, because an unreadable
    timestamp is still evidence the vendor claimed one and inventing a date for
    it would be worse than having none.

    ``%d/%m/%Y`` precedes ``%m/%d/%Y`` deliberately. Both are guesses for an
    ambiguous value and neither is knowable from the cell alone; what matters is
    that the choice is fixed and written down rather than varying by machine
    locale. Unambiguous ISO forms are tried first and are what Apollo emits.
    """

    cleaned = norm.collapse_whitespace(value)
    if cleaned is None:
        return None
    text = cleaned.replace("Z", "+0000") if cleaned.endswith("Z") else cleaned
    for fmt in _TIMESTAMP_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def normalize_provider_token(value: str | None) -> str | None:
    """Fold a vendor status token for comparison, keeping its meaning intact.

    ``Valid`` and ``valid`` become one token; ``Catch-all``, ``catch-all`` and
    ``Catch All`` become one token. Nothing is translated into this system's own
    vocabulary: the result is still the vendor's claim, merely written one way.
    """

    cleaned = norm.collapse_whitespace(value)
    if cleaned is None:
        return None
    folded = cleaned.casefold().replace("-", "_").replace(" ", "_")
    return _WS.sub("", folded) or None


def _read_address(
    raw: dict[str, str],
    detection: SchemaDetection,
    *,
    slot: str,
    email_field: str,
    source_field: str | None,
    status_field: str | None,
    verification_source_field: str | None,
    catch_all_field: str | None,
    last_verified_field: str | None,
) -> ImportedAddress | None:
    raw_value = norm.collapse_whitespace(_raw_cell(raw, detection, email_field))
    if raw_value is None:
        return None
    normalized = norm.normalize_email(raw_value)
    valid = normalized is not None and norm.is_valid_email(normalized)
    domain = normalized.rpartition("@")[2] if valid and normalized else None
    last_verified_raw = _cell(raw, detection, last_verified_field) if last_verified_field else None
    status_raw = _cell(raw, detection, status_field) if status_field else None
    catch_all_raw = _cell(raw, detection, catch_all_field) if catch_all_field else None
    return ImportedAddress(
        slot=slot,
        raw=raw_value,
        normalized=normalized if valid else None,
        is_valid_syntax=valid,
        domain=domain,
        provider_source=_cell(raw, detection, source_field) if source_field else None,
        provider_status_raw=status_raw,
        provider_status_normalized=normalize_provider_token(status_raw),
        provider_verification_source=(
            _cell(raw, detection, verification_source_field) if verification_source_field else None
        ),
        provider_catch_all_raw=catch_all_raw,
        provider_catch_all_normalized=normalize_provider_token(catch_all_raw),
        provider_last_verified_raw=last_verified_raw,
        provider_last_verified_at=parse_provider_timestamp(last_verified_raw),
    )


def _raw_cell(raw: dict[str, str], detection: SchemaDetection, canonical_field: str) -> str | None:
    column = detection.column_for(canonical_field)
    return raw.get(column) if column is not None else None


def _extras(raw: dict[str, str], detection: SchemaDetection) -> dict[str, str]:
    """Carry unrecognized columns verbatim, bounded in count and length."""

    extras: dict[str, str] = {}
    for column in detection.unmapped_columns:
        if len(extras) >= MAX_EXTRA_COLUMNS:
            break
        value = raw.get(column)
        if value is None or not value.strip():
            continue
        extras[column[:255]] = value[:MAX_EXTRA_VALUE_CHARS]
    return extras


def read_row(
    raw: dict[str, str],
    detection: SchemaDetection,
    *,
    row_number: int,
    sheet_index: int = 0,
    sheet_name: str | None = None,
) -> ApolloRow:
    """Read one verbatim row through *detection* into a normalized reading."""

    row = ApolloRow(row_number=row_number, sheet_index=sheet_index, sheet_name=sheet_name)

    row.first_name = norm.normalize_name(_cell(raw, detection, "first_name"))
    row.last_name = norm.normalize_name(_cell(raw, detection, "last_name"))
    row.title = norm.normalize_text(_cell(raw, detection, "title"))
    row.seniority = norm.normalize_text(_cell(raw, detection, "seniority"))
    row.departments = norm.normalize_text(_cell(raw, detection, "departments"))
    row.sub_departments = norm.normalize_text(_cell(raw, detection, "sub_departments"))
    person_linkedin = _cell(raw, detection, "person_linkedin_url")
    row.person_linkedin_url = norm.normalize_linkedin_url(person_linkedin)
    row.person_linkedin_identity = norm.normalize_linkedin_profile_url(person_linkedin)
    row.city = norm.normalize_text(_cell(raw, detection, "city"))
    row.state = norm.normalize_text(_cell(raw, detection, "state"))
    row.country = norm.normalize_country(_cell(raw, detection, "country"))
    row.phone = norm.normalize_text(_cell(raw, detection, "phone"))
    row.mobile_phone = norm.normalize_text(_cell(raw, detection, "mobile_phone"))
    row.corporate_phone = norm.normalize_text(_cell(raw, detection, "corporate_phone"))

    row.company_name = norm.normalize_name(_cell(raw, detection, "company_name"))
    row.company_name_for_emails = norm.normalize_name(
        _cell(raw, detection, "company_name_for_emails")
    )
    row.website_raw = _cell(raw, detection, "website")
    row.website_domain = norm.normalize_domain(row.website_raw)
    if row.website_domain is not None and not norm.is_valid_hostname(row.website_domain):
        row.warnings.append(
            (
                "website_unreadable",
                f"The Website value {row.website_raw!r} is not a readable hostname, "
                "so it was not used as company identity evidence.",
            )
        )
        row.website_domain = None
    company_linkedin = _cell(raw, detection, "company_linkedin_url")
    row.company_linkedin_url = norm.normalize_linkedin_url(company_linkedin)
    row.company_linkedin_identity = norm.normalize_linkedin_company_url(company_linkedin)
    row.company_address = norm.normalize_text(_cell(raw, detection, "company_address"))
    row.company_city = norm.normalize_text(_cell(raw, detection, "company_city"))
    row.company_state = norm.normalize_text(_cell(raw, detection, "company_state"))
    row.company_country = norm.normalize_country(_cell(raw, detection, "company_country"))
    row.employee_count = norm.normalize_text(_cell(raw, detection, "employee_count"))
    row.industry = norm.normalize_text(_cell(raw, detection, "industry"))
    row.keywords = norm.normalize_text(_cell(raw, detection, "keywords"))
    row.technologies = norm.normalize_text(_cell(raw, detection, "technologies"))
    row.annual_revenue = norm.normalize_text(_cell(raw, detection, "annual_revenue"))

    row.apollo_contact_id = _identifier(_cell(raw, detection, "apollo_contact_id"))
    row.apollo_account_id = _identifier(_cell(raw, detection, "apollo_account_id"))
    row.apollo_record_id = _identifier(_cell(raw, detection, "apollo_record_id"))

    row.primary = _read_address(
        raw,
        detection,
        slot="primary",
        email_field="email",
        source_field="primary_email_source",
        status_field="email_status",
        verification_source_field="primary_email_verification_source",
        catch_all_field="primary_email_catch_all_status",
        last_verified_field="primary_email_last_verified_at",
    )
    row.secondary = _read_address(
        raw,
        detection,
        slot="secondary",
        email_field="secondary_email",
        source_field="secondary_email_source",
        status_field="secondary_email_status",
        verification_source_field="secondary_email_verification_source",
        catch_all_field=None,
        last_verified_field="secondary_email_last_verified_at",
    )
    row.tertiary = _read_address(
        raw,
        detection,
        slot="tertiary",
        email_field="tertiary_email",
        source_field="tertiary_email_source",
        status_field="tertiary_email_status",
        verification_source_field="tertiary_email_verification_source",
        catch_all_field=None,
        last_verified_field="tertiary_email_last_verified_at",
    )

    row.extras = _extras(raw, detection)
    _add_address_warnings(row)
    _add_formula_warnings(row, raw)
    return row


def _identifier(value: str | None) -> str | None:
    """Keep a vendor key verbatim, bounded to the column width."""

    if value is None:
        return None
    return value[:256] or None


def _add_address_warnings(row: ApolloRow) -> None:
    """Record the address conditions IMP-001 §13 asks to be surfaced."""

    primary, secondary, tertiary = row.primary, row.secondary, row.tertiary

    if primary is not None and not primary.is_valid_syntax:
        if secondary is not None and secondary.is_valid_syntax:
            row.warnings.append(
                (
                    "primary_malformed_secondary_valid",
                    "The primary Email is not a valid address while the Secondary Email is. "
                    "The secondary was NOT promoted — choosing between addresses is an "
                    "operator decision, not an import one.",
                )
            )
        else:
            row.warnings.append(
                (
                    "primary_malformed",
                    f"The primary Email {primary.raw!r} is not a valid address.",
                )
            )

    seen: dict[str, str] = {}
    for address in row.addresses:
        if address.normalized is None:
            continue
        if address.normalized in seen:
            row.warnings.append(
                (
                    "duplicate_address",
                    f"The {address.slot} address repeats the {seen[address.normalized]} "
                    "address; it was retained as supplied.",
                )
            )
        else:
            seen[address.normalized] = address.slot

    domains = {a.domain for a in row.addresses if a.domain and not a.is_public_domain}
    if len(domains) > 1:
        row.warnings.append(
            (
                "addresses_span_companies",
                "The supplied addresses use more than one company domain "
                f"({', '.join(sorted(domains))}); only the primary was used.",
            )
        )

    for alternate in (secondary, tertiary):
        if alternate is not None and alternate.is_public_domain:
            row.warnings.append(
                (
                    f"{alternate.slot}_public_domain",
                    f"The {alternate.slot} address is at the public mailbox provider "
                    f"{alternate.domain}; it was retained but establishes nothing about "
                    "the company.",
                )
            )

    for address in row.addresses:
        claims_status = address.provider_status_normalized is not None
        claims_source = address.provider_verification_source is not None
        if claims_source and not claims_status:
            row.warnings.append(
                (
                    f"{address.slot}_inconsistent_provider_metadata",
                    f"The {address.slot} address names a verification source "
                    f"({address.provider_verification_source}) but no status.",
                )
            )
        if address.provider_last_verified_raw and address.provider_last_verified_at is None:
            row.warnings.append(
                (
                    f"{address.slot}_unreadable_verified_at",
                    f"The {address.slot} last-verified value "
                    f"{address.provider_last_verified_raw!r} could not be read as a date; "
                    "the original text was kept.",
                )
            )


def _add_formula_warnings(row: ApolloRow, raw: dict[str, str]) -> None:
    """Note cells a spreadsheet application would treat as formulas.

    Nothing evaluates them here. The warning exists because a value that looks
    like an expression is worth an operator seeing before it is stored under a
    person's name, and because anything exported later is neutralized on the way
    out by :func:`neutralize_formula`.
    """

    offenders = [column for column, value in raw.items() if looks_like_formula(value)]
    if offenders:
        row.warnings.append(
            (
                "formula_like_cells",
                "This row has cell(s) beginning with a formula character "
                f"({', '.join(sorted(offenders)[:5])}). They were stored as text and "
                "never evaluated.",
            )
        )


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

#: The canonical values a fingerprint is computed over: everything the import
#: persists as meaning. A change to any of them is a genuinely different
#: statement about the person and deserves fresh evidence; a change to a column
#: we do not read cannot silently invalidate what we already stored.
_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "first_name",
    "last_name",
    "title",
    "seniority",
    "person_linkedin_identity",
    "city",
    "state",
    "country",
    "company_name",
    "company_name_for_emails",
    "website_domain",
    "company_linkedin_identity",
    "employee_count",
    "industry",
    "apollo_contact_id",
    "apollo_account_id",
    "apollo_record_id",
)


def row_fingerprint(row: ApolloRow) -> str:
    """A deterministic fingerprint of one row's meaning.

    Case-folded on the free-text fields so a re-export that changed only casing
    is recognized as the same statement, and exact on the identifiers, which are
    opaque and may legitimately differ by case.
    """

    payload: dict[str, Any] = {}
    for name in _FINGERPRINT_FIELDS:
        value = getattr(row, name)
        if value is None:
            payload[name] = None
        elif name.startswith("apollo_"):
            payload[name] = value
        else:
            payload[name] = str(value).casefold()
    for address in ("primary", "secondary", "tertiary"):
        supplied: ImportedAddress | None = getattr(row, address)
        payload[f"{address}_email"] = (
            supplied.normalized or supplied.raw.casefold() if supplied else None
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def bounded_source_payload(row: ApolloRow) -> dict[str, Any]:
    """The normalized, bounded record of one source row kept on its outcome.

    Not the whole workbook and not the whole raw row — the immutable
    ``import_rows.raw_data`` already holds every original cell verbatim, and
    duplicating it per outcome would store the operator's spreadsheet twice.
    This is the reading, which is the thing a later reviewer actually needs
    beside the outcome.
    """

    payload: dict[str, Any] = {
        "schema": APOLLO_SCHEMA_ID,
        "reader_version": APOLLO_READER_VERSION,
        "row_number": row.row_number,
        "sheet_name": row.sheet_name,
        "first_name": row.first_name,
        "last_name": row.last_name,
        "title": row.title,
        "seniority": row.seniority,
        "departments": row.departments,
        "sub_departments": row.sub_departments,
        "person_linkedin_url": row.person_linkedin_url,
        "city": row.city,
        "state": row.state,
        "country": row.country,
        "company_name": row.company_name,
        "company_name_for_emails": row.company_name_for_emails,
        "website_domain": row.website_domain,
        "company_linkedin_url": row.company_linkedin_url,
        "company_city": row.company_city,
        "company_state": row.company_state,
        "company_country": row.company_country,
        "employee_count": row.employee_count,
        "industry": row.industry,
        "annual_revenue": row.annual_revenue,
        "apollo_contact_id": row.apollo_contact_id,
        "apollo_account_id": row.apollo_account_id,
        "apollo_record_id": row.apollo_record_id,
        "extras": row.extras,
    }
    for address in row.addresses:
        payload[f"{address.slot}_email"] = address.normalized or address.raw
        payload[f"{address.slot}_email_provider_status"] = address.provider_status_normalized
    return payload
