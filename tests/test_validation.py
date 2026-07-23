"""Row-validation unit tests (DAT-002)."""

from __future__ import annotations

from app.services.imports.validation import validate_row


def _base_row(**overrides: str) -> dict[str, str]:
    row = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "company_name": "Analytical Engines",
        "company_domain": "analyticalengines.example",
        "email": "ada@analyticalengines.example",
    }
    row.update(overrides)
    return row


def test_valid_row_normalizes_and_has_no_errors() -> None:
    result = validate_row(1, _base_row(first_name="  ADA ", email="ADA@AnalyticalEngines.Example"))
    assert result.is_valid
    assert result.normalized["first_name"] == "ADA"
    assert result.normalized["email"] == "ada@analyticalengines.example"
    assert result.natural_key == "ada|lovelace|analyticalengines.example"


def test_headers_matched_case_insensitively() -> None:
    row = {
        "First_Name": "Ada",
        "LAST_NAME": "Lovelace",
        "Company_Name": "Analytical Engines",
        "company_domain": "analyticalengines.example",
    }
    result = validate_row(1, row)
    assert result.is_valid
    assert result.normalized["first_name"] == "Ada"


def test_missing_required_fields_are_reported_not_dropped() -> None:
    result = validate_row(
        7, {"first_name": "", "last_name": "", "company_name": "", "company_domain": ""}
    )
    assert not result.is_valid
    codes = {(e.column, e.code) for e in result.errors}
    assert ("first_name", "missing_required") in codes
    assert ("company_domain", "missing_required") in codes


def test_invalid_domain_is_reported_with_actionable_message() -> None:
    result = validate_row(42, _base_row(company_domain="not a domain!!"))
    assert not result.is_valid
    err = next(e for e in result.errors if e.column == "company_domain")
    assert err.code == "invalid_domain"
    assert "row 42" in err.message and "not a valid hostname" in err.message


def test_invalid_email_is_reported() -> None:
    result = validate_row(3, _base_row(email="charles[at]diffeng.example"))
    assert not result.is_valid
    assert any(e.code == "invalid_email" for e in result.errors)


def test_missing_optional_email_is_allowed() -> None:
    result = validate_row(1, _base_row(email=""))
    assert result.is_valid
    assert result.normalized["email"] is None
