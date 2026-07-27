"""Shared evidence and insight model tests (INS-001)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

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
