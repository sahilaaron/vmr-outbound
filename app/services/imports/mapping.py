"""Column mapping: source columns -> supported system fields.

The operator confirms how the columns of an uploaded file correspond to the
contact-input contract before anything is validated or imported. The mapping is
deterministic data (a plain ``source column -> system field`` dictionary), it is
validated here (unknown fields, duplicate targets, missing required fields), and
the confirmed mapping is stored on the batch (``import_batches.column_mapping``)
with a ``mapper_version`` so a batch's interpretation of its file stays
reproducible.

Applying a mapping never mutates the verbatim raw row: the original row is
persisted untouched and the mapped view is what flows into validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.imports import validation

MAPPER_VERSION = "mapper-1"

# Every system field the import contract accepts, in display order.
SYSTEM_FIELDS: tuple[str, ...] = (
    validation.REQUIRED_COLUMNS + validation.RECOMMENDED_COLUMNS + validation.PROVENANCE_COLUMNS
)

# Common alternate spellings seen in real exports, used only to *suggest* an
# automatic mapping. The operator always confirms the final mapping.
_ALIASES: dict[str, str] = {
    "firstname": "first_name",
    "first": "first_name",
    "given name": "first_name",
    "lastname": "last_name",
    "last": "last_name",
    "surname": "last_name",
    "family name": "last_name",
    "company": "company_name",
    "organisation": "company_name",
    "organization": "company_name",
    "account name": "company_name",
    "domain": "company_domain",
    "website": "company_domain",
    "web site": "company_domain",
    "company website": "company_domain",
    "url": "company_domain",
    "e-mail": "email",
    "email address": "email",
    "work email": "email",
    "job title": "title",
    "position": "title",
    "role": "title",
    "linkedin": "linkedin_url",
    "linkedin profile": "linkedin_url",
    "person linkedin url": "linkedin_url",
    "country/region": "country",
    "location": "country",
    "employees": "company_size",
    "company size": "company_size",
    "headcount": "company_size",
    "# employees": "company_size",
    "source": "source_name",
    "list name": "source_reference",
    "exported by": "exported_by",
    "export date": "exported_at",
    "exported at": "exported_at",
    # Sales Navigator capture field names (camelCase collapses to one token in
    # _canon), so an operator opening a captured batch gets a sensible suggested
    # mapping. company_domain is intentionally absent from captures — Sales
    # Navigator does not expose it — so it stays unmapped for the operator.
    "companyname": "company_name",
    "linkedinprofileurl": "linkedin_url",
}


@dataclass(frozen=True)
class MappingProblem:
    """One actionable problem with a proposed mapping."""

    code: str
    message: str


@dataclass
class MappingCheck:
    """Result of validating a proposed mapping against a header."""

    problems: list[MappingProblem] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.problems


def _canon(name: str) -> str:
    return " ".join(name.strip().lower().replace("_", " ").split())


def suggest_mapping(header: list[str]) -> dict[str, str]:
    """Suggest a mapping for *header* by exact name, then by known aliases.

    Each system field is targeted at most once (first matching column wins).
    Columns with no confident correspondence are left unmapped for the operator.
    """

    suggestion: dict[str, str] = {}
    taken: set[str] = set()
    system_by_canon = {_canon(f): f for f in SYSTEM_FIELDS}

    for column in header:
        canon = _canon(column)
        target = system_by_canon.get(canon) or _ALIASES.get(canon)
        if target and target not in taken:
            suggestion[column] = target
            taken.add(target)
    return suggestion


def check_mapping(mapping: dict[str, str], header: list[str]) -> MappingCheck:
    """Validate a proposed mapping. Every problem is actionable and specific."""

    check = MappingCheck()
    header_set = set(header)
    targets_seen: dict[str, str] = {}

    for column, target in mapping.items():
        if column not in header_set:
            check.problems.append(
                MappingProblem(
                    code="unknown_column",
                    message=f"Mapped column {column!r} does not exist in the file.",
                )
            )
        if target not in SYSTEM_FIELDS:
            check.problems.append(
                MappingProblem(
                    code="unknown_field",
                    message=f"{target!r} is not a supported system field.",
                )
            )
            continue
        if target in targets_seen:
            check.problems.append(
                MappingProblem(
                    code="duplicate_target",
                    message=(
                        f"Both {targets_seen[target]!r} and {column!r} are mapped to "
                        f"{target!r}. Each system field accepts exactly one source column."
                    ),
                )
            )
        else:
            targets_seen[target] = column

    for required in validation.REQUIRED_COLUMNS:
        if required not in targets_seen:
            check.problems.append(
                MappingProblem(
                    code="missing_required",
                    message=(
                        f"No source column is mapped to required field {required!r}. "
                        "Map a column to it before continuing."
                    ),
                )
            )
    return check


def apply_mapping(raw: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    """Return the mapped view of one verbatim row (the raw row is not touched).

    Only mapped columns flow into validation; unmapped columns stay visible on
    the stored raw row. Column-name matching is exact (the mapping was built
    from this file's own header).
    """

    mapped: dict[str, str] = {}
    for column, target in mapping.items():
        if column in raw:
            mapped[target] = raw[column]
    return mapped


def identity_mapping(header: list[str]) -> dict[str, str]:
    """Mapping used when a file's header already matches the contract exactly."""

    return {
        column: column.strip().lower()
        for column in header
        if column.strip().lower() in SYSTEM_FIELDS
    }
