"""Field-level provenance and freshness tests (DAT-005).

Two layers: pure unit tests of the deterministic freshness policy, and
integration tests that drive the policy through the real staged importer and the
provenance service against PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.contact_field_value import ContactFieldValue
from app.services.campaigns import create_campaign
from app.services.imports.importer import BatchProvenance, run_import
from app.services.provenance.freshness import (
    FRESHNESS_POLICY_VERSION,
    TRACKED_FIELDS,
    Observation,
    resolve_winner,
    sort_key,
)
from app.services.provenance.service import (
    UnknownFieldError,
    explain_field,
    set_manual_override,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.usefixtures("enable_csv_import")

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _obs(
    key: str,
    value: str | None,
    *,
    observed_at: datetime | None,
    ingested_at: datetime,
    manual: bool = False,
) -> Observation:
    return Observation(
        key=key,
        value=value,
        observed_at=observed_at,
        ingested_at=ingested_at,
        is_manual_override=manual,
    )


# --------------------------------------------------------------------------- #
# Pure policy unit tests                                                       #
# --------------------------------------------------------------------------- #


def test_newer_observation_beats_older() -> None:
    old = _obs("a", "Manager", observed_at=_T0, ingested_at=_T0)
    new = _obs("b", "VP", observed_at=_T0 + timedelta(days=30), ingested_at=_T0)
    assert resolve_winner([old, new]).value == "VP"
    # Order of the input list must not matter.
    assert resolve_winner([new, old]).value == "VP"


def test_older_observation_cannot_displace_newer() -> None:
    new = _obs("b", "VP", observed_at=_T0 + timedelta(days=30), ingested_at=_T0)
    older = _obs(
        "c",
        "Analyst",
        observed_at=_T0 - timedelta(days=30),
        ingested_at=_T0 + timedelta(days=99),
    )
    # Even though the older observation was ingested much later, its earlier
    # observation time means it never wins over genuinely newer evidence.
    assert resolve_winner([new, older]).value == "VP"


def test_equal_timestamps_resolve_deterministically() -> None:
    a = _obs("a", "One", observed_at=_T0, ingested_at=_T0)
    b = _obs("b", "Two", observed_at=_T0, ingested_at=_T0 + timedelta(hours=1))
    # Equal observation time -> the later-ingested observation wins, stably.
    assert resolve_winner([a, b]).value == "Two"
    assert resolve_winner([b, a]).value == "Two"


def test_missing_timestamps_fall_back_to_ingestion() -> None:
    # Neither has a source observation time: the later-ingested one wins.
    early = _obs("a", "Old", observed_at=None, ingested_at=_T0)
    late = _obs("b", "New", observed_at=None, ingested_at=_T0 + timedelta(days=1))
    assert resolve_winner([early, late]).value == "New"
    # A known observation time outranks an ingestion fallback at the same instant.
    known = _obs("c", "Known", observed_at=_T0, ingested_at=_T0)
    fallback = _obs("d", "Fallback", observed_at=None, ingested_at=_T0)
    assert resolve_winner([known, fallback]).value == "Known"


def test_manual_override_outranks_all_imports() -> None:
    imported_new = _obs("a", "VP", observed_at=_T0 + timedelta(days=365), ingested_at=_T0)
    manual = _obs("m", "Chief", observed_at=_T0, ingested_at=_T0, manual=True)
    # Manual wins even though the import has a far newer observation time.
    assert resolve_winner([imported_new, manual]).value == "Chief"


def test_sort_key_is_total_order() -> None:
    obs = [
        _obs("a", "x", observed_at=None, ingested_at=_T0),
        _obs("b", "y", observed_at=_T0, ingested_at=_T0),
        _obs("c", "z", observed_at=_T0, ingested_at=_T0, manual=True),
    ]
    keys = [sort_key(o) for o in obs]
    assert len(set(keys)) == len(keys)  # every key distinct
    assert resolve_winner(obs).value == "z"


def test_resolve_winner_empty_is_none() -> None:
    assert resolve_winner([]) is None


# --------------------------------------------------------------------------- #
# Integration through the importer + service                                  #
# --------------------------------------------------------------------------- #

_HEADER = b"first_name,last_name,company_name,company_domain,email,title,exported_at\n"


def _csv(title: str, exported_at: str, *, tag: str = "") -> bytes:
    row = f"Dana,Lee,Acme Co,acme.example,dana@acme.example,{title},{exported_at}".encode()
    extra = f",{tag}".encode() if tag else b""
    header = _HEADER[:-1] + (b",tag\n" if tag else b"\n")
    return header + row + extra + b"\n"


def _import(db: Session, campaign: Campaign, content: bytes) -> None:
    run_import(
        db,
        campaign_id=campaign.id,
        content=content,
        filename="contacts.csv",
        provenance=BatchProvenance(source_name="src"),
    )


def _contact(db: Session) -> Contact:
    return db.scalars(select(Contact).where(Contact.email == "dana@acme.example")).one()


def _field_values(db: Session, contact_id, field: str) -> list[ContactFieldValue]:
    return list(
        db.scalars(
            select(ContactFieldValue).where(
                ContactFieldValue.contact_id == contact_id,
                ContactFieldValue.field_name == field,
            )
        ).all()
    )


def test_new_contact_seeds_field_provenance(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="C")
    _import(db_session, campaign, _csv("Manager", "2026-01-01"))
    contact = _contact(db_session)

    assert contact.title == "Manager"
    for field in TRACKED_FIELDS:
        values = _field_values(db_session, contact.id, field)
        assert len(values) == 1
        assert values[0].is_current_winner is True
        assert values[0].policy_version == FRESHNESS_POLICY_VERSION
        assert values[0].decision_reason == "only observation of this field"


def test_newer_import_updates_stale_field(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="C")
    _import(db_session, campaign, _csv("Manager", "2026-01-01"))
    contact = _contact(db_session)
    assert contact.title == "Manager"

    # A later re-export (newer observation time) promotes the person to VP.
    _import(db_session, campaign, _csv("VP", "2026-06-01", tag="reexport"))
    db_session.refresh(contact)
    assert contact.title == "VP"

    values = _field_values(db_session, contact.id, "title")
    assert len(values) == 2  # both observations preserved
    winner = next(v for v in values if v.is_current_winner)
    assert winner.value == "VP"
    assert sum(1 for v in values if v.is_current_winner) == 1
    view = explain_field(db_session, contact=contact, field_name="title")
    assert view.current_value == "VP"
    assert "recent" in (view.win_reason or "")
    assert {v.value for v in view.observations} == {"Manager", "VP"}


def test_older_import_does_not_overwrite_newer(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="C")
    _import(db_session, campaign, _csv("VP", "2026-06-01"))
    contact = _contact(db_session)
    assert contact.title == "VP"

    # A stale re-export (older observation time) must never win.
    _import(db_session, campaign, _csv("Analyst", "2026-01-01", tag="stale"))
    db_session.refresh(contact)
    assert contact.title == "VP"  # unchanged

    values = _field_values(db_session, contact.id, "title")
    assert {v.value for v in values} == {"VP", "Analyst"}  # stale evidence still stored
    loser = next(v for v in values if v.value == "Analyst")
    assert loser.is_current_winner is False
    assert "superseded" in (loser.decision_reason or "")


def test_conflicting_imports_preserve_both_and_explain(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="C")
    _import(db_session, campaign, _csv("Manager", "2026-03-01"))
    _import(db_session, campaign, _csv("Director", "2026-04-01", tag="x"))
    contact = _contact(db_session)

    view = explain_field(db_session, contact=contact, field_name="title")
    assert view.current_value == "Director"
    assert len(view.observations) == 2
    assert view.winner is not None and view.winner.value == "Director"


def test_manual_override_beats_import_and_sticks(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="C")
    _import(db_session, campaign, _csv("Manager", "2026-01-01"))
    contact = _contact(db_session)

    set_manual_override(
        db_session,
        contact=contact,
        field_name="title",
        value="Chief of Staff",
        actor="operator@vmr.example",
        reason="verified on LinkedIn",
    )
    db_session.refresh(contact)
    assert contact.title == "Chief of Staff"

    # A later import with a far newer observation time must NOT silently undo the
    # operator's explicit correction.
    _import(db_session, campaign, _csv("VP", "2027-01-01", tag="later"))
    db_session.refresh(contact)
    assert contact.title == "Chief of Staff"

    view = explain_field(db_session, contact=contact, field_name="title")
    assert view.winner is not None and view.winner.is_manual_override is True
    assert view.winner.created_by == "operator@vmr.example"
    assert "manual" in (view.win_reason or "")


def test_newer_manual_override_replaces_earlier_manual(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="C")
    _import(db_session, campaign, _csv("Manager", "2026-01-01"))
    contact = _contact(db_session)

    set_manual_override(
        db_session,
        contact=contact,
        field_name="title",
        value="First",
        actor="op",
        observed_at=_T0,
    )
    set_manual_override(
        db_session,
        contact=contact,
        field_name="title",
        value="Second",
        actor="op",
        observed_at=_T0 + timedelta(days=1),
    )
    db_session.refresh(contact)
    assert contact.title == "Second"


def test_manual_override_rejects_untracked_field(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="C")
    _import(db_session, campaign, _csv("Manager", "2026-01-01"))
    contact = _contact(db_session)
    with pytest.raises(UnknownFieldError):
        set_manual_override(
            db_session, contact=contact, field_name="email", value="x@y.z", actor="op"
        )


def test_repeated_identical_import_is_idempotent(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="C")
    content = _csv("Manager", "2026-01-01")
    _import(db_session, campaign, content)
    contact = _contact(db_session)
    before = _field_values(db_session, contact.id, "title")
    assert len(before) == 1

    # Exact same bytes -> idempotent short-circuit -> no new observations.
    _import(db_session, campaign, content)
    db_session.refresh(contact)
    after = _field_values(db_session, contact.id, "title")
    assert len(after) == 1
    assert contact.title == "Manager"
    assert sum(1 for v in after if v.is_current_winner) == 1


def test_winner_is_reproducible_from_stored_evidence(db_session: Session) -> None:
    campaign = create_campaign(db_session, name="C")
    _import(db_session, campaign, _csv("Manager", "2026-01-01"))
    _import(db_session, campaign, _csv("VP", "2026-06-01", tag="a"))
    _import(db_session, campaign, _csv("Analyst", "2026-02-01", tag="b"))
    contact = _contact(db_session)

    values = _field_values(db_session, contact.id, "title")
    observations = [
        Observation(
            key=str(v.id),
            value=v.value,
            observed_at=v.observed_at,
            ingested_at=v.ingested_at,
            is_manual_override=v.is_manual_override,
        )
        for v in values
    ]
    recomputed = resolve_winner(observations)
    stored_winner = next(v for v in values if v.is_current_winner)
    assert recomputed.key == str(stored_winner.id)
    assert stored_winner.value == "VP" == contact.title
