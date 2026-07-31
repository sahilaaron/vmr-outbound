"""Seed the first-release Company Intelligence vocabularies (CI-001).

Two sources, both committed in ``data/``:

* ``industry_categories.json`` — the operator-supplied industry taxonomy, used
  verbatim. Its sixteen categories become depth-0 terms and their entries become
  depth-1 subindustries. Nothing was renamed, merged, dropped or invented.
* ``supporting_vocabularies.json`` — short controlled lists for business model,
  company type, customer segment and operating market, plus their seed aliases.

One deliberate adjustment, and it is the only one. Every category in the supplied
file ends with an entry called ``"Others"``, so sixteen different subindustries
would normalize to the identical string and a lookup would return whichever the
database happened to return first — a silent, order-dependent misclassification.
Each is therefore stored with its category in the canonical label
(``"Others (Manufacturing)"``) and a category-scoped code. The supplied word is
preserved; only the ambiguity is removed. The bare word ``"others"`` is
deliberately **not** registered as an alias of any of them, because it genuinely
is ambiguous and an alias that guesses would be worse than an unmapped value an
operator can see.

Seeding is idempotent at the edition level: an edition that already exists is
left exactly as it is. Vocabularies are immutable once published, so re-running
this never edits anything — publishing a corrected list means a new version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import IntelligenceDimension, TaxonomyAliasSource
from app.models.intelligence_taxonomy import IntelligenceTaxonomy
from app.services.company_intelligence import taxonomy as taxonomy_service
from app.services.company_intelligence.normalization import slugify_code

DATA_DIR = Path(__file__).parent / "data"
INDUSTRY_FILE = DATA_DIR / "industry_categories.json"
SUPPORTING_FILE = DATA_DIR / "supporting_vocabularies.json"

#: The version label every first-release edition carries. Bumping this is how a
#: corrected vocabulary is published; the previous edition is never edited.
SEED_VERSION = "2026.07"

INDUSTRY_TITLE = "VMR industry and subindustry taxonomy"
INDUSTRY_SOURCE = "operator-supplied industry_categories.json"

#: The literal entry that repeats in every category of the supplied file.
_AMBIGUOUS_LEAF = "Others"

_SUPPORTING_DIMENSIONS: dict[str, IntelligenceDimension] = {
    "business_model": IntelligenceDimension.BUSINESS_MODEL,
    "company_type": IntelligenceDimension.COMPANY_TYPE,
    "customer_segment": IntelligenceDimension.CUSTOMER_SEGMENT,
    "operating_market": IntelligenceDimension.OPERATING_MARKET,
}


@dataclass(frozen=True)
class SeedReport:
    """What a seeding pass actually did, per dimension."""

    created: tuple[str, ...]
    skipped: tuple[str, ...]
    term_count: int
    alias_count: int

    @property
    def changed(self) -> bool:
        return bool(self.created)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _existing(
    session: Session, dimension: IntelligenceDimension, version: str
) -> IntelligenceTaxonomy | None:
    return session.scalars(
        select(IntelligenceTaxonomy).where(
            IntelligenceTaxonomy.dimension == dimension,
            IntelligenceTaxonomy.version == version,
        )
    ).first()


def seed_vocabularies(
    session: Session,
    *,
    version: str = SEED_VERSION,
    activate: bool = True,
    created_by: str | None = None,
) -> SeedReport:
    """Publish (and optionally activate) the first-release vocabularies.

    Safe to run repeatedly. An edition that exists is reported as skipped and
    left untouched — including its aliases, because an operator may have added
    some and this function has no business deciding they were wrong.
    """

    created: list[str] = []
    skipped: list[str] = []
    terms = 0
    aliases = 0

    industry_created, industry_terms, industry_aliases = _seed_industry(
        session, version=version, activate=activate, created_by=created_by
    )
    (created if industry_created else skipped).append(IntelligenceDimension.INDUSTRY.value)
    terms += industry_terms
    aliases += industry_aliases

    supporting = _load(SUPPORTING_FILE)
    for key, dimension in _SUPPORTING_DIMENSIONS.items():
        block = supporting[key]
        made, term_count, alias_count = _seed_flat(
            session,
            dimension=dimension,
            block=block,
            version=version,
            activate=activate,
            created_by=created_by,
        )
        (created if made else skipped).append(dimension.value)
        terms += term_count
        aliases += alias_count

    return SeedReport(
        created=tuple(created),
        skipped=tuple(skipped),
        term_count=terms,
        alias_count=aliases,
    )


def _seed_industry(
    session: Session,
    *,
    version: str,
    activate: bool,
    created_by: str | None,
) -> tuple[bool, int, int]:
    existing = _existing(session, IntelligenceDimension.INDUSTRY, version)
    if existing is not None:
        if activate and not existing.is_active:
            taxonomy_service.activate_taxonomy(session, taxonomy=existing)
        return False, 0, 0

    edition = taxonomy_service.create_taxonomy(
        session,
        dimension=IntelligenceDimension.INDUSTRY,
        version=version,
        title=INDUSTRY_TITLE,
        description=(
            "Sixteen top-level industries and their subindustries, used verbatim from "
            "the operator-supplied taxonomy. Category-level terms classify the "
            "INDUSTRY dimension; child terms classify SUBINDUSTRY."
        ),
        source=INDUSTRY_SOURCE,
        created_by=created_by,
    )

    payload: dict[str, list[str]] = _load(INDUSTRY_FILE)
    terms = 0
    aliases = 0
    for category_order, (category, children) in enumerate(payload.items()):
        category_code = slugify_code(category)
        parent = taxonomy_service.add_term(
            session,
            taxonomy=edition,
            code=category_code,
            canonical_label=category,
            sort_order=category_order,
        )
        terms += 1
        for alias in _category_aliases(category):
            taxonomy_service.add_alias(
                session,
                term=parent,
                alias=alias,
                source=TaxonomyAliasSource.SEED,
                created_by=created_by,
            )
            aliases += 1

        for child_order, child in enumerate(children):
            if child.strip() == _AMBIGUOUS_LEAF:
                label = f"{_AMBIGUOUS_LEAF} ({category})"
                code = f"{category_code}-others"
            else:
                label = child
                code = f"{category_code}--{slugify_code(child)}"
            taxonomy_service.add_term(
                session,
                taxonomy=edition,
                code=code[:160],
                canonical_label=label,
                parent=parent,
                sort_order=child_order,
            )
            terms += 1

    if activate:
        taxonomy_service.activate_taxonomy(session, taxonomy=edition)
    return True, terms, aliases


def _category_aliases(category: str) -> tuple[str, ...]:
    """Seed aliases for one industry category.

    Only two mechanical forms, both unambiguous within this list:

    * the category with ``" & "`` written out as ``" and "`` — handled by
      normalization already, so it is not registered here;
    * each side of an ``" & "`` on its own ("Pharma", "Healthcare"), which is how
      people actually write these, but **only** when that fragment is not also a
      fragment of another category.

    Anything beyond that is a human decision, made in the Admin vocabulary
    screen with an author attached, not a rule guessing on their behalf.
    """

    if " & " not in category:
        return ()
    fragments = tuple(part.strip() for part in category.split(" & ") if part.strip())
    if len(fragments) < 2:
        return ()
    return tuple(fragment for fragment in fragments if fragment not in _AMBIGUOUS_FRAGMENTS)


#: Fragments that appear on both sides of an "&" in more than one category, or
#: that name something broader than the category itself. Registering them would
#: make one alias mean two industries, which ``add_alias`` refuses anyway --
#: naming them here keeps the seed deterministic rather than order-dependent.
_AMBIGUOUS_FRAGMENTS = frozenset(
    {
        "Technology",
        "Minerals",
        "Metals",
        "Communication",
        "Material",
        "Power",
        "Transportation",
        "Engineering",
        "Semiconductor",
        "Insurance",
        "Financial Services",
        "Defence",
        "Beverages",
    }
)


def _seed_flat(
    session: Session,
    *,
    dimension: IntelligenceDimension,
    block: dict[str, Any],
    version: str,
    activate: bool,
    created_by: str | None,
) -> tuple[bool, int, int]:
    existing = _existing(session, dimension, version)
    if existing is not None:
        if activate and not existing.is_active:
            taxonomy_service.activate_taxonomy(session, taxonomy=existing)
        return False, 0, 0

    edition = taxonomy_service.create_taxonomy(
        session,
        dimension=dimension,
        version=version,
        title=str(block.get("title") or dimension.value),
        description=block.get("_note"),
        source="supporting_vocabularies.json",
        created_by=created_by,
    )
    terms = 0
    aliases = 0
    for order, entry in enumerate(block["terms"]):
        term = taxonomy_service.add_term(
            session,
            taxonomy=edition,
            code=str(entry["code"]),
            canonical_label=str(entry["label"]),
            sort_order=order,
        )
        terms += 1
        for alias in entry.get("aliases", ()):
            taxonomy_service.add_alias(
                session,
                term=term,
                alias=str(alias),
                source=TaxonomyAliasSource.SEED,
                created_by=created_by,
            )
            aliases += 1
    if activate:
        taxonomy_service.activate_taxonomy(session, taxonomy=edition)
    return True, terms, aliases
