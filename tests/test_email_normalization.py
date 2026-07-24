"""EML-001 / EML-002 / EML-004: name normalization, pattern generation, ranking.

Covers the edge cases that occur in imported B2B data: diacritics, apostrophes,
hyphens, compound/particle surnames, middle names, non-Latin scripts, and empty
input, plus deterministic, duplicate-free pattern generation and evidence-based
reordering that never marks an address valid.
"""

from __future__ import annotations

from app.models.enums import EmailCandidateSource
from app.services.email.candidates import rank_candidates
from app.services.email.normalization import ENGINE_VERSION, build_identity
from app.services.email.patterns import generate_local_parts


def test_diacritics_folded_to_ascii() -> None:
    idn = build_identity("José", "Peña")
    assert idn.first == "jose"
    assert idn.last == "pena"
    assert idn.renderable is True


def test_apostrophe_and_hyphen_handled() -> None:
    idn = build_identity("Mary Anne", "O'Brien-Smith")
    assert idn.first == "mary"
    assert idn.middle_initials == ("a",)
    # Hyphen and apostrophe collapse into a single surname token.
    assert idn.last == "obriensmith"


def test_particle_surname_offers_variants() -> None:
    idn = build_identity("Bjorn", "van der Berg")
    # Full joined form is primary; the final significant token is also offered.
    assert idn.last == "vanderberg"
    assert "berg" in idn.last_variants


def test_nordic_letters_mapped() -> None:
    idn = build_identity("Bjørn", "Ødegård")
    assert idn.first == "bjorn"
    assert idn.last == "odegard"


def test_non_latin_name_is_unrenderable() -> None:
    idn = build_identity("Аня", "Иванова")
    assert idn.renderable is False
    assert idn.reason is not None


def test_empty_name_unrenderable() -> None:
    idn = build_identity("", "")
    assert idn.renderable is False


def test_missing_first_name_warns_but_renders() -> None:
    idn = build_identity(None, "Smith")
    assert idn.renderable is True
    assert idn.first == ""
    assert any("first name" in w for w in idn.warnings)


def test_pattern_generation_is_deterministic_and_deduplicated() -> None:
    idn = build_identity("Al", "Al")  # first==last collapses some patterns
    parts = generate_local_parts(idn)
    locals_ = [p.local_part for p in parts]
    assert len(locals_) == len(set(locals_)), "no duplicate local parts"
    # Deterministic across calls.
    assert [p.local_part for p in generate_local_parts(idn)] == locals_


def test_engine_version_is_recorded() -> None:
    assert ENGINE_VERSION.startswith("eml-")


def test_ranking_prefers_common_pattern_first() -> None:
    ranked = rank_candidates(
        imported_email=None,
        identity_first="Jane",
        identity_last="Doe",
        domain="acme.com",
    )
    assert ranked[0].email == "jane.doe@acme.com"
    assert ranked[0].source == EmailCandidateSource.GENERATED


def test_imported_email_ranks_first() -> None:
    ranked = rank_candidates(
        imported_email="Jane.Doe@Acme.com",
        identity_first="Jane",
        identity_last="Doe",
        domain="acme.com",
    )
    assert ranked[0].source == EmailCandidateSource.IMPORTED
    assert ranked[0].email == "jane.doe@acme.com"  # normalized lower-case


def test_fresh_domain_pattern_evidence_reorders_without_validating() -> None:
    # Give strong evidence to the {f}{last} pattern; it should climb above the
    # default-first {first}.{last}, but nothing here marks any address valid.
    ranked = rank_candidates(
        imported_email=None,
        identity_first="Jane",
        identity_last="Doe",
        domain="acme.com",
        pattern_confidence={"{f}{last}": 1.0},
    )
    assert ranked[0].email == "jdoe@acme.com"
    assert "reorder only" in ranked[0].rank_reason


def test_no_domain_yields_no_candidates() -> None:
    ranked = rank_candidates(
        imported_email=None, identity_first="Jane", identity_last="Doe", domain=None
    )
    assert ranked == []
