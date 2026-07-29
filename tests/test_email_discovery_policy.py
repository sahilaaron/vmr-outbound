"""Focused tests for the versioned Issue #224 Email discovery policy."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.services.email.discovery_policy import (
    EmailDiscoveryPolicyDecision,
    EmailPolicyOutcome,
    EmployeeCountClass,
    EmployeeCountEvidence,
    EmployeeEvidenceFreshness,
    classify_employee_count,
    evaluate,
    evaluate_existing_accepted_email_reuse,
)

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def evidence(
    raw_value: str | None,
    *,
    observed_at: datetime | None = NOW,
    marked_stale: bool = False,
) -> EmployeeCountEvidence:
    return EmployeeCountEvidence(
        evidence_id="11111111-1111-1111-1111-111111111111",
        raw_value=raw_value,
        source_reference="company-source:employee-count",
        observed_at=observed_at,
        ingested_at=NOW,
        source_policy_version="freshness-v1",
        source_marked_stale=marked_stale,
    )


def decision(
    raw_value: str | None,
    *,
    first_name: str | None = "Ada",
    last_name: str | None = "Lovelace",
    domain: str | None = "analytical.example",
    observed_at: datetime | None = NOW,
) -> EmailDiscoveryPolicyDecision:
    return evaluate(
        first_name=first_name,
        last_name=last_name,
        domain=domain,
        employee_evidence=evidence(raw_value, observed_at=observed_at),
        now=NOW,
    )


def test_employee_count_51_is_more_than_50() -> None:
    assert classify_employee_count("51") is EmployeeCountClass.MORE_THAN_50


def test_employee_count_50_is_50_or_fewer() -> None:
    assert classify_employee_count("50") is EmployeeCountClass.FIFTY_OR_FEWER


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("more than 50 employees", EmployeeCountClass.MORE_THAN_50),
        ("51-200", EmployeeCountClass.MORE_THAN_50),
        ("51+", EmployeeCountClass.MORE_THAN_50),
        ("50 or fewer employees", EmployeeCountClass.FIFTY_OR_FEWER),
        ("50 or less", EmployeeCountClass.FIFTY_OR_FEWER),
        ("1-50", EmployeeCountClass.FIFTY_OR_FEWER),
        ("50+", EmployeeCountClass.UNKNOWN),
        ("1-200", EmployeeCountClass.UNKNOWN),
        ("small", EmployeeCountClass.UNKNOWN),
    ],
)
def test_employee_count_classification_never_guesses_across_threshold(
    raw_value: str,
    expected: EmployeeCountClass,
) -> None:
    assert classify_employee_count(raw_value) is expected


def test_larger_company_uses_locked_order() -> None:
    result = decision("51")
    assert result.outcome is EmailPolicyOutcome.READY
    assert result.ordered_formats == (
        "firstname.lastname",
        "finitiallastname",
        "lastnamefinitial",
    )
    assert [candidate.email for candidate in result.candidates] == [
        "ada.lovelace@analytical.example",
        "alovelace@analytical.example",
        "lovelacea@analytical.example",
    ]


def test_smaller_company_uses_locked_order() -> None:
    result = decision("50")
    assert result.outcome is EmailPolicyOutcome.READY
    assert result.ordered_formats == (
        "firstname",
        "firstname.lastname",
        "finitiallastname",
    )
    assert [candidate.email for candidate in result.candidates] == [
        "ada@analytical.example",
        "ada.lovelace@analytical.example",
        "alovelace@analytical.example",
    ]


@pytest.mark.parametrize("raw_value", [None, "", "unknown", "1-200", "50+"])
def test_unknown_employee_count_is_explicit(raw_value: str | None) -> None:
    result = decision(raw_value)
    assert result.outcome is EmailPolicyOutcome.EMPLOYEE_COUNT_UNKNOWN
    assert result.employee_count_class is EmployeeCountClass.UNKNOWN
    assert result.candidates == ()
    assert result.ordered_formats == ()


def test_stale_employee_count_evidence_is_blocked() -> None:
    result = evaluate(
        first_name="Ada",
        last_name="Lovelace",
        domain="analytical.example",
        employee_evidence=evidence("51", marked_stale=True),
        now=NOW,
    )
    assert result.outcome is EmailPolicyOutcome.EMPLOYEE_COUNT_STALE
    assert result.evidence_freshness is EmployeeEvidenceFreshness.STALE
    assert result.candidates == ()


def test_current_winner_is_not_expired_by_an_email_specific_age_rule() -> None:
    old_observation = datetime(2020, 1, 1, tzinfo=UTC)
    result = evaluate(
        first_name="Ada",
        last_name="Lovelace",
        domain="analytical.example",
        employee_evidence=evidence("51", observed_at=old_observation),
        now=NOW,
    )
    assert result.outcome is EmailPolicyOutcome.READY
    assert result.evidence_freshness is EmployeeEvidenceFreshness.FRESH


def test_candidate_normalization_is_deterministic() -> None:
    first = evaluate(
        first_name="  José  ",
        last_name="O’Brien",
        domain="example.com",
        employee_evidence=evidence("51"),
        now=NOW,
    )
    second = evaluate(
        first_name="  José  ",
        last_name="O’Brien",
        domain="example.com",
        employee_evidence=evidence("51"),
        now=NOW,
    )
    assert first == second
    assert [candidate.email for candidate in first.candidates] == [
        "jose.obrien@example.com",
        "jobrien@example.com",
        "obrienj@example.com",
    ]


def test_equivalent_candidates_are_deduplicated_preserving_first_occurrence() -> None:
    result = evaluate(
        first_name="Ada",
        last_name="A",
        domain="example.com",
        employee_evidence=evidence("51"),
        now=NOW,
    )
    assert result.ordered_formats == ("firstname.lastname", "finitiallastname")
    assert [candidate.email for candidate in result.candidates] == [
        "ada.a@example.com",
        "aa@example.com",
    ]


def test_policy_produces_exactly_three_candidates_maximum() -> None:
    assert len(decision("51").candidates) == 3
    assert len(decision("50").candidates) == 3


@pytest.mark.parametrize("first_name", [None, "", "李"])
def test_unusable_first_name_is_blocked(first_name: str | None) -> None:
    result = decision("51", first_name=first_name)
    assert result.outcome is EmailPolicyOutcome.UNUSABLE_FIRST_NAME
    assert result.candidates == ()


@pytest.mark.parametrize("last_name", [None, "", "王"])
def test_unusable_last_name_is_blocked(last_name: str | None) -> None:
    result = decision("51", last_name=last_name)
    assert result.outcome is EmailPolicyOutcome.UNUSABLE_LAST_NAME
    assert result.candidates == ()


@pytest.mark.parametrize(
    "domain",
    [None, "", "Example.COM", "https://example.com", "not a domain"],
)
def test_ineligible_or_noncanonical_domain_is_blocked(domain: str | None) -> None:
    result = decision("51", domain=domain)
    assert result.outcome is EmailPolicyOutcome.DOMAIN_INELIGIBLE
    assert result.candidates == ()


def test_existing_email_reuse_records_classification_without_candidate_formats() -> None:
    result = evaluate_existing_accepted_email_reuse(
        domain="analytical.example",
        employee_evidence=evidence("51"),
        now=NOW,
    )
    assert result.outcome is EmailPolicyOutcome.EXISTING_ACCEPTED_EMAIL_REUSE
    assert result.employee_count_class is EmployeeCountClass.MORE_THAN_50
    assert result.ordered_formats == ()
    assert result.candidates == ()
