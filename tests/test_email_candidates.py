"""EML-005 / EML-006: candidate persistence, selection, and review routing."""

from __future__ import annotations

import uuid

from app.models.contact import Contact
from app.models.email_candidate import EmailCandidate
from app.models.enums import EmailCandidateSource
from app.services.email.candidates import generate_candidates, get_selected_candidate
from sqlalchemy.orm import Session


def _contact(session: Session, **kw: object) -> Contact:
    defaults = dict(
        first_name="Jane",
        last_name="Doe",
        company_name="Acme",
        company_domain="acme.com",
        email=None,
        natural_key=f"jane|doe|{uuid.uuid4()}",
    )
    defaults.update(kw)
    c = Contact(**defaults)  # type: ignore[arg-type]
    session.add(c)
    session.flush()
    return c


def test_generate_persists_ranked_candidates_and_one_selection(db_session: Session) -> None:
    c = _contact(db_session)
    result = generate_candidates(db_session, c)
    assert result.selected is not None
    assert not result.needs_review
    ranks = [cand.rank for cand in result.candidates]
    assert ranks == sorted(ranks)
    selected = [cand for cand in result.candidates if cand.selected]
    assert len(selected) == 1
    assert selected[0].selection_reason


def test_regeneration_is_idempotent(db_session: Session) -> None:
    c = _contact(db_session)
    first = generate_candidates(db_session, c)
    n1 = len(first.candidates)
    second = generate_candidates(db_session, c)
    assert len(second.candidates) == n1
    total = db_session.query(EmailCandidate).filter_by(contact_id=c.id).count()
    assert total == n1  # not accumulated


def test_imported_email_becomes_selected_candidate(db_session: Session) -> None:
    c = _contact(db_session, email="jane.doe@acme.com")
    result = generate_candidates(db_session, c)
    assert result.selected is not None
    assert result.selected.source == EmailCandidateSource.IMPORTED
    assert result.selected.email == "jane.doe@acme.com"


def test_unrenderable_name_without_domain_or_email_routes_to_review(db_session: Session) -> None:
    c = _contact(db_session, first_name="Аня", last_name="Иванова", company_domain="")
    result = generate_candidates(db_session, c)
    assert result.needs_review is True
    assert result.selected is None
    assert result.review_reason


def test_one_selected_invariant_enforced(db_session: Session) -> None:
    c = _contact(db_session)
    generate_candidates(db_session, c)
    sel = get_selected_candidate(db_session, c.id)
    assert sel is not None
