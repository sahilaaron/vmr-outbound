"""DAT-010: Sales Navigator company-domain enrichment via logo.dev.

Covers the service (grouping, one-lookup-per-company, refresh, explicit
confirmation, overlay/propagation, truthful outcomes, provenance, secret safety)
and the operator web flow (stage -> map -> enrich -> preview -> confirm) end to
end, proving nothing is auto-accepted, unresolved companies stay rejected, the
raw capture is never mutated, and the API key never leaks.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.contact import Contact
from app.models.enums import (
    EnrichmentConfirmationSource,
    EnrichmentConfirmationStatus,
    EnrichmentLookupStatus,
    ImportBatchStatus,
    ImportSourceFormat,
)
from app.models.import_batch import ImportBatch, ImportRow
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.services.campaigns import create_campaign
from app.services.enrichment import companies, logodev
from app.services.imports.importer import process_pending_batch
from app.services.imports.preview import preview_pending_batch
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

SECRET = "sk_live_DAT010_TOP_SECRET"

# Sales Navigator capture rows (camelCase, no company_domain — the real shape).
_ROWS = [
    {"firstName": "Dana", "lastName": "Ng", "companyName": "Acme Inc", "title": "VP"},
    {"firstName": "Ravi", "lastName": "Iyer", "companyName": "Acme Inc", "title": "Eng"},
    {"firstName": "Mia", "lastName": "Cole", "companyName": "acme  inc", "title": "Ops"},  # same co
    {"firstName": "Sam", "lastName": "Roe", "companyName": "Globex", "title": "Sales"},
    {"firstName": "Lee", "lastName": "Fox", "companyName": "Globex", "title": "Ops"},
    {"firstName": "No", "lastName": "Company", "companyName": "", "title": "None"},  # ungroupable
]

_MAPPING = {
    "firstName": "first_name",
    "lastName": "last_name",
    "companyName": "company_name",
    "title": "title",
}


def _make_batch(db: Session, campaign_id: uuid.UUID, rows: list[dict] = _ROWS) -> ImportBatch:
    batch = ImportBatch(
        campaign_id=campaign_id,
        client_batch_id=str(uuid.uuid4()),
        content_hash=uuid.uuid4().hex,
        status=ImportBatchStatus.PENDING,
        source_format=ImportSourceFormat.SALES_NAVIGATOR,
        column_mapping=dict(_MAPPING),
        total_rows=len(rows),
    )
    db.add(batch)
    db.flush()
    for i, raw in enumerate(rows, start=1):
        db.add(ImportRow(batch_id=batch.id, row_number=i, sheet_index=0, raw_data=raw))
    db.flush()
    return batch


def _stub(mapping: dict[str, list[dict]], counter: list[str] | None = None) -> logodev.Transport:
    """A transport returning canned candidates per query (records each query)."""

    import json
    from urllib.parse import parse_qs, urlsplit

    def _call(url: str, headers: dict, timeout: float) -> logodev.RawResponse:  # type: ignore[type-arg]
        assert SECRET not in url  # key never in the URL
        q = parse_qs(urlsplit(url).query)["q"][0]
        if counter is not None:
            counter.append(q)
        return logodev.RawResponse(200, json.dumps(mapping.get(q, [])))

    return _call


def _rows(db: Session, batch: ImportBatch) -> list[ImportRow]:
    return list(db.scalars(select(ImportRow).where(ImportRow.batch_id == batch.id)).all())


# --- Grouping ----------------------------------------------------------------


def test_group_companies_collapses_case_and_whitespace(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="grp")
    db_session.flush()
    batch = _make_batch(db_session, campaign.id)
    groups = companies.group_companies(_rows(db_session, batch), _MAPPING)
    keys = {g.key: g.row_count for g in groups}
    # "Acme Inc" (x2) + "acme  inc" collapse to one company with three rows.
    assert keys == {"acme inc": 3, "globex": 2}


# --- One lookup per company / idempotency / refresh --------------------------


def test_one_lookup_per_unique_company_and_repeat_is_idempotent(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="lk")
    db_session.flush()
    batch = _make_batch(db_session, campaign.id)
    calls: list[str] = []
    transport = _stub({"Acme Inc": [{"domain": "acme.com"}]}, counter=calls)

    def run() -> None:
        companies.run_pending_lookups(
            db_session,
            batch=batch,
            rows=_rows(db_session, batch),
            column_mapping=_MAPPING,
            api_key=SECRET,
            search_url="https://stub/search",
            timeout=1.0,
            max_candidates=10,
            actor="t",
            transport=transport,
        )

    run()
    # Two unique companies -> exactly two queries (the ungroupable row is skipped).
    assert sorted(calls) == ["Acme Inc", "Globex"]
    run()  # a second pass calls nothing new (one lookup per company)
    assert sorted(calls) == ["Acme Inc", "Globex"]

    records = {
        r.company_key: r for r in db_session.scalars(select(SalesNavCompanyEnrichment)).all()
    }
    assert records["acme inc"].lookup_status is EnrichmentLookupStatus.OK
    assert records["globex"].lookup_status is EnrichmentLookupStatus.NO_MATCH
    assert records["acme inc"].lookup_attempts == 1


def test_refresh_re_looks_up_one_company(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="rf")
    db_session.flush()
    batch = _make_batch(db_session, campaign.id)
    calls: list[str] = []
    transport = _stub({"Acme Inc": [{"domain": "acme.com"}]}, counter=calls)
    recs = companies.ensure_records(
        db_session, batch=batch, rows=_rows(db_session, batch), column_mapping=_MAPPING
    )
    acme = next(r for r in recs if r.company_key == "acme inc")
    companies.run_lookup(
        db_session,
        record=acme,
        api_key=SECRET,
        search_url="s",
        timeout=1.0,
        max_candidates=10,
        actor="t",
        transport=transport,
    )
    companies.run_lookup(
        db_session,
        record=acme,
        api_key=SECRET,
        search_url="s",
        timeout=1.0,
        max_candidates=10,
        actor="t",
        transport=transport,
        force=True,
    )
    assert calls == ["Acme Inc", "Acme Inc"]
    assert acme.lookup_attempts == 2


def test_lookup_without_key_raises(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="nok")
    db_session.flush()
    batch = _make_batch(db_session, campaign.id)
    with pytest.raises(companies.ApiKeyMissing):
        companies.run_pending_lookups(
            db_session,
            batch=batch,
            rows=_rows(db_session, batch),
            column_mapping=_MAPPING,
            api_key="",
            search_url="s",
            timeout=1.0,
            max_candidates=10,
            actor="t",
        )


# --- Explicit confirmation (never auto-accept) -------------------------------


def _lookup_all(db: Session, batch: ImportBatch, mapping: dict[str, list[dict]]) -> None:
    companies.run_pending_lookups(
        db,
        batch=batch,
        rows=_rows(db, batch),
        column_mapping=_MAPPING,
        api_key=SECRET,
        search_url="s",
        timeout=1.0,
        max_candidates=10,
        actor="t",
        transport=_stub(mapping),
    )


def test_confirm_candidate_manual_and_unresolved(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="cf")
    db_session.flush()
    batch = _make_batch(db_session, campaign.id)
    _lookup_all(db_session, batch, {"Acme Inc": [{"domain": "acme.com"}, {"domain": "acme.io"}]})

    # Candidate: only a domain the operator names (and that is a candidate) applies.
    rec = companies.confirm_company(
        db_session,
        batch=batch,
        company_key_value="acme inc",
        source=EnrichmentConfirmationSource.CANDIDATE,
        domain="acme.io",
        actor="op",
    )
    assert rec.confirmation_status is EnrichmentConfirmationStatus.CONFIRMED
    assert rec.confirmed_domain == "acme.io"
    assert rec.confirmation_source is EnrichmentConfirmationSource.CANDIDATE

    # A domain that is not among the candidates cannot be accepted via the candidate path.
    with pytest.raises(companies.EnrichmentError):
        companies.confirm_company(
            db_session,
            batch=batch,
            company_key_value="acme inc",
            source=EnrichmentConfirmationSource.CANDIDATE,
            domain="evil.com",
            actor="op",
        )

    # Manual override accepts any valid hostname (URLs/www normalized).
    rec2 = companies.confirm_company(
        db_session,
        batch=batch,
        company_key_value="globex",
        source=EnrichmentConfirmationSource.MANUAL,
        domain="https://www.globex.com/x",
        actor="op",
    )
    assert rec2.confirmed_domain == "globex.com"
    assert rec2.confirmation_source is EnrichmentConfirmationSource.MANUAL

    # An invalid manual domain is rejected, changing nothing.
    with pytest.raises(companies.EnrichmentError):
        companies.confirm_company(
            db_session,
            batch=batch,
            company_key_value="globex",
            source=EnrichmentConfirmationSource.MANUAL,
            domain="not a domain",
            actor="op",
        )

    # Unresolved is an explicit decision that stores no domain.
    rec3 = companies.confirm_company(
        db_session,
        batch=batch,
        company_key_value="acme inc",
        source=EnrichmentConfirmationSource.UNRESOLVED,
        domain=None,
        actor="op",
    )
    assert rec3.confirmation_status is EnrichmentConfirmationStatus.UNRESOLVED
    assert rec3.confirmed_domain is None


# --- Overlay: propagation, truthful outcomes, immutable raw -------------------


def test_confirmation_propagates_to_all_rows_and_unresolved_stays_rejected(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    get_settings.cache_clear()
    try:
        campaign = create_campaign(db_session, name="ov")
        db_session.flush()
        batch = _make_batch(db_session, campaign.id)
        _lookup_all(db_session, batch, {"Acme Inc": [{"domain": "acme.com"}]})
        # Confirm Acme only; leave Globex unresolved (its rows must stay rejected).
        companies.confirm_company(
            db_session,
            batch=batch,
            company_key_value="acme inc",
            source=EnrichmentConfirmationSource.CANDIDATE,
            domain="acme.com",
            actor="op",
        )
        companies.confirm_company(
            db_session,
            batch=batch,
            company_key_value="globex",
            source=EnrichmentConfirmationSource.UNRESOLVED,
            domain=None,
            actor="op",
        )
        overlay = companies.domain_overlay(db_session, batch.id)
        assert overlay == {"acme inc": "acme.com"}

        # Non-committing preview predicts the committed outcome exactly.
        preview = preview_pending_batch(
            db_session,
            rows=_rows(db_session, batch),
            column_mapping=_MAPPING,
            domain_overlay=overlay,
        )
        assert preview.accepted == 3  # all three Acme rows
        assert preview.rejected == 3  # two Globex + one empty-company row
        assert db_session.scalar(select(func.count(Contact.id))) == 0  # still no writes

        summary = process_pending_batch(
            db_session, batch=batch, column_mapping=_MAPPING, domain_overlay=overlay
        )
        assert summary.accepted_rows == 3
        assert summary.rejected_rows == 3
        assert summary.contacts_created == 3
        # Every accepted contact carries the confirmed domain, propagated from one decision.
        domains = {c.company_domain for c in db_session.scalars(select(Contact)).all()}
        assert domains == {"acme.com"}

        # The immutable raw rows were never mutated — no company_domain was written in.
        for row in _rows(db_session, batch):
            assert "company_domain" not in row.raw_data
    finally:
        get_settings.cache_clear()


def test_previous_confirmation_survives_a_later_api_failure(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="surv")
    db_session.flush()
    batch = _make_batch(db_session, campaign.id)
    _lookup_all(db_session, batch, {"Acme Inc": [{"domain": "acme.com"}]})
    companies.confirm_company(
        db_session,
        batch=batch,
        company_key_value="acme inc",
        source=EnrichmentConfirmationSource.CANDIDATE,
        domain="acme.com",
        actor="op",
    )

    # A later failing lookup for another company must not disturb the staged batch,
    # its rows, or the earlier confirmation.
    def _fail(url: str, headers: dict, timeout: float) -> logodev.RawResponse:  # type: ignore[type-arg]
        raise logodev.TransportError("down")

    recs = companies.ensure_records(
        db_session, batch=batch, rows=_rows(db_session, batch), column_mapping=_MAPPING
    )
    globex = next(r for r in recs if r.company_key == "globex")
    companies.run_lookup(
        db_session,
        record=globex,
        api_key=SECRET,
        search_url="s",
        timeout=1.0,
        max_candidates=10,
        actor="t",
        transport=_fail,
        force=True,
    )
    assert globex.lookup_status is EnrichmentLookupStatus.API_UNAVAILABLE
    acme = next(r for r in recs if r.company_key == "acme inc")
    assert acme.confirmation_status is EnrichmentConfirmationStatus.CONFIRMED
    assert acme.confirmed_domain == "acme.com"
    # Staged batch and rows are intact; nothing was imported.
    assert db_session.get(ImportBatch, batch.id).status is ImportBatchStatus.PENDING
    assert len(_rows(db_session, batch)) == len(_ROWS)


# --- Provenance / audit / secret safety --------------------------------------


def test_lookup_and_confirmation_are_audited_without_the_key(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="aud")
    db_session.flush()
    batch = _make_batch(db_session, campaign.id)
    _lookup_all(db_session, batch, {"Acme Inc": [{"domain": "acme.com"}]})
    companies.confirm_company(
        db_session,
        batch=batch,
        company_key_value="acme inc",
        source=EnrichmentConfirmationSource.CANDIDATE,
        domain="acme.com",
        actor="op",
    )
    actions = {e.action for e in db_session.scalars(select(AuditEvent)).all()}
    assert "import.salesnav_domain_lookup" in actions
    assert "import.salesnav_domain_confirmed" in actions
    # No audit row, candidate blob, or enrichment record contains the API key.
    for e in db_session.scalars(select(AuditEvent)).all():
        assert SECRET not in str(e.context or {})
    for rec in db_session.scalars(select(SalesNavCompanyEnrichment)).all():
        assert SECRET not in str(rec.candidates or [])


# --- Web flow (end-to-end) ---------------------------------------------------


@pytest.fixture()
def enrich_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, dict]]:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    monkeypatch.setenv("FEATURES__SALESNAV_DOMAIN_ENRICHMENT", "true")
    monkeypatch.setenv("LOGO_DEV_API_KEY", SECRET)
    get_settings.cache_clear()

    # The web routes call logo.dev via the default (urllib) transport; stub the
    # client function so the flow runs without a network and never leaks the key.
    canned = {"Acme Inc": [{"name": "Acme", "domain": "acme.com"}]}

    def _fake_search(
        query: str,
        *,
        api_key: str,
        search_url: str,
        timeout: float,
        max_candidates: int,
        transport=None,
    ):  # type: ignore[no-untyped-def]
        assert api_key == SECRET
        body = canned.get(query)
        if not body:
            return logodev.LookupResult(EnrichmentLookupStatus.NO_MATCH)
        return logodev.LookupResult(
            EnrichmentLookupStatus.OK,
            tuple(logodev.Candidate(domain=c["domain"], name=c.get("name")) for c in body),
        )

    monkeypatch.setattr(companies.logodev, "search_brands", _fake_search)

    app = create_app()

    def _ov() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _ov
    try:
        with TestClient(app) as client:
            yield client, canned
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_enrich_page_404_when_feature_disabled(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__SALESNAV_DOMAIN_ENRICHMENT", "false")
    get_settings.cache_clear()
    campaign = create_campaign(db_session, name="off")
    db_session.flush()
    batch = _make_batch(db_session, campaign.id)
    app = create_app()

    def _ov() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _ov
    with TestClient(app) as c:
        assert c.get(f"/imports/{batch.id}/enrich").status_code == 404
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_web_full_flow_no_contacts_before_confirm(
    enrich_client: tuple[TestClient, dict], db_session: Session
) -> None:
    client, _ = enrich_client
    campaign = create_campaign(db_session, name="webflow")
    db_session.flush()
    batch = _make_batch(db_session, campaign.id)
    bid = str(batch.id)

    # Enrich page renders.
    assert client.get(f"/imports/{bid}/enrich").status_code == 200
    # Look up all companies (one per unique).
    assert client.post(f"/imports/{bid}/enrich/lookup", follow_redirects=False).status_code == 303
    # Confirm Acme by candidate; leave Globex unresolved.
    r1 = client.post(
        f"/imports/{bid}/enrich/confirm",
        data={"company_key": "acme inc", "action": "candidate", "candidate_domain": "acme.com"},
        follow_redirects=False,
    )
    assert r1.status_code == 303
    r2 = client.post(
        f"/imports/{bid}/enrich/confirm",
        data={"company_key": "globex", "action": "unresolved"},
        follow_redirects=False,
    )
    assert r2.status_code == 303

    # Preview is non-committing and reflects the confirmed domain.
    prev = client.get(f"/imports/{bid}/preview")
    assert prev.status_code == 200
    assert db_session.scalar(select(func.count(Contact.id))) == 0
    assert db_session.get(ImportBatch, batch.id).status is ImportBatchStatus.PENDING

    # Explicit import confirm -> truthful outcomes.
    conf = client.post(f"/imports/{bid}/confirm", follow_redirects=False)
    assert conf.status_code == 303
    db_session.expire_all()
    done = db_session.get(ImportBatch, batch.id)
    assert done.status is ImportBatchStatus.COMPLETED
    assert done.accepted_rows == 3  # three Acme rows resolved
    assert done.rejected_rows == 3  # two Globex (unresolved) + one empty company
    assert done.contacts_created == 3


def test_web_lookup_without_key_reports_not_configured(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    monkeypatch.setenv("FEATURES__SALESNAV_DOMAIN_ENRICHMENT", "true")
    monkeypatch.delenv("LOGO_DEV_API_KEY", raising=False)
    get_settings.cache_clear()
    campaign = create_campaign(db_session, name="nokey")
    db_session.flush()
    batch = _make_batch(db_session, campaign.id)
    app = create_app()

    def _ov() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _ov
    try:
        with TestClient(app) as c:
            resp = c.post(f"/imports/{batch.id}/enrich/lookup", follow_redirects=False)
            assert resp.status_code == 303
            assert (
                "not+configured" in resp.headers["location"]
                or "LOGO_DEV_API_KEY" in (resp.headers["location"])
            )
        # No lookup ran: the company stays NOT_STARTED.
        recs = db_session.scalars(select(SalesNavCompanyEnrichment)).all()
        assert all(r.lookup_status is EnrichmentLookupStatus.NOT_STARTED for r in recs)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_secret_never_appears_in_rendered_enrich_html(
    enrich_client: tuple[TestClient, dict], db_session: Session
) -> None:
    client, _ = enrich_client
    campaign = create_campaign(db_session, name="nohtml")
    db_session.flush()
    batch = _make_batch(db_session, campaign.id)
    bid = str(batch.id)
    client.post(f"/imports/{bid}/enrich/lookup", follow_redirects=False)
    html = client.get(f"/imports/{bid}/enrich").text
    assert SECRET not in html
