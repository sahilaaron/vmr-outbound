"""Column-mapping tests: suggestion, validation, application."""

from __future__ import annotations

from app.services.imports import mapping


def test_suggest_mapping_exact_and_alias_names() -> None:
    header = ["First Name", "Surname", "Company", "Website", "Email Address", "Notes"]
    suggestion = mapping.suggest_mapping(header)
    assert suggestion == {
        "First Name": "first_name",
        "Surname": "last_name",
        "Company": "company_name",
        "Website": "company_domain",
        "Email Address": "email",
    }
    assert "Notes" not in suggestion  # no confident correspondence -> operator decides


def test_suggest_mapping_never_double_targets_a_field() -> None:
    suggestion = mapping.suggest_mapping(["email", "Work Email"])
    assert list(suggestion.values()).count("email") == 1


def test_check_mapping_flags_duplicate_targets() -> None:
    header = ["a", "b"]
    check = mapping.check_mapping({"a": "email", "b": "email"}, header)
    codes = {p.code for p in check.problems}
    assert "duplicate_target" in codes


def test_check_mapping_flags_missing_required_and_unknowns() -> None:
    check = mapping.check_mapping({"ghost": "first_name", "x": "not_a_field"}, ["x"])
    codes = {p.code for p in check.problems}
    assert {"unknown_column", "unknown_field", "missing_required"} <= codes
    assert not check.is_valid


def test_check_mapping_accepts_complete_mapping() -> None:
    header = ["fn", "ln", "co", "web"]
    check = mapping.check_mapping(
        {"fn": "first_name", "ln": "last_name", "co": "company_name", "web": "company_domain"},
        header,
    )
    assert check.is_valid


def test_apply_mapping_returns_mapped_view_without_touching_raw() -> None:
    raw = {"First Name": "Ada", "Notes": "keep me"}
    mapped = mapping.apply_mapping(raw, {"First Name": "first_name"})
    assert mapped == {"first_name": "Ada"}
    assert raw == {"First Name": "Ada", "Notes": "keep me"}  # verbatim row untouched
