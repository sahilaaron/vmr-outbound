"""Geography persistence contract and worker durability (UAT defect regression).

A live run crashed persisting a Geography classification shaped exactly like:

    dimension=GEOGRAPHY, model_value=Philippines, term_code=ph,
    term_label=Philippines, normalization=UNMAPPED, state=RESOLVED,
    evidence_status=SUPPORTED, normalized_value=NULL,
    geo_relationship=OPERATIONS, presence_kind=PHYSICAL

which violates ``ck_company_intelligence_classifications_resolved_has_value``:
a RESOLVED row must carry the controlled term it resolved to (or belong to a
dimension with no controlled vocabulary). The geography branch of the producer
assumed the active vocabulary edition always carries every place the vendored
extraction base knows — untrue the moment the database holds an older edition
— and assigned RESOLVED without checking the term lookup.

These tests pin the corrected contract (an unmapped place persists UNRESOLVED
with ``unmapped_value``, CI-002 relationship/presence intact), prove the schema
contract itself still stands unweakened, and pin the worker's behaviour when a
flush-time contract violation does occur: the job fails durably, is never
silently returned to pending, is never re-claimed (no repeat model spend), and
the continuous worker moves on to later jobs.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.models.company_intelligence import (
    CompanyIntelligenceClassification,
    CompanyIntelligenceVersion,
)
from app.models.enums import (
    IntelligenceDimension,
    IntelligenceEvidenceStatus,
    IntelligenceGeoRelationship,
    IntelligenceJobStatus,
    IntelligenceNormalization,
    IntelligencePresenceKind,
    IntelligenceValueState,
)
from app.models.intelligence_taxonomy import IntelligenceTaxonomyTerm
from app.services.company_intelligence import jobs as ci_jobs
from app.services.company_intelligence import producer as ci_producer
from app.services.company_intelligence import runner as ci_runner
from app.services.company_intelligence import taxonomy as taxonomy_service
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.test_company_intelligence import (
    assemble,
    make_company,
    make_dossier,
    make_fact,
    seeded,
)
from tests.test_company_intelligence_jobs import ScriptedThinker, factory


def _produce_geography(
    session: Session,
    *,
    name: str,
    claims: list[str],
    geography: list[dict[str, Any]],
) -> tuple[Any, ci_producer.ProductionResult]:
    """Drive the real producer with a scripted geography answer."""

    company = make_company(session, name=name)
    make_dossier(session, company=company)
    for index, claim in enumerate(claims):
        make_fact(session, company=company, claim=claim, key=f"geo-uat:{company.id}:{index}")
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


def _geo_rows(session: Session, company: Any) -> list[CompanyIntelligenceClassification]:
    return list(
        session.scalars(
            select(CompanyIntelligenceClassification)
            .where(
                CompanyIntelligenceClassification.company_id == company.id,
                CompanyIntelligenceClassification.dimension == IntelligenceDimension.GEOGRAPHY,
            )
            .order_by(CompanyIntelligenceClassification.rank)
        ).all()
    )


def _drop_from_active_edition(session: Session, *, code: str) -> None:
    """Simulate vocabulary-edition drift: the active edition lacks this place.

    A live database keeps whatever edition an operator last published; renaming
    the term's code reproduces "the vendored base knows this place, the active
    edition does not" without fighting child/alias foreign keys.
    """

    edition = taxonomy_service.active_taxonomy(session, dimension=IntelligenceDimension.GEOGRAPHY)
    assert edition is not None
    term = session.scalars(
        select(IntelligenceTaxonomyTerm).where(
            IntelligenceTaxonomyTerm.taxonomy_id == edition.id,
            IntelligenceTaxonomyTerm.code == code,
        )
    ).one()
    term.code = f"{code}-absent-from-this-edition"
    session.flush()


# --- 1. resolved to a known term --------------------------------------------


def test_a_geography_resolved_to_a_known_term_persists_canonical(
    db_session: Session,
) -> None:
    seeded(db_session)
    company, result = _produce_geography(
        db_session,
        name="Known Term Co",
        claims=["headquarters: headquartered in London, United Kingdom"],
        geography=[{"candidate": "London", "relationship": "headquarters", "evidence": ["F1"]}],
    )
    rows = _geo_rows(db_session, company)
    row = next(item for item in rows if item.term_code == "gb-london")
    assert row.state is IntelligenceValueState.RESOLVED
    assert row.normalization is IntelligenceNormalization.CANONICAL
    assert row.term_id is not None
    assert row.term_code == "gb-london"
    assert row.geo_relationship is IntelligenceGeoRelationship.HEADQUARTERS
    assert row.presence_kind is IntelligencePresenceKind.PHYSICAL
    db_session.flush()  # the schema contract accepts the row


# --- 2 + 4. the exact Philippines shape, corrected ---------------------------


def test_the_philippines_row_shape_persists_unresolved_instead_of_crashing(
    db_session: Session,
) -> None:
    """The UAT crash, reproduced: PH absent from the active edition.

    The run must now complete, and the row must persist internally valid:
    UNRESOLVED + UNMAPPED + no term id, with the CI-002 relationship and
    presence fields intact — never RESOLVED with nothing behind it.
    """

    seeded(db_session)
    _drop_from_active_edition(db_session, code="ph")

    company, result = _produce_geography(
        db_session,
        name="Manila Ops Co",
        claims=["office_locations: operations teams based in the Philippines"],
        geography=[{"candidate": "Philippines", "relationship": "operations", "evidence": ["F1"]}],
    )
    (row,) = _geo_rows(db_session, company)

    # The incident row's committed shape, corrected where it was contradictory.
    assert row.dimension is IntelligenceDimension.GEOGRAPHY
    assert row.term_code == "ph"
    assert row.term_label == "Philippines"
    assert row.normalization is IntelligenceNormalization.UNMAPPED
    assert row.normalized_value is None
    assert row.term_id is None
    assert row.state is IntelligenceValueState.UNRESOLVED  # was RESOLVED: the defect
    assert row.unresolved_reason == ci_producer.REASON_UNMAPPED
    assert row.evidence_status is IntelligenceEvidenceStatus.SUPPORTED
    # CI-002 fields survive intact on the unresolved row.
    assert row.geo_relationship is IntelligenceGeoRelationship.OPERATIONS
    assert row.presence_kind is IntelligencePresenceKind.PHYSICAL

    # The database accepts it — this exact flush is what crashed in UAT.
    db_session.flush()

    # The gap is named where an operator will read it, with the remedy.
    assert any("active" in w and "geography" in w for w in result.warnings)
    assert result.unresolved >= 1


# --- 3. the schema contract stands unweakened --------------------------------


def test_a_resolved_classification_can_never_persist_without_its_term(
    db_session: Session,
) -> None:
    seeded(db_session)
    company, _ = _produce_geography(
        db_session,
        name="Contract Stand Co",
        claims=["headquarters: headquartered in London, United Kingdom"],
        geography=[{"candidate": "London", "relationship": "headquarters", "evidence": ["F1"]}],
    )
    version = db_session.scalars(
        select(CompanyIntelligenceVersion).where(
            CompanyIntelligenceVersion.company_id == company.id
        )
    ).one()
    bad = CompanyIntelligenceClassification(
        intelligence_version_id=version.id,
        company_id=company.id,
        dimension=IntelligenceDimension.GEOGRAPHY,
        rank=7,
        model_value="Philippines",
        term_code="ph",
        term_label="Philippines",
        term_id=None,
        normalization=IntelligenceNormalization.UNMAPPED,
        state=IntelligenceValueState.RESOLVED,
        evidence_status=IntelligenceEvidenceStatus.SUPPORTED,
        evidence_count=1,
        geo_relationship=IntelligenceGeoRelationship.OPERATIONS,
        presence_kind=IntelligencePresenceKind.PHYSICAL,
    )
    with pytest.raises(IntegrityError) as excinfo:
        with db_session.begin_nested():
            db_session.add(bad)
            db_session.flush()
    assert "resolved_has_value" in str(excinfo.value)


# --- 5 + 6 + 7. worker durability on an integrity failure ---------------------


def _integrity_raising_producer(session: Session, company: Any) -> Any:
    """A stand-in for ``produce_for_company`` that hits a real DB contract.

    It inserts a deliberately invalid classification and flushes, so the
    resulting :class:`IntegrityError` comes from PostgreSQL through the real
    savepoint machinery — not from a hand-raised exception.
    """

    def _run(inner_session: Session, **_kwargs: Any) -> ci_runner.RunOutcome:
        from app.models.company_dossier import CompanyDossierVersion

        dossier = inner_session.scalars(
            select(CompanyDossierVersion).where(CompanyDossierVersion.company_id == company.id)
        ).one()
        version = CompanyIntelligenceVersion(
            company_id=company.id,
            version_number=999,
            dossier_version_id=dossier.id,
            dossier_version_number=dossier.version_number,
            sourced_fact_ids=[],
            sourced_fact_count=0,
            taxonomy_versions={},
            producer="test",
            producer_version="test/1",
            policy_version="test",
            input_digest="deadbeef",
            dimensions_addressed=[],
        )
        inner_session.add(version)
        inner_session.flush()
        inner_session.add(
            CompanyIntelligenceClassification(
                intelligence_version_id=version.id,
                company_id=company.id,
                dimension=IntelligenceDimension.GEOGRAPHY,
                rank=0,
                model_value="Philippines",
                term_id=None,
                normalization=IntelligenceNormalization.UNMAPPED,
                state=IntelligenceValueState.RESOLVED,
                evidence_status=IntelligenceEvidenceStatus.SUPPORTED,
                evidence_count=1,
                geo_relationship=IntelligenceGeoRelationship.OPERATIONS,
                presence_kind=IntelligencePresenceKind.PHYSICAL,
            )
        )
        inner_session.flush()
        raise AssertionError("the flush above must violate resolved_has_value")

    return _run


def test_an_integrity_failure_fails_the_job_durably_without_losing_it(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(db_session)
    company = make_company(db_session, name="Integrity Fail Co")
    make_dossier(db_session, company=company)
    make_fact(db_session, company=company, claim="overview: builds kilns", key="if:1")
    job, _created = ci_jobs.enqueue(db_session, company=company)
    claimed = ci_jobs.claim_next(db_session, worker_id="uat-worker", lease_seconds=300)
    assert claimed is not None and claimed.id == job.id

    monkeypatch.setattr(
        ci_runner, "produce_for_company", _integrity_raising_producer(db_session, company)
    )
    outcome = ci_runner.execute_job(db_session, job=claimed, worker_id="uat-worker")

    # A durable, truthful failure — not a crash, not a silent return to pending.
    assert outcome.succeeded is False
    assert outcome.code == ci_runner.PERSISTENCE_INTEGRITY_CODE
    assert job.status is IntelligenceJobStatus.FAILED
    assert job.error_class == ci_runner.PERSISTENCE_INTEGRITY_CODE
    assert job.lease_owner is None
    assert job.attempts == 1
    assert job.error is not None
    assert job.error["detail"]["constraint"] == (
        "ck_company_intelligence_classifications_resolved_has_value"
    )
    # The message never carries the statement or its parameters.
    assert "INSERT" not in job.error["message"]

    # Nothing half-written survived the savepoint rollback.
    assert (
        db_session.scalars(
            select(CompanyIntelligenceVersion).where(
                CompanyIntelligenceVersion.company_id == company.id
            )
        ).first()
        is None
    )

    # Not retryable, so it is never silently re-claimed: no repeat model spend
    # on a defect that would fail identically.
    assert ci_jobs.claim_next(db_session, worker_id="uat-worker", lease_seconds=300) is None


def test_the_continuous_worker_moves_on_to_later_jobs_after_an_integrity_failure(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(db_session)
    monkeypatch.setenv("FEATURES__COMPANY_INTELLIGENCE", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        broken = make_company(db_session, name="Broken First Co")
        make_dossier(db_session, company=broken)
        make_fact(db_session, company=broken, claim="overview: builds kilns", key="cw:1")
        healthy = make_company(db_session, name="Healthy Second Co")
        make_dossier(db_session, company=healthy)
        make_fact(
            db_session,
            company=healthy,
            claim="headquarters: headquartered in London, United Kingdom",
            key="cw:2",
        )
        first, _ = ci_jobs.enqueue(db_session, company=broken, priority=200)
        second, _ = ci_jobs.enqueue(db_session, company=healthy, priority=100)

        real_produce = ci_runner.produce_for_company
        crash_once = _integrity_raising_producer(db_session, broken)

        def selective(session: Session, *, company: Any, **kwargs: Any) -> ci_runner.RunOutcome:
            if company.id == broken.id:
                return crash_once(session, company=company, **kwargs)
            return real_produce(session, company=company, **kwargs)

        monkeypatch.setattr(ci_runner, "produce_for_company", selective)
        thinker = ScriptedThinker(
            {
                "classifications": [],
                "geography": [
                    {"candidate": "London", "relationship": "headquarters", "evidence": ["F1"]}
                ],
            }
        )

        first_outcome = ci_runner.run_next(
            db_session, worker_id="cw", thinker_factory=factory(thinker)
        )
        assert first_outcome is not None
        assert first_outcome.code == ci_runner.PERSISTENCE_INTEGRITY_CODE
        assert first.status is IntelligenceJobStatus.FAILED

        second_outcome = ci_runner.run_next(
            db_session, worker_id="cw", thinker_factory=factory(thinker)
        )
        assert second_outcome is not None
        assert second_outcome.succeeded is True
        assert second.status is IntelligenceJobStatus.SUCCEEDED
        # One model call total: the crashing job never reached the model (its
        # stand-in produced rows directly), and the failed job is not re-run.
        assert len(thinker.requests) == 1
        assert ci_runner.run_next(db_session, worker_id="cw") is None
    finally:
        get_settings.cache_clear()


# --- 8. versioning and idempotency stay intact --------------------------------


def test_versioning_and_idempotency_survive_an_unmapped_geography(
    db_session: Session,
) -> None:
    seeded(db_session)
    _drop_from_active_edition(db_session, code="ph")
    company, first = _produce_geography(
        db_session,
        name="Version Seq Co",
        claims=["office_locations: operations teams based in the Philippines"],
        geography=[{"candidate": "Philippines", "relationship": "operations", "evidence": ["F1"]}],
    )
    assert first.created is True
    assert first.version.version_number == 1

    # The identical evidence produces no second version: the digest short-circuit
    # is unchanged by the geography correction.
    source = assemble(db_session, company)
    handles = {item.place.label: item.handle for item in source.geography.candidates}
    again = ci_producer.produce(
        db_session,
        company=company,
        source=source,
        answer={
            "classifications": [],
            "geography": [
                {
                    "candidate": handles["Philippines"],
                    "relationship": "operations",
                    "evidence": ["F1"],
                }
            ],
        },
        raw_answer="{}",
    )
    assert again.created is False
    assert again.version.id == first.version.id
