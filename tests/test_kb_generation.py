"""Filling the seller knowledge base from the seller's own website.

Every test injects a scripted thinker: nothing shells out and nothing reaches a
network. The guarantees under test are mostly about restraint — what the generator
refuses to overwrite, and what it refuses to store.
"""

from __future__ import annotations

import pytest
from app.models.enums import SellerOfferingType
from app.services.seller import generate as kb
from app.services.seller import records
from app.services.seller.profile import get_profile, save_profile
from app.services.thinking.contracts import ThinkingRequest, ThinkingResult, ThinkingTimeout
from sqlalchemy.orm import Session


class ScriptedThinker:
    name = "scripted"
    version = "scripted/v1"

    def __init__(self, payload: dict[str, object] | None = None, *, error: Exception | None = None):
        self._payload = payload or {}
        self._error = error
        self.requests: list[ThinkingRequest] = []

    def think(self, request: ThinkingRequest) -> ThinkingResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return ThinkingResult(
            payload=self._payload,
            producer=self.name,
            producer_version=self.version,
            duration_seconds=0.01,
        )


FULL = {
    "profile": {
        "name": "Kiln Systems",
        "short_description": "Industrial kiln controllers.",
        "industries_served": ["Cement", "Lime"],
        "capabilities": ["Retrofit control systems"],
    },
    "offerings": [
        {"name": "KilnOS", "offering_type": "product", "short_description": "控制 software."},
        {"name": "Commissioning", "offering_type": "service"},
    ],
    "proof_points": [
        {"statement": "Deployed at 40 plants.", "source_reference": "https://kiln.example/about"}
    ],
    "personas": [{"name": "Plant Manager", "seniority": "Senior"}],
    "unknowns": ["pricing"],
}


# --- parsing ----------------------------------------------------------------


def test_a_bare_domain_is_accepted_and_given_a_scheme() -> None:
    """An operator typing their own company's site should not need to remember https."""

    assert kb.parse_websites("kiln.example") == ("https://kiln.example",)


def test_duplicates_collapse_and_blank_lines_are_ignored() -> None:
    parsed = kb.parse_websites("https://kiln.example\n\nhttps://kiln.example\n kiln.example/about ")
    assert parsed == ("https://kiln.example", "https://kiln.example/about")


@pytest.mark.parametrize("raw", ["", "   \n  ", "not a url at all\n"])
def test_input_that_is_not_a_website_is_refused(raw: str) -> None:
    with pytest.raises(kb.KnowledgeBaseGenerationError):
        kb.parse_websites(raw)


def test_too_many_sites_is_refused_with_the_reason() -> None:
    """Five different companies would blend into one profile describing none of them."""

    with pytest.raises(kb.KnowledgeBaseGenerationError) as caught:
        kb.parse_websites("\n".join(f"site{i}.example" for i in range(kb.MAX_WEBSITES + 1)))
    assert "at a time" in str(caught.value)


# --- generation -------------------------------------------------------------


def test_a_full_answer_creates_every_kind_of_record(db_session: Session) -> None:
    thinker = ScriptedThinker(FULL)
    result = kb.generate_from_websites(
        db_session, websites=("https://kiln.example",), thinker=thinker
    )

    assert result.profile_written is True
    assert result.offerings == ["KilnOS", "Commissioning"]
    assert result.proof_points == 1
    assert result.personas == ["Plant Manager"]
    assert result.unknowns == ["pricing"]
    assert result.created_anything is True

    profile = get_profile(db_session)
    assert profile is not None
    assert profile.name == "Kiln Systems"
    assert profile.industries_served == ["Cement", "Lime"]

    offerings = {o.name: o for o in records.list_offerings(db_session)}
    assert offerings["KilnOS"].offering_type is SellerOfferingType.PRODUCT
    assert offerings["Commissioning"].offering_type is SellerOfferingType.SERVICE


def test_reading_the_seller_site_is_the_one_call_allowed_to_browse(
    db_session: Session,
) -> None:
    thinker = ScriptedThinker(FULL)
    kb.generate_from_websites(db_session, websites=("https://kiln.example",), thinker=thinker)
    assert thinker.requests[0].allowed_tools == ("WebSearch",)


def test_an_existing_profile_is_left_alone_and_said_so(db_session: Session) -> None:
    """An operator's own wording outranks a generated one, and silence would hide that."""

    save_profile(db_session, name="What The Operator Typed")
    result = kb.generate_from_websites(
        db_session, websites=("https://kiln.example",), thinker=ScriptedThinker(FULL)
    )

    assert result.profile_written is False
    assert any("already exists" in note for note in result.skipped)
    profile = get_profile(db_session)
    assert profile is not None
    assert profile.name == "What The Operator Typed"


def test_a_duplicate_offering_is_skipped_with_its_reason_not_silently_dropped(
    db_session: Session,
) -> None:
    records.create_offering(db_session, name="KilnOS", created_by="operator")
    result = kb.generate_from_websites(
        db_session, websites=("https://kiln.example",), thinker=ScriptedThinker(FULL)
    )

    assert "KilnOS" not in result.offerings
    assert "Commissioning" in result.offerings
    assert any("KilnOS" in note for note in result.skipped)
    # The operator's row survived; there is exactly one.
    assert len([o for o in records.list_offerings(db_session) if o.name == "KilnOS"]) == 1


def test_an_empty_answer_reports_that_nothing_was_established(db_session: Session) -> None:
    """A thin website must produce a visibly thin result, not a quiet success."""

    result = kb.generate_from_websites(
        db_session, websites=("https://kiln.example",), thinker=ScriptedThinker({})
    )
    assert result.created_anything is False
    assert "Nothing could be established" in result.summary


def test_entries_without_the_field_that_identifies_them_are_ignored(
    db_session: Session,
) -> None:
    """A nameless offering or persona is not storable and is not invented into one."""

    result = kb.generate_from_websites(
        db_session,
        websites=("https://kiln.example",),
        thinker=ScriptedThinker(
            {
                "offerings": [{"short_description": "no name at all"}],
                "personas": [{"seniority": "Senior"}],
                "proof_points": [{"supporting_detail": "no statement"}],
            }
        ),
    )
    assert result.offerings == []
    assert result.personas == []
    assert result.proof_points == 0


def test_a_model_failure_is_reported_rather_than_half_applied(db_session: Session) -> None:
    with pytest.raises(kb.KnowledgeBaseGenerationError):
        kb.generate_from_websites(
            db_session,
            websites=("https://kiln.example",),
            thinker=ScriptedThinker(error=ThinkingTimeout("too slow")),
        )
    assert get_profile(db_session) is None
