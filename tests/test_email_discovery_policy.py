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


def test_the_locked_order_and_the_addresses_it_produces() -> None:
    """The three formats, in order, and exactly the addresses they render.

    Replaces a pair of tests that asserted a different order per company size.
    ``lastnamefinitial`` is no longer tried at all — three is the ceiling and these
    three are the ones worth spending a verification credit on.
    """

    result = decision("51")
    assert result.outcome is EmailPolicyOutcome.READY
    assert result.ordered_formats == (
        "firstname.lastname",
        "firstname",
        "finitiallastname",
    )
    assert [candidate.email for candidate in result.candidates] == [
        "ada.lovelace@analytical.example",
        "ada@analytical.example",
        "alovelace@analytical.example",
    ]
    # Same plan for a small company; size does not enter into it.
    assert decision("50").ordered_formats == result.ordered_formats


@pytest.mark.parametrize("raw_value", [None, "", "unknown", "1-200", "50+"])
def test_an_unknown_employee_count_falls_back_instead_of_refusing(
    raw_value: str | None,
) -> None:
    """Not knowing a company's size is not a reason to refuse to look.

    This used to return zero candidates, which meant a Contact at a company whose
    headcount nobody had sourced could never have an address discovered or
    verified at all. Since size is only sourced by company research, and research
    is optional, the ordinary case silently produced nothing — and downstream that
    read as "no address could be found" rather than as a policy refusal.

    Size only ever chose the ORDER of three formats; it never chose how many. So
    the honest behaviour is to use a default order and record the classification
    as unknown, which is what the attempt row now shows.
    """

    result = decision(raw_value)
    assert result.outcome is EmailPolicyOutcome.READY
    assert result.employee_count_class is EmployeeCountClass.UNKNOWN
    assert result.ordered_formats == (
        "firstname.lastname",
        "firstname",
        "finitiallastname",
    )
    assert len(result.candidates) == 3


def test_stale_employee_count_evidence_falls_back_and_is_recorded_as_unknown() -> None:
    """A stale count is a real observation the policy declines to act on.

    It does not block, and it no longer has to be flattened to "unknown" either.
    Now that the classification steers nothing, recording what the evidence said
    alongside the fact that it is stale is strictly more informative than
    discarding it — the attempt row keeps both.
    """

    result = evaluate(
        first_name="Ada",
        last_name="Lovelace",
        domain="analytical.example",
        employee_evidence=evidence("51", marked_stale=True),
        now=NOW,
    )
    assert result.outcome is EmailPolicyOutcome.READY
    assert result.evidence_freshness is EmployeeEvidenceFreshness.STALE
    assert result.employee_count_class is EmployeeCountClass.MORE_THAN_50
    assert len(result.candidates) == 3


def test_every_contact_gets_the_same_three_formats_in_the_same_order() -> None:
    """One order for everyone, whatever is or is not known about the company.

    Size used to choose between two orders. Because headcount is only sourced by
    optional company research, the ordinary Contact got the fallback order anyway —
    so the branch bought inconsistency rather than accuracy.
    """

    for raw in ("12", "5000", None, "unknown", "1-200"):
        result = decision(raw)
        assert result.outcome is EmailPolicyOutcome.READY
        assert result.ordered_formats == (
            "firstname.lastname",
            "firstname",
            "finitiallastname",
        ), f"employee count {raw!r} must not change the plan"


def test_the_size_classification_is_still_recorded_even_though_it_steers_nothing() -> None:
    """What was known about a company when an attempt was made is worth keeping.

    That it no longer influences the plan is not a reason to stop recording it.
    """

    small = decision("12")
    assert small.employee_count_class is EmployeeCountClass.FIFTY_OR_FEWER
    large = decision("5000")
    assert large.employee_count_class is EmployeeCountClass.MORE_THAN_50


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
        "jose@example.com",
        "jobrien@example.com",
    ]


def test_equivalent_candidates_are_deduplicated_preserving_first_occurrence() -> None:
    result = evaluate(
        first_name="Ada",
        last_name="A",
        domain="example.com",
        employee_evidence=evidence("51"),
        now=NOW,
    )
    # "firstname" and "finitiallastname" both render "ada" / "aa" distinctly here,
    # so nothing collapses; the surname of a single letter is what makes the last
    # two differ only by a dot.
    assert result.ordered_formats == ("firstname.lastname", "firstname", "finitiallastname")
    assert [candidate.email for candidate in result.candidates] == [
        "ada.a@example.com",
        "ada@example.com",
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
