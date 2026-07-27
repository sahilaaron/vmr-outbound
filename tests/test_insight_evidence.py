"""Shared evidence and insight model tests (INS-001)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models.company import Company
from app.models.contact import Contact
from app.models.enums import InsightKind, InsightState, InsightSubject
from app.models.insight import Insight, InsightEvidence
from app.services.insights import evidence as insight_service
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _company(session: Session, *, domain: str = "acme.example") -> Company:
    company = Company(name="Acme", domain=domain)
    session.add(company)
    session.flush()
    return company


def _contact(session: Session, *, domain: str = "acme.example") -> Contact:
    contact = Contact(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Acme",
        company_domain=domain,
        natural_key=f"ada|lovelace|{domain}",
    )
    session.add(contact)
    session.flush()
    return contact


def _evidence(**overrides: object) -> insight_service.EvidenceInput:
    values: dict[str, object] = {
        "source_url": "https://acme.example/news/expansion",
        "retrieved_at": datetime.now(UTC),
        "evidence_summary": "The company announcement names the new Pune office.",
        "confidence": 0.9,
        "extraction_method": "website-parser-v1",
    }
    values.update(overrides)
    return insight_service.EvidenceInput(**values)  # type: ignore[arg-type]


def test_company_claim_and_source_observation_are_stored_separately(
    db_session: Session,
) -> None:
    company = _company(db_session)
    source_record_id = uuid.uuid4()

    insight = insight_service.create_insight(
        db_session,
        company_id=company.id,
        claim="Acme announced a Pune office.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[
            _evidence(
                source_record_type="company_research_submission",
                source_record_id=source_record_id,
            )
        ],
        actor="operator:sahil",
    )

    stored = db_session.scalar(
        select(InsightEvidence).where(InsightEvidence.insight_id == insight.id)
    )
    assert stored is not None
    assert insight.subject is InsightSubject.COMPANY
    assert insight.company_id == company.id
    assert insight.contact_id is None
    assert insight.source_url is None
    assert stored.source_url == "https://acme.example/news/expansion"
    assert stored.source_record_id == source_record_id
    assert insight_service.is_personalization_eligible(db_session, insight=insight)


def test_contact_insight_is_reusable_and_campaign_free(db_session: Session) -> None:
    contact = _contact(db_session)
    insight = insight_service.create_insight(
        db_session,
        contact_id=contact.id,
        claim="Ada leads procurement.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[_evidence()],
    )

    assert insight.subject is InsightSubject.CONTACT
    assert insight_service.list_for_contact(db_session, contact_id=contact.id) == [insight]


def test_unknown_is_explicit_and_does_not_need_a_fabricated_source(
    db_session: Session,
) -> None:
    company = _company(db_session)
    insight = insight_service.create_insight(
        db_session,
        company_id=company.id,
        claim="The company's current research budget is unknown.",
        kind=InsightKind.FACT,
        state=InsightState.UNKNOWN,
        evidence=[],
    )

    assert not insight_service.is_personalization_eligible(db_session, insight=insight)


def test_supported_claim_requires_evidence(db_session: Session) -> None:
    company = _company(db_session)
    with pytest.raises(insight_service.InsightError, match="require evidence"):
        insight_service.create_insight(
            db_session,
            company_id=company.id,
            claim="Acme is hiring.",
            kind=InsightKind.FACT,
            state=InsightState.SUPPORTED,
            evidence=[],
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"source_url": "javascript:alert(1)"}, "absolute http"),
        ({"confidence": 1.1}, "between 0 and 1"),
        ({"retrieved_at": datetime.now()}, "timezone"),
        ({"evidence_summary": "  "}, "must not be blank"),
        ({"extraction_method": ""}, "must not be blank"),
        ({"source_record_type": "dossier"}, "supplied together"),
    ],
)
def test_malformed_evidence_is_rejected(
    db_session: Session,
    overrides: dict[str, object],
    message: str,
) -> None:
    company = _company(db_session)
    with pytest.raises(insight_service.InsightError, match=message):
        insight_service.create_insight(
            db_session,
            company_id=company.id,
            claim="Acme is hiring.",
            kind=InsightKind.FACT,
            state=InsightState.SUPPORTED,
            evidence=[_evidence(**overrides)],
        )


def test_exactly_one_subject_is_required_by_service(db_session: Session) -> None:
    company = _company(db_session)
    contact = _contact(db_session)
    with pytest.raises(insight_service.InsightError, match="exactly one"):
        insight_service.create_insight(
            db_session,
            company_id=company.id,
            contact_id=contact.id,
            claim="Ambiguous owner.",
            kind=InsightKind.INTERPRETATION,
            state=InsightState.CONFLICTING,
            evidence=[_evidence()],
        )


def test_subject_ownership_is_also_enforced_by_database(db_session: Session) -> None:
    company = _company(db_session)
    contact = _contact(db_session)
    db_session.add(
        Insight(
            subject=InsightSubject.COMPANY,
            company_id=company.id,
            contact_id=contact.id,
            claim="Invalid dual owner.",
            kind=InsightKind.FACT,
            state=InsightState.SUPPORTED,
            version=1,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_source_less_legacy_claim_cannot_qualify_for_personalization(
    db_session: Session,
) -> None:
    contact = _contact(db_session)
    insight = Insight(
        subject=InsightSubject.CONTACT,
        contact_id=contact.id,
        claim="Uncited legacy claim.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        version=1,
    )
    db_session.add(insight)
    db_session.flush()

    assert not insight_service.is_personalization_eligible(db_session, insight=insight)


def test_retry_with_same_idempotency_key_reuses_the_insight(db_session: Session) -> None:
    company = _company(db_session)
    retrieved_at = datetime.now(UTC)
    kwargs = {
        "company_id": company.id,
        "claim": "Acme announced a Pune office.",
        "kind": InsightKind.FACT,
        "state": InsightState.SUPPORTED,
        "evidence": [_evidence(retrieved_at=retrieved_at)],
        "idempotency_key": "research-job-17:claim-1",
    }

    first = insight_service.create_insight(db_session, **kwargs)  # type: ignore[arg-type]
    retried = insight_service.create_insight(db_session, **kwargs)  # type: ignore[arg-type]

    assert retried.id == first.id
    assert len(insight_service.list_for_company(db_session, company_id=company.id)) == 1


def test_idempotency_key_rejects_changed_content(db_session: Session) -> None:
    company = _company(db_session)
    insight_service.create_insight(
        db_session,
        company_id=company.id,
        claim="Acme announced a Pune office.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[_evidence()],
        idempotency_key="research-job-17:claim-1",
    )

    with pytest.raises(insight_service.InsightError, match="different content"):
        insight_service.create_insight(
            db_session,
            company_id=company.id,
            claim="Acme announced a Mumbai office.",
            kind=InsightKind.FACT,
            state=InsightState.SUPPORTED,
            evidence=[_evidence()],
            idempotency_key="research-job-17:claim-1",
        )


def test_retry_after_refetching_the_source_returns_the_original_insight(
    db_session: Session,
) -> None:
    """A real retry re-reads its sources, so retrieval timestamps move.

    Same subject, claim, kind, state, version and sources — only the clock
    differs. That is the case an idempotency key exists to absorb, so it must
    return the original insight rather than report a collision.
    """

    company = _company(db_session)
    key = "research-job-17:claim-1"
    first = insight_service.create_insight(
        db_session,
        company_id=company.id,
        claim="Acme announced a Pune office.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[_evidence(retrieved_at=datetime.now(UTC) - timedelta(minutes=5))],
        idempotency_key=key,
    )

    retried = insight_service.create_insight(
        db_session,
        company_id=company.id,
        claim="Acme announced a Pune office.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[
            _evidence(
                retrieved_at=datetime.now(UTC),
                excerpt="A different excerpt from the same page.",
                freshness_at=datetime.now(UTC),
            )
        ],
        idempotency_key=key,
    )

    assert retried.id == first.id
    assert len(insight_service.list_for_company(db_session, company_id=company.id)) == 1
    stored = db_session.scalars(
        select(InsightEvidence).where(InsightEvidence.insight_id == first.id)
    ).all()
    assert len(stored) == 1, "the retry must not append a second copy of the same source"


def test_retry_identity_ignores_the_order_evidence_arrives_in(db_session: Session) -> None:
    """Two sources supplied in either order describe the same claim."""

    company = _company(db_session)
    key = "research-job-17:claim-2"
    first_source = _evidence(source_url="https://acme.example/a")
    second_source = _evidence(source_url="https://acme.example/b")

    first = insight_service.create_insight(
        db_session,
        company_id=company.id,
        claim="Acme opened two offices.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[first_source, second_source],
        idempotency_key=key,
    )
    reordered = insight_service.create_insight(
        db_session,
        company_id=company.id,
        claim="Acme opened two offices.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[second_source, first_source],
        idempotency_key=key,
    )

    assert reordered.id == first.id


def test_retry_identity_still_rejects_a_changed_source_set(db_session: Session) -> None:
    """Dropping retrieval metadata from the digest must not weaken the guard."""

    company = _company(db_session)
    key = "research-job-17:claim-3"
    insight_service.create_insight(
        db_session,
        company_id=company.id,
        claim="Acme announced a Pune office.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[_evidence(source_url="https://acme.example/original")],
        idempotency_key=key,
    )

    with pytest.raises(insight_service.InsightError, match="different content"):
        insight_service.create_insight(
            db_session,
            company_id=company.id,
            claim="Acme announced a Pune office.",
            kind=InsightKind.FACT,
            state=InsightState.SUPPORTED,
            evidence=[_evidence(source_url="https://acme.example/substituted")],
            idempotency_key=key,
        )


def test_retry_identity_still_rejects_a_changed_evidence_version(db_session: Session) -> None:
    """The same URL re-observed as a new evidence version is a new source."""

    company = _company(db_session)
    key = "research-job-17:claim-4"
    insight_service.create_insight(
        db_session,
        company_id=company.id,
        claim="Acme announced a Pune office.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[_evidence(version=1)],
        idempotency_key=key,
    )

    with pytest.raises(insight_service.InsightError, match="different content"):
        insight_service.create_insight(
            db_session,
            company_id=company.id,
            claim="Acme announced a Pune office.",
            kind=InsightKind.FACT,
            state=InsightState.SUPPORTED,
            evidence=[_evidence(version=2)],
            idempotency_key=key,
        )


def test_duplicate_source_within_one_packet_is_rejected(db_session: Session) -> None:
    """The same URL at the same version twice would violate the unique index."""

    company = _company(db_session)
    with pytest.raises(insight_service.InsightError, match="repeats source"):
        insight_service.create_insight(
            db_session,
            company_id=company.id,
            claim="Acme announced a Pune office.",
            kind=InsightKind.FACT,
            state=InsightState.SUPPORTED,
            evidence=[_evidence(), _evidence(evidence_summary="A second reading.")],
        )


def test_the_same_source_at_different_versions_is_accepted(db_session: Session) -> None:
    """Re-observing one page as a new version is legitimate, not a duplicate."""

    company = _company(db_session)
    insight = insight_service.create_insight(
        db_session,
        company_id=company.id,
        claim="Acme announced a Pune office.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[_evidence(version=1), _evidence(version=2)],
    )

    stored = db_session.scalars(
        select(InsightEvidence).where(InsightEvidence.insight_id == insight.id)
    ).all()
    assert {item.version for item in stored} == {1, 2}


def test_oversized_source_url_is_rejected(db_session: Session) -> None:
    """Longer than the column, so the database would truncate-error instead."""

    company = _company(db_session)
    long_url = "https://acme.example/" + ("a" * insight_service.SOURCE_URL_MAX_LENGTH)
    with pytest.raises(insight_service.InsightError, match="at most"):
        insight_service.create_insight(
            db_session,
            company_id=company.id,
            claim="Acme announced a Pune office.",
            kind=InsightKind.FACT,
            state=InsightState.SUPPORTED,
            evidence=[_evidence(source_url=long_url)],
        )


@pytest.mark.parametrize(
    ("bad_evidence", "message"),
    [
        pytest.param(
            [_evidence(), _evidence(evidence_summary="A second reading.")],
            "repeats source",
            id="duplicate-source",
        ),
        pytest.param(
            [_evidence(source_url="https://acme.example/" + ("a" * 1024))],
            "at most",
            id="oversized-url",
        ),
    ],
)
def test_a_rejected_packet_leaves_the_transaction_usable(
    db_session: Session,
    bad_evidence: list[insight_service.EvidenceInput],
    message: str,
) -> None:
    """Validation happens before any write, so nothing aborts the transaction.

    A driver-level IntegrityError or DataError would poison the session and
    force the caller to roll back work unrelated to the bad packet.
    """

    company = _company(db_session)

    with pytest.raises(insight_service.InsightError, match=message):
        insight_service.create_insight(
            db_session,
            company_id=company.id,
            claim="Acme announced a Pune office.",
            kind=InsightKind.FACT,
            state=InsightState.SUPPORTED,
            evidence=bad_evidence,
        )

    # The same session must still be able to read and write.
    assert db_session.scalar(select(Company).where(Company.id == company.id)) is not None
    recovered = insight_service.create_insight(
        db_session,
        company_id=company.id,
        claim="Acme announced a Pune office.",
        kind=InsightKind.FACT,
        state=InsightState.SUPPORTED,
        evidence=[_evidence()],
    )
    assert recovered.id is not None
    assert insight_service.is_personalization_eligible(db_session, insight=recovered)
