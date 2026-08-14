"""Two pages describing one capture must not disagree about it.

``/captures/{id}`` and ``/contact-captures/{id}`` are both legitimate — one is the
CRM record, the other the promotion queue — but they were reading the capture's
employer through different accessors, and for Sales Navigator captures the two
gave different answers. One showed the company name; the other showed a dash for
the same person, on the same data, at the same moment.

That is worse than either page being sparse, because an operator who sees a dash
concludes the company was never captured and stops looking. It was captured; the
page was reading a relationship that a SalesNav results row never populates.

The tests below are written against the *shape the extension actually sends* — no
experience rows, employer in ``current_employment_hint``, no public profile URL —
because a fixture with tidy experience rows passes while production fails.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.services.captures import promotion as capture_promotion
from app.services.crm import detail as crm_detail
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests import capture_factory


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CONTACT_CAPTURE_PROMOTION", "true")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as app_client:
        yield app_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_a_salesnav_capture_really_does_carry_no_experience_rows(db_session: Session) -> None:
    """The premise of the bug, pinned so the fixture cannot drift away from it.

    If this ever fails because the extension starts sending experience rows, the
    tests below stop testing anything and should be re-examined rather than
    quietly passing for the wrong reason.
    """

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")

    assert list(snapshot.experiences) == []
    assert snapshot.normalized_profile_url is None
    assert capture_promotion.company_hints(snapshot).name == "QuantHealth"


def test_both_readers_report_the_same_employer(db_session: Session) -> None:
    """One accessor, checked at the service layer where the divergence lived."""

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")

    promotion_view = capture_promotion.company_hints(snapshot)
    crm_view = crm_detail.get_capture_detail(db_session, snapshot.id)

    assert crm_view is not None
    assert crm_view.company is not None
    assert crm_view.company.captured_name == promotion_view.name == "QuantHealth"


def test_a_capture_with_genuinely_no_employer_still_says_so(db_session: Session) -> None:
    """The fix must not manufacture a company where none was captured.

    A dash was wrong for SalesNav captures. It is exactly right here, and the note
    has to say why rather than leaving a blank.
    """

    snapshot = capture_factory.salesnav_capture(db_session, company_name=None)

    view = crm_detail.get_capture_detail(db_session, snapshot.id)
    assert view is not None
    assert view.company is not None
    assert view.company.captured_name is None
    assert "No current employer was captured" in view.company.resolution_note


def test_the_two_pages_agree_when_rendered(client: TestClient, db_session: Session) -> None:
    """The end-to-end version of the same claim, through both routes."""

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    db_session.commit()

    crm_page = client.get(f"/captures/{snapshot.id}").text
    queue_page = client.get(f"/contact-captures/{snapshot.id}").text

    assert "QuantHealth" in queue_page
    assert "QuantHealth" in crm_page, (
        "the CRM page showed a dash for a company the promotion page displayed"
    )


def test_the_salesnav_alias_is_shown_as_the_linkedin_address(
    client: TestClient, db_session: Session
) -> None:
    """A results row has no profile URL, only a member id and a derived alias.

    The alias was sitting on the same row the page was rendering and was simply
    not being read.
    """

    snapshot = capture_factory.salesnav_capture(db_session, member_id="ACwAAAB1x9k")
    db_session.commit()

    body = client.get(f"/captures/{snapshot.id}").text
    assert "https://www.linkedin.com/in/ACwAAAB1x9k" in body


def test_the_alias_is_labelled_as_an_alias(db_session: Session) -> None:
    """The difference is load-bearing, not cosmetic.

    Only a normalized profile URL may match a person to an existing Contact
    automatically. An alias opens the right page for a human and is deliberately
    not identity evidence, so a page showing one must not imply otherwise.
    """

    aliased = capture_factory.salesnav_capture(db_session)
    view = crm_detail.get_capture_detail(db_session, aliased.id)
    assert view is not None
    assert view.linkedin_url_is_alias

    captured = capture_factory.salesnav_capture(
        db_session, normalized_profile_url="https://www.linkedin.com/in/dana-whitfield"
    )
    real = crm_detail.get_capture_detail(db_session, captured.id)
    assert real is not None
    assert not real.linkedin_url_is_alias
    assert real.linkedin_url == "https://www.linkedin.com/in/dana-whitfield"


def test_a_capture_with_no_linkedin_address_at_all_still_shows_a_dash(
    db_session: Session,
) -> None:
    snapshot = capture_factory.salesnav_capture(db_session, with_alias=False)

    view = crm_detail.get_capture_detail(db_session, snapshot.id)
    assert view is not None
    assert view.linkedin_url is None
    assert not view.linkedin_url_is_alias


def test_an_unattempted_lookup_says_which_switch_is_missing(
    client: TestClient, db_session: Session
) -> None:
    """ "not_started · 0 attempt(s)" reported a state and explained nothing.

    Four unrelated preconditions produce it, none of them visible anywhere, and a
    queue silently piling up behind an unset flag reads as a broken pipeline. The
    page now names what is missing — including where to change it, because "the
    lookup flag" is not something anyone can act on.
    """

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    db_session.commit()

    body = client.get(f"/contact-captures/{snapshot.id}").text

    assert "not_started" in body
    assert "Nothing has been looked up for this company yet" in body
    assert "configuration state, not a result" in body
    # Where the switch actually lives, under the name the screen shows it under.
    # These are operator controls now, so the panel points at the Admin
    # Configuration screen rather than at an .env line nobody can edit here.
    assert "logo.dev domain lookup — Admin → Configuration" in body
    # The one precondition that is genuinely a deployment secret still names its
    # variable, verbatim and unbroken: it is the thing the reader has to
    # reproduce exactly, and no screen can set it.
    assert "LOGO_DEV_API_KEY" in body
