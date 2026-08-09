"""IMP-001 → SEQ-001: an imported Contact through to a seven-message sequence.

Two features that were built in parallel meet here for the first time. Each has
its own truth model, and the whole point of this file is that neither is allowed
to launder the other's.

The import model says: *somebody else told us this address*. The verification
model says: *we asked a provider about this exact mailbox and it answered*. An
imported address that reached Personalization must still read as the first thing
and never as the second — not on the Contact page, not in the Admin diagnosis,
not in the sequence's own lineage, and not by the mere fact that seven
personalized messages now exist for that person.

The tests are grouped A–G to match the reconciliation brief, so a reviewer can
find the specific claim they want to check.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.campaign import CampaignContact
from app.models.contact import Contact
from app.models.draft import DraftVersion
from app.models.email_candidate import EmailCandidate
from app.models.email_evidence import ExactEmailVerification
from app.models.email_sequence import (
    SEQUENCE_LENGTH,
    EmailSequence,
    EmailSequenceMessage,
    EmailSequenceMessageReview,
    EmailSequenceMessageVersion,
)
from app.models.enums import (
    SequenceMessageOrigin,
    SequenceReviewState,
    SuppressionReason,
    SuppressionType,
    VerificationJobStatus,
)
from app.models.imported_email import ImportedContactEmail, ImportSourceIdentifier
from app.models.verification_job import AgentJob
from app.services import suppressions
from app.services.imports import campaign_import
from app.services.personalization import policy as personalization_policy
from app.services.personalization import sequence as sequence_generation
from app.services.sequences import persistence as sequence_persistence
from app.services.sequences import read as sequence_read
from app.services.sequences import review as sequence_review
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from tests import apollo_factory as af
from tests.test_campaign_import_final_review import _live_formulas
from tests.test_email_sequence import BODIES, SUBJECTS, CountingThinker, sequence_payload

IMPORTED_ADDRESS = "ada@engines.example"


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def _client(db_session: Session, monkeypatch: pytest.MonkeyPatch, *, sequences: bool) -> TestClient:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    if sequences:
        monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
    else:
        monkeypatch.delenv("FEATURES__EMAIL_SEQUENCES", raising=False)
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(db_session, monkeypatch, sequences=True) as app_client:
        yield app_client
    get_settings.cache_clear()


@pytest.fixture()
def client_off(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(db_session, monkeypatch, sequences=False) as app_client:
        yield app_client
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _import_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The import service refuses outright while its flag is off.

    Set for the whole module because these tests are about what happens *after*
    an import, and every one of them needs a real imported Contact.
    """

    monkeypatch.setenv("FEATURES__CSV_IMPORT", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def imported(db_session: Session) -> tuple[Any, ...]:
    """One Apollo-imported Contact, enrolled, opted in to sequences.

    Built through ``campaign_import.confirm`` rather than by hand-inserting
    rows: the point of these tests is the real import path, and a synthetic
    Contact would prove nothing about how import truth behaves.
    """

    campaign = af.make_campaign(db_session, execution=True)
    campaign.cadence_config = {"sequence": {"enabled": True}}
    campaign.description = "Sourced market intelligence reports for investment teams"
    db_session.flush()

    result = campaign_import.confirm(
        db_session,
        campaign_id=campaign.id,
        content=af.csv_bytes([af.row()]),
        filename="apollo.csv",
    )
    contact = db_session.scalars(select(Contact)).one()
    membership = db_session.scalars(
        select(CampaignContact).where(CampaignContact.campaign_id == campaign.id)
    ).one()
    policy = personalization_policy.ensure_initial_policy(db_session, actor="test")
    return campaign, contact, membership, policy, result


def _generate(
    db_session: Session, imported: tuple[Any, ...]
) -> tuple[EmailSequence, CountingThinker]:
    """Generate and persist a sequence for the imported Contact."""

    _campaign, contact, membership, policy, _result = imported
    thinker = CountingThinker(sequence_payload())
    generated = sequence_generation.generate_sequence(
        db_session, membership=membership, policy=policy, thinker=thinker
    )
    sequence = sequence_persistence.persist_sequence(
        db_session, membership=membership, contact=contact, generated=generated
    )
    return sequence, thinker


# ===========================================================================
# A. Imported Contact → sequence
# ===========================================================================


def test_an_imported_contact_produces_exactly_one_seven_message_sequence(
    db_session: Session, imported: tuple[Any, ...]
) -> None:
    _campaign, contact, membership, _policy, _result = imported
    assert contact.email == IMPORTED_ADDRESS

    sequence, thinker = _generate(db_session, imported)

    assert thinker.calls == 1, "one bounded model call, as for any other contact"
    assert db_session.scalar(select(func.count(EmailSequence.id))) == 1
    assert sequence.campaign_contact_id == membership.id

    messages = db_session.scalars(
        select(EmailSequenceMessage).where(
            EmailSequenceMessage.sequence_key == sequence.sequence_key
        )
    ).all()
    assert len(messages) == SEQUENCE_LENGTH

    versions = sequence_read.message_rows(db_session, sequence=sequence)
    assert len(versions) == SEQUENCE_LENGTH
    assert [row.position for row in versions] == [1, 2, 3, 4, 5, 6, 7]


def test_sequence_mode_writes_no_legacy_draft_for_the_imported_contact(
    db_session: Session, imported: tuple[Any, ...]
) -> None:
    _generate(db_session, imported)
    assert db_session.scalar(select(func.count(DraftVersion.id))) == 0, (
        "sequence mode must not also produce the single-draft outcome"
    )


def test_suppression_still_blocks_an_imported_contact_from_sequence_generation(
    db_session: Session, imported: tuple[Any, ...]
) -> None:
    """The suppression re-check reads the imported address, and still refuses."""

    _campaign, _contact, membership, policy, _result = imported
    suppressions.add_suppression(
        db_session,
        suppression_type=SuppressionType.EMAIL,
        value=IMPORTED_ADDRESS,
        reason=SuppressionReason.OPT_OUT,
        actor="test",
    )
    db_session.flush()

    thinker = CountingThinker(sequence_payload())
    with pytest.raises(sequence_generation.SequenceGenerationError) as excinfo:
        sequence_generation.generate_sequence(
            db_session, membership=membership, policy=policy, thinker=thinker
        )
    assert excinfo.value.code == "suppression"
    assert "opt_out" in str(excinfo.value)
    assert thinker.calls == 0, "a suppressed contact must not reach the model at all"
    assert db_session.scalar(select(func.count(EmailSequence.id))) == 0


# ===========================================================================
# B. Imported truth stays imported
# ===========================================================================


def test_generating_a_sequence_fabricates_no_verification_evidence(
    db_session: Session, imported: tuple[Any, ...]
) -> None:
    """The load-bearing test of this whole file.

    Seven personalized messages now exist for this person. That must not have
    created a provider result, a candidate, or a provider call — an imported
    address is somebody else's claim, and nothing Personalization does can
    upgrade it into a mailbox somebody asked a provider about.
    """

    before_verifications = db_session.scalar(select(func.count(ExactEmailVerification.id)))
    before_candidates = db_session.scalar(select(func.count(EmailCandidate.id)))
    before_jobs = db_session.scalar(
        select(func.count(AgentJob.id)).where(AgentJob.email.is_not(None))
    )

    _generate(db_session, imported)

    assert db_session.scalar(select(func.count(ExactEmailVerification.id))) == before_verifications
    assert db_session.scalar(select(func.count(EmailCandidate.id))) == before_candidates
    assert (
        db_session.scalar(select(func.count(AgentJob.id)).where(AgentJob.email.is_not(None)))
        == before_jobs
    )
    # And in absolute terms: none of the three exists at all.
    assert db_session.scalar(select(func.count(ExactEmailVerification.id))) == 0
    assert db_session.scalar(select(func.count(EmailCandidate.id))) == 0


def test_no_verification_job_is_created_or_completed_by_sequence_generation(
    db_session: Session, imported: tuple[Any, ...]
) -> None:
    _generate(db_session, imported)
    live = db_session.scalars(
        select(AgentJob).where(
            AgentJob.status.in_(
                (
                    VerificationJobStatus.PENDING,
                    VerificationJobStatus.LEASED,
                    VerificationJobStatus.IN_PROGRESS,
                )
            )
        )
    ).all()
    assert [job for job in live if job.email is not None] == []


def test_imported_email_provenance_survives_personalization(
    db_session: Session, imported: tuple[Any, ...]
) -> None:
    """The vendor's own words are still there, still labelled as the vendor's."""

    _campaign, contact, _membership, _policy, _result = imported
    before = db_session.scalars(
        select(ImportedContactEmail).where(ImportedContactEmail.contact_id == contact.id)
    ).all()
    assert before, "the import must have recorded its own evidence"
    snapshot = [
        (
            row.id,
            row.normalized_email,
            row.provider_status_normalized,
            row.provider_verification_source,
        )
        for row in before
    ]

    _generate(db_session, imported)

    after = db_session.scalars(
        select(ImportedContactEmail).where(ImportedContactEmail.contact_id == contact.id)
    ).all()
    assert [
        (
            row.id,
            row.normalized_email,
            row.provider_status_normalized,
            row.provider_verification_source,
        )
        for row in after
    ] == snapshot, "Personalization must not rewrite imported-email evidence"

    identifiers = db_session.scalars(
        select(ImportSourceIdentifier).where(ImportSourceIdentifier.contact_id == contact.id)
    ).all()
    assert identifiers, "Apollo source identifiers must survive Personalization"


def test_the_verification_vocabulary_never_gains_a_bypass_member() -> None:
    """`VerificationDecision` must keep meaning *a real provider decision*.

    Asserted as an exact set rather than a membership check: adding a value is
    exactly the change this guards against, and a `not in` test would not catch
    a differently-spelled one.
    """

    from app.services.verification.decisions import VerificationDecision

    assert {member.value for member in VerificationDecision} == {
        "accept",
        "try_next_candidate",
        "retry_later",
        "stop_no_result",
        "refused",
    }


def test_the_sequence_modules_reference_no_verification_concept() -> None:
    """A structural proof, stronger than any single behavioural assertion.

    If the sequence code never mentions verification, candidates or exact-email
    evidence in executable code, it cannot fabricate any of them by any path.
    """

    import io
    import pathlib
    import tokenize

    forbidden = (
        "exactemailverification",
        "emailcandidate",
        "verificationdecision",
        "millionverifier",
        "debounce",
    )
    roots = [
        pathlib.Path("app/services/sequences"),
        pathlib.Path("app/services/personalization/sequence.py"),
        pathlib.Path("app/services/personalization/sequence_validation.py"),
        pathlib.Path("app/services/personalization/cadence.py"),
    ]
    files = [
        path
        for root in roots
        for path in ([root] if root.is_file() else sorted(root.rglob("*.py")))
    ]
    assert files
    for path in files:
        code = " ".join(
            token.string.casefold()
            for token in tokenize.generate_tokens(io.StringIO(path.read_text()).readline)
            if token.type not in {tokenize.STRING, tokenize.COMMENT}
        )
        for marker in forbidden:
            assert marker not in code, f"{path} references {marker}"


# ===========================================================================
# C. Research / Company Intelligence / Insights lineage
# ===========================================================================


def test_sequence_lineage_is_recorded_for_an_imported_contact(
    db_session: Session, imported: tuple[Any, ...]
) -> None:
    sequence, _thinker = _generate(db_session, imported)

    assert sequence.research_lineage is not None
    assert sequence.insights_lineage is not None
    assert sequence.intelligence_lineage is not None
    assert sequence.personalization_policy_version_id is not None
    assert sequence.personalization_strategy_id
    decision = sequence.personalization_decision
    assert isinstance(decision, dict)
    assert "fallback_identifier" in decision
    assert "context_used" in decision


def test_company_intelligence_stays_non_citable_for_an_imported_contact(
    db_session: Session, imported: tuple[Any, ...]
) -> None:
    """Arriving by import grants no evidence privileges whatsoever."""

    _campaign, _contact, membership, policy, _result = imported
    payload = sequence_payload()
    # A later follow-up reaching for an id the policy did not supply is refused
    # exactly as it is for a captured or hand-entered contact.
    payload["messages"][4]["evidence_insight_ids"] = ["11111111-1111-1111-1111-111111111111"]
    thinker = CountingThinker(payload)
    with pytest.raises(sequence_generation.SequenceGenerationError) as excinfo:
        sequence_generation.generate_sequence(
            db_session, membership=membership, policy=policy, thinker=thinker
        )
    assert excinfo.value.code == "citation_not_supplied"


def test_an_imported_contact_with_no_prospect_evidence_uses_the_offering_fallback(
    db_session: Session, imported: tuple[Any, ...]
) -> None:
    """Thin evidence stays thin. Import does not manufacture context."""

    _campaign, _contact, membership, policy, _result = imported
    thinker = CountingThinker(sequence_payload())
    generated = sequence_generation.generate_sequence(
        db_session, membership=membership, policy=policy, thinker=thinker
    )
    assert generated.decision.fallback_level == 5
    assert generated.decision.fallback_identifier == "offering_led"
    assert all(not message.evidence_insight_ids for message in generated.messages)


# ===========================================================================
# D. Combined Admin diagnosis — both provenances, side by side
# ===========================================================================


def test_admin_diagnosis_shows_import_origin_and_sequence_together(
    db_session: Session, client: TestClient, imported: tuple[Any, ...]
) -> None:
    """Neither provenance may be collapsed into the other.

    Asserted against IMP-001's own canonical statements rather than against
    guessed wording. Those constants exist precisely so no template can
    paraphrase the bypass into something weaker, which makes them the right
    thing for a cross-feature test to pin.
    """

    from app.services.admin_workbench.import_lineage import (
        BYPASS_STATEMENT,
        NO_DISCOVERY_STATEMENT,
    )

    campaign, _contact, membership, _policy, _result = imported
    sequence, _thinker = _generate(db_session, imported)

    response = client.get(f"/admin/campaigns/{campaign.id}/contacts/{membership.id}")
    assert response.status_code == 200
    body = response.text

    # --- Origin: the file, the row, the address, the identifiers ---
    assert "apollo.csv" in body
    assert IMPORTED_ADDRESS in body

    # --- Email: nothing was generated, nothing was discovered ---
    assert NO_DISCOVERY_STATEMENT in body

    # --- Verification: no provider was called ---
    assert BYPASS_STATEMENT in body

    # --- Personalization: the sequence, as its own distinct section ---
    assert f"Sequence v{sequence.sequence_version}" in body
    assert "Input digest" in body
    assert "Research lineage" in body
    assert "Insights lineage" in body
    assert "Company Intelligence lineage" in body

    # --- And they are separate sections, not one merged blob ---
    assert body.index("apollo.csv") != body.index(f"Sequence v{sequence.sequence_version}")


def test_admin_diagnosis_never_labels_an_imported_address_as_verified(
    db_session: Session, client: TestClient, imported: tuple[Any, ...]
) -> None:
    """No *affirmative* verification claim may appear for an imported address.

    The naive version of this test — searching for the substring "verified
    mailbox" — fails against a correct page, because IMP-001's own disclaimer
    contains that phrase inside a negation: "not a provider-verified mailbox".
    So the check is that every occurrence of the phrase is part of that
    disclaimer, and that no affirmative claim appears anywhere.
    """

    import re

    from app.services.admin_workbench.import_lineage import BYPASS_STATEMENT

    campaign, _contact, membership, _policy, _result = imported
    _generate(db_session, imported)
    body = client.get(f"/admin/campaigns/{campaign.id}/contacts/{membership.id}").text
    lowered = body.casefold()

    # The disclaimer is present, and is the only thing saying "verified mailbox".
    assert BYPASS_STATEMENT in body
    for match in re.finditer(r"verified mailbox", lowered):
        window = lowered[max(0, match.start() - 40) : match.start()]
        assert "not a provider-" in window, (
            "an occurrence of 'verified mailbox' that is not part of the disclaimer"
        )

    # No affirmative claim, in any wording, and no provider name.
    for claim in (
        "provider confirmed this",
        "provider verified this",
        "millionverifier",
        "debounce",
        "verification evidence:",
    ):
        assert claim not in lowered, f"the page claims {claim!r} for an imported address"


def test_an_admin_diagnosis_get_writes_nothing(
    db_session: Session, client: TestClient, imported: tuple[Any, ...]
) -> None:
    from app.models.audit_event import AuditEvent

    campaign, _contact, membership, _policy, _result = imported
    _generate(db_session, imported)
    before = db_session.scalar(select(func.count(AuditEvent.id)))

    client.get(f"/admin/campaigns/{campaign.id}/contacts/{membership.id}")
    client.get(f"/admin/campaigns/{campaign.id}")
    client.get("/admin/contacts")

    assert db_session.scalar(select(func.count(AuditEvent.id))) == before


# ===========================================================================
# E. Imported Contact with sequence mode off
# ===========================================================================


def test_an_imported_contact_with_sequences_off_keeps_its_import_truth(
    db_session: Session, client_off: TestClient, imported: tuple[Any, ...]
) -> None:
    from app.services.agents.adapters import sequence_mode_enabled

    campaign, contact, membership, _policy, _result = imported
    get_settings.cache_clear()
    assert sequence_mode_enabled(get_settings(), campaign) is False

    # No sequence row exists on any path.
    assert db_session.scalar(select(func.count(EmailSequence.id))) == 0

    body = client_off.get(f"/app/contacts/{contact.id}?campaign={membership.campaign_id}").text
    assert "The seven-message sequence" not in body
    # Import provenance is untouched and still rendered.
    assert db_session.scalars(
        select(ImportedContactEmail).where(ImportedContactEmail.contact_id == contact.id)
    ).all()


def test_a_legacy_draft_for_an_imported_contact_is_unaffected_by_sequences(
    db_session: Session, client_off: TestClient, imported: tuple[Any, ...]
) -> None:
    _campaign, contact, membership, _policy, _result = imported
    db_session.add(
        DraftVersion(
            contact_id=contact.id,
            campaign_id=membership.campaign_id,
            version_number=1,
            subject="A single draft for an imported contact",
            body="Written in single-draft mode, and unchanged by the sequence feature.",
        )
    )
    db_session.flush()

    body = client_off.get("/app/review").text
    assert "A single draft for an imported contact" in body
    assert "v2-seq-card" not in body


# ===========================================================================
# F. A normal, non-imported Contact
# ===========================================================================


def test_a_non_imported_contact_still_produces_a_sequence_and_no_import_provenance(
    db_session: Session, client: TestClient
) -> None:
    from tests.test_agent_studio_policy import _policy, _subject, _supported_insight
    from tests.test_email_sequence import build

    campaign, company, contact, membership = _subject(
        db_session,
        title="Head of Research",
        industry="Industrial technology",
        campaign_description="Sourced market intelligence reports for investment teams",
    )
    campaign.cadence_config = {"sequence": {"enabled": True}}
    db_session.flush()
    evidence_id = _supported_insight(
        db_session, company, "kiln control: publishes sourced market coverage"
    )
    policy = _policy(db_session)
    sequence = build(db_session, (campaign, company, contact, membership, policy, evidence_id))

    assert len(sequence_read.message_rows(db_session, sequence=sequence)) == SEQUENCE_LENGTH
    # No import provenance is invented for a contact that never came from a file.
    assert (
        db_session.scalars(
            select(ImportedContactEmail).where(ImportedContactEmail.contact_id == contact.id)
        ).all()
        == []
    )
    assert (
        db_session.scalars(
            select(ImportSourceIdentifier).where(ImportSourceIdentifier.contact_id == contact.id)
        ).all()
        == []
    )

    body = client.get(f"/admin/campaigns/{campaign.id}/contacts/{membership.id}").text
    assert response_is_truthful_about_absent_import(body)


def response_is_truthful_about_absent_import(body: str) -> bool:
    """The Admin page must not imply an import that never happened.

    A page that simply omits the import section is truthful; one that shows an
    empty import block with a filename would not be. Either shape passes as long
    as no import filename or source identifier is asserted.
    """

    return "apollo.csv" not in body


# ===========================================================================
# G. Regeneration for an imported Contact
# ===========================================================================


def test_regeneration_keeps_message_identity_and_import_provenance_stable(
    db_session: Session, imported: tuple[Any, ...]
) -> None:
    campaign, contact, _membership, _policy, _result = imported
    first, _thinker = _generate(db_session, imported)
    first_ids = {
        row.position: row.message_id
        for row in sequence_read.message_rows(db_session, sequence=first)
    }
    rows = sequence_read.message_rows(db_session, sequence=first)
    sequence_review.approve_message(db_session, message_version_id=rows[0].version_id)

    imported_before = db_session.scalars(
        select(ImportedContactEmail).where(ImportedContactEmail.contact_id == contact.id)
    ).all()
    snapshot = [
        (row.id, row.normalized_email, row.provider_status_normalized) for row in imported_before
    ]

    campaign.primary_cta = "A different ask entirely"
    db_session.flush()
    second, _thinker2 = _generate(db_session, imported)

    # Logical identity survives.
    second_ids = {
        row.position: row.message_id
        for row in sequence_read.message_rows(db_session, sequence=second)
    }
    assert second_ids == first_ids
    assert second.sequence_key == first.sequence_key
    assert second.sequence_version == 2

    # Content versions moved on, and the old approval did not follow them.
    new_rows = sequence_read.message_rows(db_session, sequence=second)
    assert all(row.decision is None for row in new_rows)
    assert all(row.origin is SequenceMessageOrigin.REGENERATED for row in new_rows)

    # The old sequence stays auditable.
    db_session.refresh(first)
    assert first.superseded_at is not None
    assert first.review_state is SequenceReviewState.SUPERSEDED
    assert db_session.scalar(select(func.count(EmailSequenceMessageReview.id))) == 1

    # Import provenance is untouched, and no verification lineage appeared.
    after = db_session.scalars(
        select(ImportedContactEmail).where(ImportedContactEmail.contact_id == contact.id)
    ).all()
    assert [
        (row.id, row.normalized_email, row.provider_status_normalized) for row in after
    ] == snapshot
    assert db_session.scalar(select(func.count(ExactEmailVerification.id))) == 0
    assert db_session.scalar(select(func.count(EmailCandidate.id))) == 0


# ===========================================================================
# Combined Admin performance — both features now add readers
# ===========================================================================


def test_the_combined_admin_diagnosis_query_count_is_bounded_by_history(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, imported: tuple[Any, ...]
) -> None:
    """Import lineage plus sequence history must not multiply together."""

    from app.services.admin_workbench.reader import AdminWorkbenchReader

    campaign, _contact, membership, _policy, _result = imported

    def _measure() -> int:
        statements: list[str] = []

        def _record(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
        get_settings.cache_clear()
        reader = AdminWorkbenchReader(db_session, settings=get_settings())
        event.listen(db_session.bind, "before_cursor_execute", _record)
        try:
            view = reader.contact_diagnosis(membership.campaign_id, membership.id)
        finally:
            event.remove(db_session.bind, "before_cursor_execute", _record)
        assert view is not None
        return len(statements)

    _generate(db_session, imported)
    one = _measure()

    for index in range(5):
        campaign.primary_cta = f"CTA revision {index}"
        db_session.flush()
        _generate(db_session, imported)
    many = _measure()

    assert many == one, (
        f"6 sequence versions cost {many} queries where 1 cost {one}; the combined "
        "Admin reader must stay bounded as sequence history grows"
    )


def test_the_review_queue_stays_compact_for_imported_contacts(
    db_session: Session, client: TestClient, imported: tuple[Any, ...]
) -> None:
    """A collapsed card carries no message body, imported or not."""

    _generate(db_session, imported)
    body = client.get("/app/review").text

    assert body.count("v2-seq-card") == 1
    for text in BODIES:
        assert text not in body
    assert SUBJECTS[0] in body


def test_hostile_import_values_stay_escaped_on_the_sequence_pages(
    db_session: Session, client: TestClient
) -> None:
    """An imported field is attacker-controlled. It must render as text."""

    campaign = af.make_campaign(db_session, execution=True)
    campaign.cadence_config = {"sequence": {"enabled": True}}
    db_session.flush()
    campaign_import.confirm(
        db_session,
        campaign_id=campaign.id,
        content=af.csv_bytes(
            [af.row(**{"First Name": "<script>alert(1)</script>", "Title": '"><img src=x>'})]
        ),
        filename="hostile.csv",
    )
    contact = db_session.scalars(select(Contact)).one()
    membership = db_session.scalars(
        select(CampaignContact).where(CampaignContact.campaign_id == campaign.id)
    ).one()
    policy = personalization_policy.ensure_initial_policy(db_session, actor="test")
    thinker = CountingThinker(sequence_payload())
    generated = sequence_generation.generate_sequence(
        db_session, membership=membership, policy=policy, thinker=thinker
    )
    sequence_persistence.persist_sequence(
        db_session, membership=membership, contact=contact, generated=generated
    )

    for url in (
        "/app/review",
        f"/app/contacts/{contact.id}?campaign={campaign.id}&step=1",
        f"/admin/campaigns/{campaign.id}/contacts/{membership.id}",
    ):
        body = client.get(url).text
        assert "<script>alert(1)</script>" not in body, url
        assert "<img src=x>" not in body, url


def test_sequence_and_draft_filters_remain_independent_after_reconciliation(
    db_session: Session, client: TestClient, imported: tuple[Any, ...]
) -> None:
    sequence, _thinker = _generate(db_session, imported)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    sequence_review.approve_sequence(
        db_session,
        sequence_id=sequence.id,
        expected_version_ids=tuple(row.version_id for row in rows),
    )

    # Updated for default approval: the sequence filters no longer describe a
    # backlog, so the pair asserted here is "contains a discard" (empty -- this
    # sequence has none) against "you reviewed these" (populated by the bulk
    # confirmation above). The property under test is unchanged: the draft
    # filter must not reinterpret the sequence one.
    discarded = client.get("/app/review?view=discarded&sview=discarded").text
    assert "v2-seq-card" not in discarded
    reviewed = client.get("/app/review?view=discarded&sview=reviewed").text
    assert "v2-seq-card" in reviewed


def test_same_origin_protection_still_refuses_after_reconciliation(
    db_session: Session, client: TestClient, imported: tuple[Any, ...]
) -> None:
    sequence, _thinker = _generate(db_session, imported)
    rows = sequence_read.message_rows(db_session, sequence=sequence)
    response = client.post(
        f"/app/review/sequence/messages/{rows[0].version_id}/edit",
        data={"subject": "Injected", "body": "From elsewhere.", "back": "/app/review"},
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert (
        db_session.scalar(
            select(func.count(EmailSequenceMessageVersion.id)).where(
                EmailSequenceMessageVersion.message_id == rows[0].message_id
            )
        )
        == 1
    )


@pytest.fixture()
def disabled_sequence_boundary(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Turn the display projection into a pass-through, in-process only.

    **Patching the Jinja filter registry is not enough here, and finding that
    out is why this fixture exists separately from the admin one.**

    ``_sequence.html`` is pulled in with ``{% import %}``. Jinja compiles an
    imported template into a module, binds the filters that module's macros use
    at module-construction time, and caches that module on the cached
    ``Template`` object for the life of the process. So
    ``monkeypatch.setitem(env.filters, "neutralize", ...)`` reaches a macro only
    if this test is the first thing in the process to import ``_sequence.html``.
    Run alone, the mutation proof passed; run after any other test that had
    already rendered a sequence page, it reported the sequence surfaces as
    unprotected -- a false negative that would have made the sweep above look
    non-vacuous when it was not.

    So the pass-through is installed *underneath* the filter instead.
    ``display.safe_text`` resolves ``neutralize_formula`` from its own module
    globals on every call, so replacing that reaches the boundary whichever
    binding a cached macro is holding.

    No repository file is touched; ``monkeypatch`` restores everything.
    """

    from app.services.admin_workbench import import_lineage
    from app.services.imports import display

    def _passthrough_optional(value: str | None) -> str | None:
        return value

    monkeypatch.setattr(display, "neutralize_formula", _passthrough_optional)
    monkeypatch.setattr(import_lineage, "_safe", _passthrough_optional)
    return _passthrough_optional


# ===========================================================================
# H. The display boundary, extended to the sequence surfaces
#
# IMP-001 established that an imported cell is attacker-controlled twice over:
# once as HTML, and once as a spreadsheet formula that fires when an operator
# copies a value back out of the page. Escaping answers the first; the
# ``neutralize`` projection answers the second.
#
# The sequence branch predates that projection -- ``app/services/imports/display``
# did not exist when its templates were written -- so its card and its contact
# table rendered imported names raw. Nothing caught it: the existing formula
# matrix sweeps admin surfaces only, and the escaping test above passes on a
# formula because a formula contains no markup.
#
# These two tests close it, and the second is what stops the first being
# vacuous.
# ===========================================================================


def _seq_urls(contact: Contact, campaign_id: Any, membership_id: Any) -> dict[str, str]:
    """Every surface that renders sequence content from imported fields."""

    return {
        "review_queue": "/app/review",
        "review_expanded": "/app/review?sview=all",
        "contact_sequence": f"/app/contacts/{contact.id}?campaign={campaign_id}&step=1",
        "admin_sequence_diagnosis": f"/admin/campaigns/{campaign_id}/contacts/{membership_id}",
    }


@pytest.fixture()
def hostile_sequence_state(db_session: Session) -> Any:
    """An imported contact whose visible fields all open a formula, with a sequence."""

    def _build(prefix: str, lead: str) -> dict[str, Any]:
        marker = f"{lead}{prefix}cmd|"
        campaign = af.make_campaign(db_session, execution=True)
        campaign.cadence_config = {"sequence": {"enabled": True}}
        db_session.flush()
        campaign_import.confirm(
            db_session,
            campaign_id=campaign.id,
            content=af.csv_bytes(
                [
                    af.row(
                        **{
                            "First Name": f"{marker}first",
                            "Last Name": f"{marker}last",
                            "Title": f"{marker}title",
                            "Company Name": f"{marker}company",
                            "Company Name for Emails": f"{marker}coemails",
                        }
                    )
                ]
            ),
            filename="hostile.csv",
        )
        contact = db_session.scalars(select(Contact)).one()
        membership = db_session.scalars(
            select(CampaignContact).where(CampaignContact.campaign_id == campaign.id)
        ).one()
        policy = personalization_policy.ensure_initial_policy(db_session, actor="test")
        generated = sequence_generation.generate_sequence(
            db_session,
            membership=membership,
            policy=policy,
            thinker=CountingThinker(sequence_payload()),
        )
        sequence_persistence.persist_sequence(
            db_session, membership=membership, contact=contact, generated=generated
        )
        # The campaign name is rendered on the card beside the contact's, and it
        # is operator-supplied rather than imported -- but it reaches the same
        # cell an operator copies, so it goes through the same boundary.
        campaign.name = f"{marker}campaign"
        db_session.flush()
        return {"campaign": campaign, "contact": contact, "membership": membership}

    return _build


@pytest.mark.parametrize("lead", ("", " ", "\t"))
@pytest.mark.parametrize("prefix", ("=", "+", "-", "@"))
def test_the_sequence_surfaces_render_no_live_formula(
    client: TestClient, hostile_sequence_state: Any, prefix: str, lead: str
) -> None:
    """Every sequence surface shows the imported value, and shows it inert."""

    state = hostile_sequence_state(prefix, lead)
    needle = f"{prefix}cmd|"
    for surface, url in _seq_urls(
        state["contact"], state["campaign"].id, state["membership"].id
    ).items():
        body = client.get(url).text
        assert not _live_formulas(body, needle), f"{surface} rendered a live formula"


@pytest.mark.parametrize("prefix", ("=", "+", "-", "@"))
def test_the_sequence_formula_sweep_would_fail_if_the_boundary_were_removed(
    client: TestClient,
    hostile_sequence_state: Any,
    disabled_sequence_boundary: Any,
    prefix: str,
) -> None:
    """The mutation proof: with the projection neutered, every surface must leak.

    Without this, the sweep above could pass because the hostile value never
    reached the page at all -- which is exactly how the defect survived the
    original branch's own escaping test.
    """

    state = hostile_sequence_state(prefix, "")
    needle = f"{prefix}cmd|"
    unprotected: list[str] = []
    for surface, url in _seq_urls(
        state["contact"], state["campaign"].id, state["membership"].id
    ).items():
        if not _live_formulas(client.get(url).text, needle):
            unprotected.append(surface)
    assert not unprotected, (
        "with the boundary disabled these sequence surfaces still showed no live "
        f"formula, so their absence assertion proves nothing: {unprotected}"
    )
