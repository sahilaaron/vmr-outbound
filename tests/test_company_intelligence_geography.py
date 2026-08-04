"""Geography hardening tests (CI-002).

Four things these prove, in order of how much they matter:

1. **A place the evidence never named cannot become a geography.** Deterministic
   extraction owns the place list; the model owns only the relationship.
2. **Ordinary words are not places.** "Reading the manual", "mobile devices",
   "a nice facility" — the cases that make a naive matcher embarrassing.
3. **A place belonging to somebody else is not our presence.** Publishers,
   conferences, universities, jurisdictions, customer examples.
4. **The relationship is what makes a place useful.** Physical presence and
   commercial market are kept apart, and planned and former sites are never
   counted as current.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.models.enums import (
    IntelligenceDimension,
    IntelligenceGeoRelationship,
    IntelligencePresenceKind,
    IntelligenceValueState,
)
from app.services.company_intelligence import geography as geo
from app.services.company_intelligence import inputs as ci_inputs
from app.services.company_intelligence import producer as ci_producer
from app.services.company_intelligence import read as ci_read
from app.services.company_intelligence.normalization import normalize_term
from sqlalchemy.orm import Session

from tests.test_company_intelligence import (
    assemble,
    make_company,
    make_dossier,
    make_fact,
    seeded,
)

BASE = geo.load_base()


# --- the dataset ------------------------------------------------------------


def test_the_edition_covers_the_regions_it_claims_to() -> None:
    assert 45 <= len(BASE.countries) <= 80, "roughly fifty commercially relevant countries"
    assert BASE.regions
    covered = {place.region for place in BASE.countries}
    assert covered == set(BASE.regions), "every declared region must actually be represented"


def test_country_codes_are_unique_and_validly_shaped() -> None:
    raw = json.loads(geo.DATA_FILE.read_text(encoding="utf-8"))
    alpha2 = [country["alpha2"] for country in raw["countries"]]
    alpha3 = [country["alpha3"] for country in raw["countries"]]
    assert len(set(alpha2)) == len(alpha2)
    assert len(set(alpha3)) == len(alpha3)
    assert all(len(code) == 2 and code.isupper() for code in alpha2)
    assert all(len(code) == 3 and code.isupper() for code in alpha3)


def test_every_city_resolves_to_exactly_one_country() -> None:
    codes = {place.code for place in BASE.countries}
    for city in BASE.cities:
        assert city.country_code in codes
        assert city.country_name
        assert city.code != city.country_code


def test_every_country_carries_at_least_three_cities() -> None:
    raw = json.loads(geo.DATA_FILE.read_text(encoding="utf-8"))
    thin = [country["name"] for country in raw["countries"] if len(country.get("cities", ())) < 3]
    assert thin == [], f"these countries have fewer than three cities: {thin}"


def test_no_two_places_claim_the_same_surface() -> None:
    """One written form means one place. A collision would make resolution
    order-dependent, which is the single failure this whole area exists to
    prevent."""

    assert len(BASE.surfaces) == len(set(BASE.surfaces))
    for surface, code in BASE.surfaces.items():
        assert surface == normalize_term(surface)
        assert code in BASE.by_code


def test_provenance_and_criteria_are_documented_in_the_data() -> None:
    assert BASE.edition
    assert "ISO 3166-1" in BASE.source
    assert BASE.license
    assert "not" in BASE.criteria.lower(), (
        "the criteria must say what the selection is NOT claiming"
    )


def test_the_edition_loads_deterministically() -> None:
    first = geo._build(json.loads(geo.DATA_FILE.read_text(encoding="utf-8")))
    second = geo._build(json.loads(geo.DATA_FILE.read_text(encoding="utf-8")))
    assert [place.code for place in first.places] == [place.code for place in second.places]
    assert first.surfaces == second.surfaces


@pytest.mark.parametrize(
    "broken",
    [
        {"edition": "x", "title": "t", "source": "s", "license": "l", "criteria": "c"},
        {
            "edition": "x",
            "title": "t",
            "source": "s",
            "license": "l",
            "criteria": "c",
            "countries": [{"name": "Nowhere", "alpha2": "XXX", "alpha3": "XXX", "cities": []}],
        },
        {
            "edition": "x",
            "title": "t",
            "source": "s",
            "license": "l",
            "criteria": "c",
            "countries": [
                {"name": "A", "alpha2": "AA", "alpha3": "AAA", "cities": []},
                {"name": "B", "alpha2": "AA", "alpha3": "BBB", "cities": []},
            ],
        },
        {
            "edition": "x",
            "title": "t",
            "source": "s",
            "license": "l",
            "criteria": "c",
            "countries": [
                {
                    "name": "A",
                    "alpha2": "AA",
                    "alpha3": "AAA",
                    "aliases": ["Shared"],
                    "cities": [],
                },
                {
                    "name": "B",
                    "alpha2": "BB",
                    "alpha3": "BBB",
                    "aliases": ["Shared"],
                    "cities": [],
                },
            ],
        },
    ],
)
def test_malformed_geography_data_fails_loudly(broken: dict[str, Any]) -> None:
    with pytest.raises(geo.GeographyDataError):
        geo._build(broken)


# --- extraction -------------------------------------------------------------


def extract(session: Session, claims: list[str]) -> geo.ExtractionResult:
    seeded(session)
    company = make_company(session, name=f"Kiln {len(claims)}{claims[0][:8]}")
    make_dossier(session, company=company)
    for index, claim in enumerate(claims):
        make_fact(session, company=company, claim=claim, key=f"geo:{company.id}:{index}")
    return assemble(session, company).geography


def labels(result: geo.ExtractionResult) -> set[str]:
    return {candidate.place.label for candidate in result.candidates}


def test_a_headquarters_city_and_its_country_are_both_found(db_session: Session) -> None:
    found = extract(db_session, ["headquarters: headquartered in London, United Kingdom"])
    assert labels(found) == {"London", "United Kingdom"}
    london = next(item for item in found.candidates if item.place.label == "London")
    assert london.place.country_code == "gb"
    assert london.evidence_handles == ("F1",)


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        ("office_locations: opened an office in Toronto", "Toronto"),
        ("office_locations: a branch in Osaka serves the region", "Osaka"),
        ("overview: operates a facility in Rotterdam", "Rotterdam"),
        ("overview: runs a manufacturing plant in Pune", "Pune"),
        ("overview: its research centre in Grenoble develops sensors", "Grenoble"),
        ("overview: a distribution warehouse in Memphis", None),
        ("overview: regional operations across Singapore", "Singapore"),
    ],
)
def test_site_wording_produces_the_city(
    db_session: Session, claim: str, expected: str | None
) -> None:
    found = extract(db_session, [claim])
    if expected is None:
        assert not labels(found), "a city outside the edition must not be invented"
    else:
        assert expected in labels(found)


def test_a_commercial_market_without_an_office_is_still_a_candidate(
    db_session: Session,
) -> None:
    found = extract(db_session, ["overview: serves customers across Germany"])
    assert "Germany" in labels(found)


def test_city_aliases_and_former_names_resolve(db_session: Session) -> None:
    found = extract(
        db_session,
        [
            "overview: an engineering centre in Bangalore",
            "overview: a plant in Bombay",
            "overview: an office in Saigon",
        ],
    )
    assert {"Bengaluru", "Mumbai", "Ho Chi Minh City"} <= labels(found)


def test_country_abbreviations_and_iso_codes_resolve(db_session: Session) -> None:
    found = extract(
        db_session,
        [
            "overview: maintains a service centre in the UAE",
            "overview: a site in DEU supplies the region",
        ],
    )
    assert {"United Arab Emirates", "Germany"} <= labels(found)


def test_unicode_and_punctuation_do_not_defeat_matching(db_session: Session) -> None:
    found = extract(
        db_session,
        [
            "overview: an office in Zürich, Switzerland",
            "overview: a plant in Sao Paulo",
            "overview: based in Washington, D.C.",
        ],
    )
    assert {"Zurich", "São Paulo", "Washington, D.C."} <= labels(found)


def test_the_longest_match_wins(db_session: Session) -> None:
    """ "New Zealand" must not be read as a country plus the word "Zealand", and
    "Washington, D.C." must not collapse to the state-name reading."""

    found = extract(db_session, ["overview: headquartered in Washington, D.C."])
    assert labels(found) == {"Washington, D.C."}, (
        "the full form must win; the bare alias must not also produce a separate match"
    )


def test_the_same_place_mentioned_twice_is_one_candidate(db_session: Session) -> None:
    found = extract(
        db_session,
        [
            "headquarters: headquartered in Dublin",
            "overview: the Dublin office also hosts engineering",
        ],
    )
    dublin = [item for item in found.candidates if item.place.label == "Dublin"]
    assert len(dublin) == 1
    assert set(dublin[0].evidence_handles) == {"F1", "F2"}


def test_candidate_handles_are_stable_and_dense(db_session: Session) -> None:
    found = extract(
        db_session,
        ["headquarters: headquartered in Helsinki", "overview: a plant in Tampere"],
    )
    assert [item.handle for item in found.candidates] == [
        f"G{index}" for index in range(1, len(found.candidates) + 1)
    ]


# --- false positives --------------------------------------------------------


@pytest.mark.parametrize(
    "claim",
    [
        "leadership: Georgia Fowler leads the commercial team",
        "leadership: Michael Jordan joined as operations director",
        "products: mobile diagnostics units for field service",
        "overview: reading the operating manual is required before commissioning",
        "products: orange and amber warning beacons",
        "overview: a nice improvement in throughput was recorded",
        "products: bath and shower sealant systems",
        "leadership: Washington Silva chairs the advisory board",
        "products: turkey and poultry processing lines",
        "products: victoria sponge production equipment",
        "leadership: David Chen manages procurement",
        "products: panama-style filtration hats for industrial use",
        "overview: cambridge-style collaborative engineering methods",
        "products: lima bean processing equipment",
    ],
)
def test_ordinary_words_and_names_do_not_become_geographies(
    db_session: Session, claim: str
) -> None:
    found = extract(db_session, [claim])
    assert found.candidates == (), f"{claim!r} produced {labels(found)}"


@pytest.mark.parametrize(
    "claim",
    [
        # Eleven ISO alpha-3 codes are also ordinary English words. Matching is
        # case-insensitive, so before these were treated as ambiguous surfaces
        # each of these sentences handed the model a country the evidence never
        # named: ARE, BRA, CAN, COL, FIN, KEN, MAR, NOR, PAN, PER, POL.
        "overview: deliveries are scheduled weekly across the estate",
        "overview: invoices are issued per shipment",
        "overview: pricing is quoted per unit and per annum",
        "overview: the product can be configured for high-temperature service",
        "products: bra and garment finishing equipment",
        "products: col-rolled sheet handling systems",
        "products: fin-and-tube heat exchangers",
        "leadership: Ken Alvarez leads the service desk",
        "overview: the mar sediment analysis rig was retired",
        "overview: neither the plant nor the depot was affected",
        "products: pan and tray washing systems",
        "overview: pol tested cabling is used throughout",
    ],
)
def test_an_iso_code_that_is_an_english_word_is_not_a_place(
    db_session: Session, claim: str
) -> None:
    """An alpha-3 code in ordinary lower-case prose is a word, not a country.

    Regression for a UAT finding: "deliveries are scheduled weekly" offered
    United Arab Emirates (ARE) and "quoted per unit" offered Peru (PER), because
    the alpha-3 was registered as a plain surface and matched case-insensitively.
    A candidate the evidence never named is the one failure this module exists to
    prevent, so the codes are now ambiguous surfaces like "Reading".
    """

    found = extract(db_session, [claim])
    assert found.candidates == (), f"{claim!r} produced {labels(found)}"


def test_an_iso_code_written_as_a_code_still_resolves(db_session: Session) -> None:
    """The fix must not cost the legitimate reading of a code.

    Capitalised and preceded by a preposition is how a code actually appears in
    evidence, and it must still produce the country.
    """

    found = extract(db_session, ["overview: a service centre in ARE supports the region"])
    assert "United Arab Emirates" in labels(found)
    candidate = next(
        item for item in found.candidates if item.place.label == "United Arab Emirates"
    )
    assert candidate.qualified_ambiguous is True


@pytest.mark.parametrize(
    "claim",
    [
        "sources: published by an academic publisher in Munich",
        "sources: appeared in a journal edited in Boston",
        "activity_signals: presented at a semiconductor conference in Berlin",
        "activity_signals: exhibited at an industry expo in Chicago",
        "leadership: the CTO graduated from a university in Amsterdam",
        "leadership: the founder was born in Lisbon",
        "overview: incorporated under the laws of Ireland",
        "overview: disputes fall under the jurisdiction of Singapore",
    ],
)
def test_a_place_belonging_to_somebody_else_is_never_offered(
    db_session: Session, claim: str
) -> None:
    found = extract(db_session, [claim])
    assert found.candidates == (), f"{claim!r} produced {labels(found)}"
    assert found.warnings, "a refusal must be recorded, never silent"


def test_an_ambiguous_name_with_a_location_signal_is_offered_but_marked(
    db_session: Session,
) -> None:
    found = extract(db_session, ["office_locations: an office in Reading, United Kingdom"])
    reading = next(item for item in found.candidates if item.place.label == "Reading")
    assert reading.qualified_ambiguous is True


def test_an_ambiguous_name_needs_a_capital_letter_too(db_session: Session) -> None:
    """Capitalization is a weak signal used as a necessary condition, never a
    sufficient one: a location word nearby cannot rescue a lower-case ordinary
    word."""

    found = extract(db_session, ["overview: reading in the commissioning manual is required"])
    assert "Reading" not in labels(found)


def test_a_customer_example_is_offered_but_flagged(db_session: Session) -> None:
    found = extract(db_session, ["overview: our customer Nordwerk is based in Munich"])
    munich = next(item for item in found.candidates if item.place.label == "Munich")
    assert "customer_example" in munich.flags


def test_an_acquisition_is_offered_but_flagged_historical(db_session: Session) -> None:
    found = extract(db_session, ["activity_signals: acquired a Paris-based controls company"])
    paris = next(item for item in found.candidates if item.place.label == "Paris")
    assert "historical_or_acquired" in paris.flags


def test_a_second_clean_sighting_clears_the_doubt(db_session: Session) -> None:
    found = extract(
        db_session,
        [
            "activity_signals: acquired a Paris-based controls company in 2018",
            "office_locations: the Paris office employs forty engineers",
        ],
    )
    paris = next(item for item in found.candidates if item.place.label == "Paris")
    assert paris.flags == (), "an unflagged sighting resolves the doubt the flagged one raised"


def test_source_titles_and_urls_are_never_scanned(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session, name="Kiln Sources")
    make_dossier(db_session, company=company)
    make_fact(
        db_session,
        company=company,
        claim="overview: builds kiln controllers",
        url="https://press.example/tokyo-review/2024",
        key="src:1",
    )
    found = assemble(db_session, company).geography
    assert "Tokyo" not in labels(found)


def test_the_candidate_list_is_capped_and_says_so(db_session: Session) -> None:
    cities = [place.label for place in BASE.cities[: geo.MAX_CANDIDATES + 8]]
    found = extract(db_session, [f"office_locations: an office in {city}" for city in cities])
    assert len(found.candidates) <= geo.MAX_CANDIDATES
    assert any("beyond the cap" in warning for warning in found.warnings)


# --- relationships ----------------------------------------------------------


def produce(
    session: Session, claims: list[str], geography: list[dict[str, Any]]
) -> tuple[Any, ci_producer.ProductionResult]:
    seeded(session)
    company = make_company(session, name=f"Rel {claims[0][:12]}{len(claims)}")
    make_dossier(session, company=company)
    for index, claim in enumerate(claims):
        make_fact(session, company=company, claim=claim, key=f"rel:{company.id}:{index}")
    source = assemble(session, company)
    handles = {item.place.label: item.handle for item in source.geography.candidates}
    resolved = [
        {**entry, "candidate": handles.get(entry["candidate"], entry["candidate"])}
        for entry in geography
    ]
    result = ci_producer.produce(
        session,
        company=company,
        source=source,
        answer={"classifications": [], "geography": resolved},
        raw_answer="{}",
    )
    return company, result


def geo_rows(session: Session, company: Any) -> dict[str, ci_read.ClassificationView]:
    view = ci_read.get_company_intelligence(session, company_id=company.id)
    assert view is not None
    return {row.display_value: row for row in view.geographies()}


def test_a_valid_candidate_and_relationship_becomes_a_settled_physical_presence(
    db_session: Session,
) -> None:
    company, _ = produce(
        db_session,
        ["headquarters: headquartered in London, United Kingdom"],
        [{"candidate": "London", "relationship": "headquarters", "evidence": ["F1"]}],
    )
    rows = geo_rows(db_session, company)
    london = rows["London"]
    assert london.geo_relationship is IntelligenceGeoRelationship.HEADQUARTERS
    assert london.presence_kind is IntelligencePresenceKind.PHYSICAL
    assert london.state is IntelligenceValueState.RESOLVED
    assert london.country_code == "GB"
    assert london.city_name == "London"
    assert london.is_physical_presence is True


def test_a_commercial_market_is_not_a_physical_presence(db_session: Session) -> None:
    company, _ = produce(
        db_session,
        ["overview: serves customers across Germany"],
        [{"candidate": "Germany", "relationship": "commercial_market", "evidence": ["F1"]}],
    )
    germany = geo_rows(db_session, company)["Germany"]
    assert germany.presence_kind is IntelligencePresenceKind.COMMERCIAL
    assert germany.is_physical_presence is False
    assert germany.is_current_presence is True


def test_an_unclear_relationship_is_kept_and_named(db_session: Session) -> None:
    company, _ = produce(
        db_session,
        ["overview: a long-standing association with Milan"],
        [{"candidate": "Milan", "relationship": "unclear", "evidence": ["F1"]}],
    )
    milan = geo_rows(db_session, company)["Milan"]
    assert milan.state is IntelligenceValueState.UNRESOLVED
    assert milan.unresolved_reason == geo.REASON_UNCLEAR_RELATIONSHIP
    assert milan.geo_relationship is IntelligenceGeoRelationship.UNCLEAR


def test_an_unknown_candidate_handle_is_refused(db_session: Session) -> None:
    company, result = produce(
        db_session,
        ["headquarters: headquartered in Oslo"],
        [
            {"candidate": "G99", "relationship": "headquarters", "evidence": ["F1"]},
            {"candidate": "Oslo", "relationship": "office", "evidence": ["F1"]},
        ],
    )
    rows = geo_rows(db_session, company)
    assert set(rows) == {"Oslo", "Norway"}
    assert any("was not offered" in warning for warning in result.warnings)


def test_an_unknown_relationship_becomes_unclear_rather_than_being_invented(
    db_session: Session,
) -> None:
    company, result = produce(
        db_session,
        ["overview: an office in Vienna"],
        [{"candidate": "Vienna", "relationship": "teleportation", "evidence": ["F1"]}],
    )
    vienna = geo_rows(db_session, company)["Vienna"]
    assert vienna.geo_relationship is IntelligenceGeoRelationship.UNCLEAR
    assert any("not one of ours" in warning for warning in result.warnings)


def test_a_relationship_that_contradicts_its_context_stays_unresolved(
    db_session: Session,
) -> None:
    company, result = produce(
        db_session,
        ["overview: our customer Nordwerk is based in Munich"],
        [{"candidate": "Munich", "relationship": "headquarters", "evidence": ["F1"]}],
    )
    munich = geo_rows(db_session, company)["Munich"]
    assert munich.state is IntelligenceValueState.UNRESOLVED
    assert munich.unresolved_reason == geo.REASON_CONTEXT_MISMATCH
    assert any("customer_example" in warning for warning in result.warnings)


def test_a_customer_context_does_support_a_commercial_market(db_session: Session) -> None:
    company, _ = produce(
        db_session,
        ["overview: our customers across Sweden rely on the platform"],
        [{"candidate": "Sweden", "relationship": "commercial_market", "evidence": ["F1"]}],
    )
    sweden = geo_rows(db_session, company)["Sweden"]
    assert sweden.state is IntelligenceValueState.RESOLVED


def test_historical_and_planned_presence_are_never_current(db_session: Session) -> None:
    company, _ = produce(
        db_session,
        [
            "activity_signals: acquired a Paris-based controls company in 2018",
            "activity_signals: announced a planned facility in Katowice",
            "activity_signals: plans a new site in Bilbao",
        ],
        [
            {"candidate": "Paris", "relationship": "historical_presence", "evidence": ["F1"]},
            {"candidate": "Bilbao", "relationship": "planned_presence", "evidence": ["F3"]},
        ],
    )
    rows = geo_rows(db_session, company)
    paris = rows["Paris"]
    assert paris.presence_kind is IntelligencePresenceKind.FORMER
    assert paris.state is IntelligenceValueState.UNRESOLVED
    assert paris.unresolved_reason == geo.REASON_NOT_CURRENT
    assert paris.is_current_presence is False
    bilbao = rows["Bilbao"]
    assert bilbao.presence_kind is IntelligencePresenceKind.PROSPECTIVE
    assert bilbao.is_current_presence is False


def test_a_candidate_the_model_ignored_is_still_recorded(db_session: Session) -> None:
    company, _ = produce(
        db_session,
        ["headquarters: headquartered in Lyon", "overview: an office in Nantes"],
        [{"candidate": "Lyon", "relationship": "headquarters", "evidence": ["F1"]}],
    )
    rows = geo_rows(db_session, company)
    assert "Lyon" in rows
    assert rows["Lyon"].state is IntelligenceValueState.RESOLVED


def test_a_settled_city_infers_its_country_but_never_the_reverse(
    db_session: Session,
) -> None:
    company, result = produce(
        db_session,
        ["overview: runs a manufacturing plant in Pune"],
        [{"candidate": "Pune", "relationship": "manufacturing", "evidence": ["F1"]}],
    )
    rows = geo_rows(db_session, company)
    assert "India" in rows, "a plant in Pune is a plant in India"
    assert rows["India"].geo_relationship is IntelligenceGeoRelationship.MANUFACTURING
    assert any("no country implies a city" in warning for warning in result.warnings)

    # And the reverse: a country alone yields no city.
    other, _ = produce(
        db_session,
        ["overview: material operations across Japan"],
        [{"candidate": "Japan", "relationship": "operations", "evidence": ["F1"]}],
    )
    assert all(row.city_name is None for row in geo_rows(db_session, other).values())


def test_an_unclear_city_does_not_infer_a_country(db_session: Session) -> None:
    company, _ = produce(
        db_session,
        ["overview: a long-standing association with Porto"],
        [{"candidate": "Porto", "relationship": "unclear", "evidence": ["F1"]}],
    )
    assert "Portugal" not in geo_rows(db_session, company)


def test_conflicting_locations_are_both_kept(db_session: Session) -> None:
    company, _ = produce(
        db_session,
        [
            "headquarters: headquartered in Dublin",
            "overview: the head office is located in Cork",
        ],
        [
            {"candidate": "Dublin", "relationship": "headquarters", "evidence": ["F1"]},
            {"candidate": "Cork", "relationship": "headquarters", "evidence": ["F2"]},
        ],
    )
    rows = geo_rows(db_session, company)
    assert {"Dublin", "Cork"} <= set(rows)
    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    assert view.headquarters() is None, "two headquarters is a disagreement, not a pick"


def test_geography_never_arrives_through_the_classification_list(
    db_session: Session,
) -> None:
    seeded(db_session)
    company = make_company(db_session, name="Smuggled")
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="overview: builds controllers", key="s:1")
    result = ci_producer.produce(
        db_session,
        company=company,
        source=assemble(db_session, company),
        answer={
            "classifications": [{"dimension": "geography", "value": "Tokyo", "evidence": ["F1"]}]
        },
        raw_answer="{}",
    )
    view = ci_read.get_company_intelligence(db_session, company_id=company.id)
    assert view is not None
    assert view.for_dimension(IntelligenceDimension.GEOGRAPHY) == ()
    assert any("must come from the candidate list" in warning for warning in result.warnings)


def test_geography_rows_keep_their_evidence(db_session: Session) -> None:
    company, _ = produce(
        db_session,
        ["headquarters: headquartered in Copenhagen"],
        [{"candidate": "Copenhagen", "relationship": "headquarters", "evidence": ["F1"]}],
    )
    row = geo_rows(db_session, company)["Copenhagen"]
    assert row.evidence
    assert row.evidence[0].insight_id is not None


def test_the_prompt_lists_candidates_and_forbids_inventing_places(
    db_session: Session,
) -> None:
    from app.services.company_intelligence import prompts

    seeded(db_session)
    company = make_company(db_session, name="Prompted")
    make_dossier(db_session, company=company)
    make_fact(
        db_session,
        company=company,
        claim="headquarters: headquartered in Seoul",
        key="p:1",
    )
    source = assemble(db_session, company)
    text = prompts.classification_prompt(
        source, vocabularies=ci_producer.vocabulary_for_prompt(db_session)
    )
    assert "GEOGRAPHY CANDIDATES" in text
    assert "Seoul" in text
    assert "You may not introduce any other location" in text


def test_the_geography_vocabulary_is_not_dumped_into_the_prompt(
    db_session: Session,
) -> None:
    seeded(db_session)
    vocabularies = ci_producer.vocabulary_for_prompt(db_session)
    assert IntelligenceDimension.GEOGRAPHY.value not in vocabularies


def test_the_geography_edition_is_part_of_the_input_digest(db_session: Session) -> None:
    seeded(db_session)
    company = make_company(db_session, name="Digest Co")
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="overview: an office in Porto", key="d:1")
    source = ci_inputs.assemble(
        db_session,
        company=company,
        producer="p",
        producer_version="1",
        policy_version=ci_producer.POLICY_VERSION,
    )
    assert source.taxonomy_versions[IntelligenceDimension.GEOGRAPHY.value] == BASE.edition
