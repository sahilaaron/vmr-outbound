"""Normalization unit tests (DAT-003)."""

from __future__ import annotations

import pytest
from app.services.imports import normalization as norm


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Ada  ", "Ada"),
        ("van  der   Berg", "van der Berg"),
        ("McDonald", "McDonald"),  # case is preserved, never title-cased
        ("O'Brien", "O'Brien"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_name(raw: str | None, expected: str | None) -> None:
    assert norm.normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ada@Example.COM", "ada@example.com"),
        ("  grace@corp.example ", "grace@corp.example"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_email(raw: str | None, expected: str | None) -> None:
    assert norm.normalize_email(raw) == expected


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("ada@analyticalengines.example", True),
        ("charles[at]diffeng.example", False),
        ("no-at-sign.example", False),
        ("missing@domain", False),
    ],
)
def test_is_valid_email(value: str, valid: bool) -> None:
    assert norm.is_valid_email(value) is valid


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.Acme.com/about", "acme.com"),
        ("WWW.ACME.COM", "acme.com"),
        ("acme.com", "acme.com"),
        ("acme.com:8080", "acme.com"),
        ("user@acme.com", "acme.com"),
        ("acme.com.", "acme.com"),
        ("", None),
    ],
)
def test_normalize_domain(raw: str, expected: str | None) -> None:
    assert norm.normalize_domain(raw) == expected


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("acme.com", True),
        ("sub.acme.co.uk", True),
        ("acme", False),  # no dot
        ("not a domain!!", False),
        ("-bad.com", False),
    ],
)
def test_is_valid_hostname(value: str, valid: bool) -> None:
    assert norm.is_valid_hostname(value) is valid


def test_normalize_linkedin_url_adds_scheme_and_lowers_host() -> None:
    assert (
        norm.normalize_linkedin_url("LinkedIn.com/in/Ada-Lovelace/")
        == "https://linkedin.com/in/Ada-Lovelace"
    )


def test_normalize_country_upper_cases_iso_codes_only() -> None:
    assert norm.normalize_country("us") == "US"
    assert norm.normalize_country("  United States ") == "United States"


def test_build_natural_key_is_case_insensitive_on_name() -> None:
    assert norm.build_natural_key("Ada", "Lovelace", "acme.com") == norm.build_natural_key(
        "ADA", "lovelace", "acme.com"
    )
    assert norm.build_natural_key("Ada", "Lovelace", "acme.com") == "ada|lovelace|acme.com"
