"""The operator control that puts a Campaign into seven-message mode.

Why this file exists at all. ``cadence_config["sequence"]["enabled"]`` has been
read by sequence generation since SEQ-001 and was written by nothing: the only
way to opt a Campaign in was to edit JSONB by hand. A reader whose writer does
not exist has never had its assumptions tested from the writing side, and the
two assumptions that matter are both silent when broken.

**Unrelated keys must survive.** The column belongs to the Campaign and this
module claims exactly one key inside it. A writer that replaced the whole object
would discard anything stored alongside — and nothing would fail, because
nothing else reads the column yet.

**The flag must be a real ``bool``.** :func:`campaign_opted_in` tests
``is True``. An HTML checkbox arrives as the string ``"on"``, which is truthy in
every ordinary sense and would read back as *not* opted in: a switch that
appears to work, saves without error, and does nothing. That failure is asserted
directly here rather than inferred, because "the value is truthy" is precisely
the assertion that would have let it through.

**An absent field is not always "off".** The checkbox is rendered only when the
deployment flag is on. Where it is not rendered, reading it unconditionally
would opt a Campaign *out* every time somebody renamed it. That regression is
the last section of this file.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.api.deps import get_db
from app.core.config import get_settings
from app.main import create_app
from app.models.audit_event import AuditEvent
from app.models.campaign import Campaign
from app.models.email_sequence import SEQUENCE_LENGTH
from app.models.enums import CampaignStatus
from app.services.personalization.cadence import (
    CADENCE_KEY,
    DEFAULT_ELAPSED_DAYS,
    UNREADABLE_CONFIG_KEY,
    UNREADABLE_SEQUENCE_KEY,
    campaign_opted_in,
    sequence_settings,
    with_campaign_opt_in,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

#: The ratified ladder, spelled out rather than imported, so that a change to
#: the constant has to be made in two places by somebody who meant it.
RATIFIED_LADDER: tuple[int, ...] = (0, 3, 7, 12, 18, 25, 35)


def _client(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sequences: bool = True,
) -> TestClient:
    monkeypatch.setenv("FEATURES__WORKBENCH", "true")
    monkeypatch.setenv("FEATURES__AGENT_WORKBENCH", "true")
    if sequences:
        monkeypatch.setenv("FEATURES__EMAIL_SEQUENCES", "true")
    else:
        # Explicitly removed rather than left to the default: this fixture's
        # whole purpose is a deployment where the control is not offered, and
        # inheriting an enabled flag would make its assertions vacuous.
        monkeypatch.delenv("FEATURES__EMAIL_SEQUENCES", raising=False)
    get_settings.cache_clear()
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    return TestClient(app)


@pytest.fixture()
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(db_session, monkeypatch) as app_client:
        yield app_client
    get_settings.cache_clear()


@pytest.fixture()
def client_without_sequences(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with _client(db_session, monkeypatch, sequences=False) as app_client:
        yield app_client
    get_settings.cache_clear()


def _campaign(db: Session, *, cadence_config: Any = None) -> Campaign:
    """A draft Campaign, execution off, carrying whatever cadence config is given.

    Built directly rather than through ``create_campaign`` so that a deliberately
    malformed ``cadence_config`` can be planted — the service validates the
    column, and the point of several tests below is what happens to a row that
    was written before that validation existed.
    """

    campaign = Campaign(
        name=f"Sequence control {uuid.uuid4()}",
        description="Operator opt-in coverage",
        status=CampaignStatus.DRAFT,
        execution_enabled=False,
        cadence_config=cadence_config,
    )
    db.add(campaign)
    db.flush()
    return campaign


def _sequence_checkbox(body: str) -> str | None:
    """The one rendered ``sequence_enabled`` input tag, or ``None`` if withheld."""

    match = re.search(r"<input[^>]*name=\"sequence_enabled\"[^>]*>", body)
    return match.group(0) if match else None


# ===========================================================================
# A. The writer's contract
#
# Asserted against the reader that has depended on this shape all along, never
# against the raw dictionary alone — agreement between the two is the property
# worth pinning.
# ===========================================================================


def test_writing_the_opt_in_leaves_unrelated_cadence_keys_untouched(
    db_session: Session,
) -> None:
    """The column belongs to the Campaign; this module claims one key of it.

    A writer that replaced the whole object would silently discard anything
    stored beside the sequence block, and nothing in the product reads those
    other keys yet — so the loss would surface much later, as missing data
    nobody could account for.
    """

    campaign = _campaign(db_session, cadence_config={"other": {"x": 1}, "note": "keep me"})

    written = with_campaign_opt_in(campaign, enabled=True)

    assert written["other"] == {"x": 1}
    assert written["note"] == "keep me"
    assert written[CADENCE_KEY] == {"enabled": True}


def test_writing_the_opt_in_leaves_unrelated_keys_inside_the_sequence_block(
    db_session: Session,
) -> None:
    """The block is shared with the cadence override, which must not be erased.

    ``elapsed_days`` lives in the same block and is the one setting an operator
    cannot re-enter from the product. Toggling the switch is not permission to
    drop it.
    """

    campaign = _campaign(
        db_session,
        cadence_config={CADENCE_KEY: {"enabled": False, "elapsed_days": [0, 2, 4, 6, 8, 10, 12]}},
    )

    written = with_campaign_opt_in(campaign, enabled=True)

    assert written[CADENCE_KEY]["elapsed_days"] == [0, 2, 4, 6, 8, 10, 12]
    assert written[CADENCE_KEY]["enabled"] is True


def test_the_written_flag_is_a_real_bool_that_reads_back_as_opted_in(
    db_session: Session,
) -> None:
    """Writer and reader must agree, and the reader tests identity, not truth.

    The assertion is ``is True`` rather than a truthiness check on purpose: a
    truthy assertion here would pass for the exact value that breaks the
    feature.
    """

    campaign = _campaign(db_session)

    campaign.cadence_config = with_campaign_opt_in(campaign, enabled=True)
    db_session.flush()

    stored = sequence_settings(campaign)["enabled"]
    assert stored is True
    assert campaign_opted_in(campaign) is True


def test_the_string_a_checkbox_actually_posts_would_not_have_opted_the_campaign_in(
    db_session: Session,
) -> None:
    """Anti-vacuity for the test above, and the defect it was written against.

    An HTML checkbox posts ``"on"``. Stored unchanged that is a value which
    looks correct in the database, passes every truthiness check, and reads back
    as *not opted in* — a switch that saves without error and does nothing. This
    test fails the moment ``campaign_opted_in`` is relaxed to a truthiness test,
    which is what would make the ``is True`` assertion above meaningless.
    """

    campaign = _campaign(db_session, cadence_config={CADENCE_KEY: {"enabled": "on"}})

    assert campaign_opted_in(campaign) is False

    # And the writer coerces, so the value the route hands it can never land
    # in the column in that shape.
    written = with_campaign_opt_in(campaign, enabled=bool("on"))
    assert written[CADENCE_KEY]["enabled"] is True


@pytest.mark.parametrize(
    ("label", "existing"),
    [
        ("null column", None),
        ("empty object", {}),
        ("empty sequence block", {CADENCE_KEY: {}}),
        ("not an object at all", "garbage"),
        ("sequence block is not an object", {CADENCE_KEY: "garbage"}),
    ],
)
def test_the_writer_accepts_every_shape_the_column_is_known_to_hold(
    db_session: Session, label: str, existing: Any
) -> None:
    """Existing rows predate this writer, so it meets them as they are.

    ``cadence_config`` has been nullable and unvalidated since Phase 2. Raising
    on a malformed value would break the settings page for exactly the Campaign
    an operator is trying to repair, so malformed is treated as absent — the
    same reading :func:`sequence_settings` has always taken.

    Both directions are asserted for each shape. Asserting only the enabled case
    would pass just as well against a writer that hard-coded ``True``.
    """

    campaign = _campaign(db_session, cadence_config=existing)

    campaign.cadence_config = with_campaign_opt_in(campaign, enabled=True)
    db_session.flush()
    assert campaign_opted_in(campaign) is True, f"opting in failed for {label}"

    campaign.cadence_config = with_campaign_opt_in(campaign, enabled=False)
    db_session.flush()
    assert campaign_opted_in(campaign) is False, f"opting out failed for {label}"


@pytest.mark.parametrize(
    ("existing", "quarantine_key", "expected"),
    [
        ("garbage", UNREADABLE_CONFIG_KEY, "garbage"),
        ({CADENCE_KEY: "garbage"}, UNREADABLE_SEQUENCE_KEY, "garbage"),
        ({CADENCE_KEY: [1, 2, 3]}, UNREADABLE_SEQUENCE_KEY, [1, 2, 3]),
    ],
)
def test_a_value_the_writer_cannot_read_is_kept_rather_than_overwritten(
    db_session: Session, existing: Any, quarantine_key: str, expected: Any
) -> None:
    """Treating malformed as absent must not quietly become deleting it.

    The reader can afford to read past something it does not understand. A
    writer cannot afford to drop it: whatever is in that column was put there by
    somebody, and this control is not the thing that should decide it was
    worthless. Refusing instead would be worse again -- this function is on the
    path of every settings save, so it would block renaming the campaign too,
    breaking the settings page for exactly the campaign being repaired.
    """

    campaign = _campaign(db_session, cadence_config=existing)

    written = with_campaign_opt_in(campaign, enabled=True)

    assert written[quarantine_key] == expected, "the unreadable value was not preserved"
    assert written[CADENCE_KEY]["enabled"] is True, "the switch did not take effect"


def test_quarantining_happens_once_and_does_not_accumulate(db_session: Session) -> None:
    """The next write sees a well-formed object and has nothing to move aside."""

    campaign = _campaign(db_session, cadence_config="garbage")

    campaign.cadence_config = with_campaign_opt_in(campaign, enabled=True)
    db_session.flush()
    first = dict(campaign.cadence_config or {})

    campaign.cadence_config = with_campaign_opt_in(campaign, enabled=False)
    db_session.flush()
    second = dict(campaign.cadence_config or {})

    assert first[UNREADABLE_CONFIG_KEY] == "garbage"
    assert second[UNREADABLE_CONFIG_KEY] == "garbage", "the preserved value was lost on rewrite"
    assert [key for key in second if key.startswith("_unreadable")] == [UNREADABLE_CONFIG_KEY]
    assert campaign_opted_in(campaign) is False


# ===========================================================================
# B. The settings form, over HTTP
# ===========================================================================


def test_the_settings_form_saves_the_sequence_switch_both_ways(
    client: TestClient, db_session: Session
) -> None:
    """An unchecked box is absent from the body, which is how "off" arrives.

    Both directions in one test because they are one behaviour: a form that can
    only turn the switch on is a trap, since the operator has no other way to
    turn it off and the page will keep reporting it as on.
    """

    campaign = _campaign(db_session)
    db_session.commit()

    response = client.post(
        f"/app/campaigns/{campaign.id}/setup",
        data={"name": campaign.name, "sequence_enabled": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.expire_all()
    assert campaign_opted_in(campaign) is True

    response = client.post(
        f"/app/campaigns/{campaign.id}/setup",
        data={"name": campaign.name},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.expire_all()
    assert campaign_opted_in(campaign) is False


@pytest.mark.parametrize("opted_in", [True, False])
def test_the_edit_form_renders_the_checkbox_in_its_current_state(
    client: TestClient, db_session: Session, opted_in: bool
) -> None:
    """The form is the operator's only view of this setting.

    A box that always renders unchecked reads as "sequences are off" for a
    Campaign that is generating seven messages per contact, and saving the page
    for any other reason would then make that false statement true.
    """

    campaign = _campaign(db_session, cadence_config={CADENCE_KEY: {"enabled": opted_in}})
    db_session.commit()

    response = client.get(f"/app/campaigns/{campaign.id}/setup")
    assert response.status_code == 200
    checkbox = _sequence_checkbox(response.text)
    assert checkbox is not None
    assert ("checked" in checkbox) is opted_in, checkbox


def test_the_edit_form_states_the_fixed_cadence_it_cannot_change(
    client: TestClient, db_session: Session
) -> None:
    """Seven messages over five weeks is the commitment being made.

    The cadence is not editable here, so the page has to say what it is; an
    operator ticking a box labelled only "seven messages" has not been told that
    the last one lands thirty-five days later.
    """

    campaign = _campaign(db_session)
    db_session.commit()

    body = client.get(f"/app/campaigns/{campaign.id}/setup").text
    assert ", ".join(str(day) for day in RATIFIED_LADDER) in body


def test_saving_the_sequence_switch_bumps_the_settings_version_and_is_audited(
    client: TestClient, db_session: Session
) -> None:
    """This is a settings change and must be indistinguishable from one.

    The write goes through ``update_campaign`` rather than assigning the column,
    precisely so it inherits the version bump and the audit event that function
    owns. Assigning the JSON from the handler would have skipped both and left
    no record that anybody turned the workflow on.
    """

    campaign = _campaign(db_session)
    before = campaign.settings_version
    db_session.commit()

    client.post(
        f"/app/campaigns/{campaign.id}/setup",
        data={"name": campaign.name, "sequence_enabled": "on"},
        follow_redirects=False,
    )
    db_session.expire_all()

    assert campaign.settings_version > before
    events = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.entity_type == "campaign",
            AuditEvent.entity_id == str(campaign.id),
            AuditEvent.action == "campaign.updated",
        )
    ).all()
    assert len(events) == 1
    assert "cadence_config" in events[0].context["fields_changed"]


# ===========================================================================
# C. The deployment flag, and the regression it creates
# ===========================================================================


def test_the_checkbox_is_withheld_where_sequences_cannot_be_generated(
    client_without_sequences: TestClient, db_session: Session
) -> None:
    """Offering a switch for a feature the deployment cannot run is a lie."""

    campaign = _campaign(db_session)
    db_session.commit()

    body = client_without_sequences.get(f"/app/campaigns/{campaign.id}/setup").text
    assert _sequence_checkbox(body) is None
    assert "Prepare seven emails per person" not in body


def test_saving_an_unrelated_edit_cannot_opt_a_campaign_out_of_a_control_it_was_never_shown(
    client_without_sequences: TestClient, db_session: Session
) -> None:
    """Absent means unchanged when the control was never offered.

    The complement of "an unchecked box is absent from the body": absence only
    means *off* when the box was rendered. Where it was withheld, reading the
    field unconditionally would silently opt a Campaign out — with a
    settings-version bump and an audit event claiming an operator decision —
    every time somebody corrected a typo in its name.
    """

    campaign = _campaign(db_session, cadence_config={CADENCE_KEY: {"enabled": True}})
    db_session.commit()

    response = client_without_sequences.post(
        f"/app/campaigns/{campaign.id}/setup",
        data={"name": "Renamed, nothing else meant", "description": "still opted in"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.expire_all()

    assert campaign.name == "Renamed, nothing else meant"
    assert campaign_opted_in(campaign) is True


# ===========================================================================
# D. What the existing rows and the constant still say
# ===========================================================================


def test_a_campaign_that_predates_the_column_still_renders_and_is_not_opted_in(
    client: TestClient, db_session: Session
) -> None:
    """``cadence_config`` is NULL on every Campaign created before SEQ-001.

    Opt-in defaults off, so turning the deployment flag on must not change what
    any existing Campaign produces — and must not make its settings page fail to
    load either.
    """

    campaign = _campaign(db_session, cadence_config=None)
    db_session.commit()
    assert campaign.cadence_config is None

    response = client.get(f"/app/campaigns/{campaign.id}/setup")
    assert response.status_code == 200
    checkbox = _sequence_checkbox(response.text)
    assert checkbox is not None
    assert "checked" not in checkbox
    assert campaign_opted_in(campaign) is False


def test_the_fixed_cadence_is_still_the_ratified_seven_step_ladder() -> None:
    """The switch commits a Campaign to this ladder and to no other.

    Kept as a constant test rather than only a rendered one because the number
    of positions and the number of planned days have to stay equal: a ladder of
    six days would leave the seventh message with no planned timing at all.
    """

    assert DEFAULT_ELAPSED_DAYS == RATIFIED_LADDER
    assert SEQUENCE_LENGTH == 7
    assert len(DEFAULT_ELAPSED_DAYS) == SEQUENCE_LENGTH
