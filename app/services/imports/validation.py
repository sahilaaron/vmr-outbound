"""Per-row validation for the authorized contact-input contract (DAT-002).

Each row is validated independently so a batch can partially succeed. Errors are
actionable and row-level (row number, column, reason) and are never silently
dropped. Column names are matched case-insensitively and trimmed; unknown columns
are preserved on the raw row but ignored here (contact-input contract).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.services.imports import normalization as norm

REQUIRED_COLUMNS: tuple[str, ...] = (
    "first_name",
    "last_name",
    "company_name",
    "company_domain",
)
RECOMMENDED_COLUMNS: tuple[str, ...] = (
    "title",
    "email",
    "linkedin_url",
    "country",
    "industry",
    "company_size",
)
PROVENANCE_COLUMNS: tuple[str, ...] = (
    "source_name",
    "source_reference",
    "exported_by",
    "exported_at",
)


@dataclass(frozen=True)
class RowError:
    """One actionable, column-scoped validation error."""

    column: str | None
    code: str
    message: str


@dataclass
class ValidatedRow:
    """The validation outcome for a single raw CSV row."""

    row_number: int
    raw: dict[str, str]
    normalized: dict[str, str | None] = field(default_factory=dict)
    natural_key: str | None = None
    errors: list[RowError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _case_insensitive_lookup(raw: dict[str, str]) -> dict[str, str]:
    """Map trimmed, lower-cased header -> value for case-insensitive access."""

    lookup: dict[str, str] = {}
    for key, value in raw.items():
        if key is None:
            continue
        lookup[key.strip().lower()] = value if value is not None else ""
    return lookup


def _parse_export_date(value: str | None) -> str | None:
    """Parse an ``exported_at`` cell leniently to an ISO date string.

    Provenance is metadata, not a gate: an unparseable date is dropped (stored as
    None) rather than rejecting the whole row.
    """

    cleaned = norm.collapse_whitespace(value)
    if cleaned is None:
        return None
    try:
        return date.fromisoformat(cleaned).isoformat()
    except ValueError:
        return None


def validate_row(row_number: int, raw: dict[str, str]) -> ValidatedRow:
    """Validate and normalize one raw row against the contact-input contract."""

    lookup = _case_insensitive_lookup(raw)
    result = ValidatedRow(row_number=row_number, raw=raw)
    normalized: dict[str, str | None] = {}

    # --- Required text fields ------------------------------------------------
    for column in ("first_name", "last_name", "company_name"):
        value = norm.normalize_name(lookup.get(column))
        normalized[column] = value
        if value is None:
            result.errors.append(
                RowError(
                    column=column,
                    code="missing_required",
                    message=f"row {row_number}: {column} is required but was empty",
                )
            )

    # --- Required domain -----------------------------------------------------
    raw_domain = norm.collapse_whitespace(lookup.get("company_domain"))
    domain = norm.normalize_domain(lookup.get("company_domain"))
    if raw_domain is None:
        normalized["company_domain"] = None
        result.errors.append(
            RowError(
                column="company_domain",
                code="missing_required",
                message=f"row {row_number}: company_domain is required but was empty",
            )
        )
    elif domain is None or not norm.is_valid_hostname(domain):
        normalized["company_domain"] = None
        result.errors.append(
            RowError(
                column="company_domain",
                code="invalid_domain",
                message=(
                    f"row {row_number}: company_domain {raw_domain!r} is not a valid hostname"
                ),
            )
        )
    else:
        normalized["company_domain"] = domain

    # --- Optional email (validated when present) -----------------------------
    raw_email = norm.collapse_whitespace(lookup.get("email"))
    if raw_email is None:
        normalized["email"] = None
    else:
        email = norm.normalize_email(raw_email)
        if email is not None and norm.is_valid_email(email):
            normalized["email"] = email
        else:
            normalized["email"] = None
            result.errors.append(
                RowError(
                    column="email",
                    code="invalid_email",
                    message=f"row {row_number}: email {raw_email!r} is not a valid address",
                )
            )

    # --- Optional recommended fields -----------------------------------------
    normalized["title"] = norm.normalize_text(lookup.get("title"))
    normalized["linkedin_url"] = norm.normalize_linkedin_url(lookup.get("linkedin_url"))
    normalized["country"] = norm.normalize_country(lookup.get("country"))
    normalized["industry"] = norm.normalize_text(lookup.get("industry"))
    normalized["company_size"] = norm.normalize_text(lookup.get("company_size"))

    # --- Optional per-row provenance -----------------------------------------
    normalized["source_name"] = norm.collapse_whitespace(lookup.get("source_name"))
    normalized["source_reference"] = norm.collapse_whitespace(lookup.get("source_reference"))
    normalized["exported_by"] = norm.collapse_whitespace(lookup.get("exported_by"))
    normalized["exported_at"] = _parse_export_date(lookup.get("exported_at"))

    result.normalized = normalized

    # Natural key requires the identity trio; only meaningful for valid rows.
    if normalized["first_name"] and normalized["last_name"] and normalized["company_domain"]:
        result.natural_key = norm.build_natural_key(
            normalized["first_name"],
            normalized["last_name"],
            normalized["company_domain"],
        )

    return result
