"""The model fallback behind the logo.dev lookup.

Two things are being tested, and only one of them is "does it work".

The first is that it *reaches* the companies it exists for. logo.dev's Search
Brands is a brand-name matcher: it answers well for a company whose domain spells
its name and returns nothing for the rest, and that silence is the single largest
reason a captured person never becomes a Contact. The fallback earns its place
only if it resolves the cases the matcher could not.

The second, and the larger half of this file, is that it stays inside its lane. A
model that can name a domain can also name a confident wrong one, so the tests
assert the refusals: it never confirms, never overrules an approved mapping, never
breaks a tie, never accepts a directory page, and never runs where the
deterministic path already succeeded.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from app.core.config import get_settings
from app.models.company import Company
from app.models.enums import (
    DomainResolutionState,
    EnrichmentLookupStatus,
)
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.services.enrichment import logodev, model_domain
from app.services.resolution import policy
from app.services.resolution import service as resolution_service
from app.services.thinking.contracts import (
    ThinkingMalformed,
    ThinkingRequest,
    ThinkingResult,
    ThinkingTimeout,
    ThinkingUnavailable,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests import capture_factory

ACTOR = "model-fallback-test"


class ScriptedThinker:
    """A thinker that answers from a script. Never touches a subprocess."""

    name = "scripted-thinker"
    version = "test/1"

    def __init__(self, *answers: Any) -> None:
        self._answers = list(answers)
        self.requests: list[ThinkingRequest] = []

    def think(self, request: ThinkingRequest) -> ThinkingResult:
        self.requests.append(request)
        answer = self._answers.pop(0) if self._answers else {"domain": None}
        if isinstance(answer, Exception):
            raise answer
        return ThinkingResult(
            payload=answer,
            producer=self.name,
            producer_version=self.version,
            duration_seconds=0.01,
            raw=json.dumps(answer),
        )


def _provider(body: str = "[]") -> resolution_service.ProviderAccess:
    """logo.dev with a stubbed transport. Defaults to finding nothing."""

    def transport(url: str, headers: Any, timeout: float) -> logodev.RawResponse:
        return logodev.RawResponse(status_code=200, body=body)

    return resolution_service.ProviderAccess(api_key="test-key", transport=transport)


def _model(thinker: ScriptedThinker) -> resolution_service.ModelAccess:
    return resolution_service.ModelAccess(thinker_factory=lambda: thinker, timeout=5.0)


# ---------------------------------------------------------------------------
# Parsing the model's answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("example.com", "example.com"),
        ("https://example.com/about", "example.com"),
        ("www.Example.COM", "example.com"),
        ("http://www.example.co.uk/", "example.co.uk"),
        ("example.com.", "example.com"),
        ("'example.com'", "example.com"),
        ("hello@example.com", "example.com"),
        ("example.com:8443", "example.com"),
    ],
)
def test_the_shapes_a_model_returns_a_domain_in(given: str, expected: str) -> None:
    """Tolerant on the way in, exact on the way out.

    Asking for a bare domain does not reliably get one — a scheme, a ``www.``, a
    trailing slash or a whole address all turn up — and rejecting those would
    throw away correct answers over formatting.
    """

    assert model_domain.normalize_domain(given) == expected


@pytest.mark.parametrize(
    "given",
    ["", "   ", "not a domain", "localhost", "example", "..", "http://", 42, None, ["example.com"]],
)
def test_what_is_not_a_domain_is_refused_rather_than_half_parsed(given: object) -> None:
    """A half-parsed domain is worse than none: it would be stored as a decision."""

    assert model_domain.normalize_domain(given) is None


def test_a_declined_answer_is_an_answer_not_a_failure() -> None:
    """The distinction this module exists to preserve.

    ``find_domain.py`` — the script this replaces — exits 1 both when the CLI
    fails and when it cannot name a domain, so the two are indistinguishable. They
    call for opposite responses: one is worth retrying, the other never is.
    """

    thinker = ScriptedThinker({"domain": None, "reason": "three companies share this name"})
    result = model_domain.look_up(company_name="Acme", thinker=thinker)

    assert result.status is EnrichmentLookupStatus.NO_MATCH
    assert not result.found
    assert not result.retryable, "asking again without new information gets the same answer"
    assert result.reason is not None
    assert "three companies" in result.reason


def test_an_answer_that_is_not_a_domain_is_retryable_but_a_refusal_is_not() -> None:
    thinker = ScriptedThinker({"domain": "see the website"})
    unusable = model_domain.look_up(company_name="Acme", thinker=thinker)
    assert unusable.status is EnrichmentLookupStatus.MALFORMED
    assert unusable.retryable


def test_a_missing_domain_key_is_malformed_but_an_explicit_null_is_not() -> None:
    """``{"domain": null}`` is a deliberate answer; ``{}`` is a broken one."""

    assert (
        model_domain.look_up(company_name="Acme", thinker=ScriptedThinker({})).status
        is EnrichmentLookupStatus.MALFORMED
    )
    assert (
        model_domain.look_up(company_name="Acme", thinker=ScriptedThinker({"domain": None})).status
        is EnrichmentLookupStatus.NO_MATCH
    )


@pytest.mark.parametrize(
    ("raised", "expected", "retryable"),
    [
        (ThinkingTimeout("timed out"), EnrichmentLookupStatus.API_UNAVAILABLE, True),
        (ThinkingUnavailable("no executable"), EnrichmentLookupStatus.API_UNAVAILABLE, False),
        (ThinkingMalformed("not json"), EnrichmentLookupStatus.MALFORMED, True),
    ],
)
def test_every_seam_failure_becomes_a_status_not_an_exception(
    raised: Exception, expected: EnrichmentLookupStatus, retryable: bool
) -> None:
    """One company's failed lookup must not end a pass over fifty others."""

    result = model_domain.look_up(company_name="Acme", thinker=ScriptedThinker(raised))
    assert result.status is expected
    assert result.retryable is retryable


def test_the_location_hint_reaches_the_prompt() -> None:
    """The hint has been stored on every enrichment record all along, unused.

    It is the disambiguator that matters in practice: a captured company name is
    routinely shared by unrelated companies in different countries.
    """

    thinker = ScriptedThinker({"domain": "quanthealth.ai"})
    model_domain.look_up(
        company_name="QuantHealth", thinker=thinker, location_hint="Tel Aviv, Israel"
    )

    prompt = thinker.requests[0].prompt
    assert "Identifiers: Tel Aviv, Israel" in prompt
    assert "Company: QuantHealth" in prompt


def test_the_lookup_is_allowed_to_search_and_nothing_else() -> None:
    """Without search this asks a model to recall a domain, which invents them.

    Asserted on the request rather than through behaviour, because a scripted
    thinker answers identically either way — the permission is the thing being
    tested, and only the call carries it.
    """

    thinker = ScriptedThinker({"domain": "acme.com"})
    model_domain.look_up(company_name="Acme", thinker=thinker, timeout_seconds=42.0)

    request = thinker.requests[0]
    assert request.allowed_tools == ("WebSearch",)
    assert request.timeout_seconds == 42.0


def test_the_policy_and_the_lookup_agree_on_the_provider_name() -> None:
    """The string is duplicated so the pure policy need not import the seam.

    Duplication is the right trade there — the alternative inverts the dependency
    and pulls a subprocess-capable module into one whose contract is that it
    touches nothing — but a duplicated constant needs a test or it drifts.
    """

    assert policy.MODEL_PROVIDER == model_domain.PROVIDER


# ---------------------------------------------------------------------------
# The policy: what a model answer is and is not allowed to do
# ---------------------------------------------------------------------------


def _evidence(**kwargs: Any) -> policy.ResolutionEvidence:
    base: dict[str, Any] = {
        "company_name": "QuantHealth",
        "normalized_company_name": "quanthealth",
        "lookup_status": EnrichmentLookupStatus.NO_MATCH,
    }
    base.update(kwargs)
    return policy.ResolutionEvidence(**base)


def test_a_model_answer_is_provisional_and_can_never_be_confirmed() -> None:
    """The ceiling that makes the whole fallback safe to switch on.

    Provisional opens company research and nothing else, so a wrong answer costs
    one wasted crawl — not an email to a stranger at the wrong company.
    """

    decision = policy.evaluate(
        _evidence(
            model_lookup_status=EnrichmentLookupStatus.OK,
            model_domain="quanthealth.ai",
        )
    )

    assert decision.state is DomainResolutionState.PROVISIONAL
    assert decision.selected_domain == "quanthealth.ai"
    assert decision.provider == policy.MODEL_PROVIDER
    assert policy.WARNING_PROVISIONAL_LIMITS in decision.warnings
    assert policy.WARNING_MODEL_ANSWER_NOT_DETERMINISTIC in decision.warnings


def test_the_waived_alignment_is_recorded_as_waived_not_as_passed() -> None:
    """A reader must not be able to mistake "we did not apply the rule" for "it passed".

    The waiver is the point of the fallback — ``Alphabet`` → ``abc.xyz`` is the
    shape of the problem — but the stored candidate says ``aligned: false``, which
    is the truth, and a reason code says why that was acceptable.
    """

    decision = policy.evaluate(
        _evidence(model_lookup_status=EnrichmentLookupStatus.OK, model_domain="abc.xyz")
    )

    assert decision.state is DomainResolutionState.PROVISIONAL
    assert policy.REASON_MODEL_NAME_ALIGNMENT_WAIVED in decision.reasons
    chosen = decision.candidates[-1]
    assert chosen.domain == "abc.xyz"
    assert chosen.aligned is False
    assert chosen.eligible is True


@pytest.mark.parametrize(
    "domain",
    [
        "linkedin.com",
        "www.linkedin.com",
        "crunchbase.com",
        "facebook.com",
        "wix.com",
        "gmail.com",
        "godaddy.com",
        "github.io",
    ],
)
def test_domain_hygiene_is_not_waived_for_a_model(domain: str) -> None:
    """A model asked for "the official domain" reaches for these readily.

    They are the top search result for a great many companies, and the alignment
    rule — the one thing that is waived — would not have caught them anyway.
    """

    decision = policy.evaluate(
        _evidence(model_lookup_status=EnrichmentLookupStatus.OK, model_domain=domain)
    )

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert policy.REASON_MODEL_DOMAIN_UNSUITABLE in decision.reasons
    assert decision.selected_domain is None


def test_an_approved_mapping_still_wins_without_the_model_being_consulted() -> None:
    """Established evidence short-circuits before any fallback is reached."""

    decision = policy.evaluate(
        _evidence(
            approved_mapping_domains=frozenset({"quanthealth.com"}),
            model_lookup_status=EnrichmentLookupStatus.OK,
            model_domain="somethingelse.ai",
        )
    )

    assert decision.state is DomainResolutionState.CONFIRMED
    assert decision.selected_domain == "quanthealth.com"


def test_the_model_is_not_asked_to_break_a_tie_between_aligned_candidates() -> None:
    """Two sources aligning and disagreeing is where the policy refuses to guess.

    A third opinion there produces a more confident guess, not a better one — so
    the tie stays a tie and an operator decides.
    """

    decision = policy.evaluate(
        _evidence(
            lookup_status=EnrichmentLookupStatus.OK,
            candidates=(
                {"domain": "quanthealth.ai", "name": "QuantHealth", "rank": 1},
                {"domain": "quanthealth.com", "name": "Quant Health", "rank": 2},
            ),
            model_lookup_status=EnrichmentLookupStatus.OK,
            model_domain="quanthealth.ai",
        )
    )

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert policy.REASON_MULTIPLE_ALIGNED_CANDIDATES in decision.reasons
    assert policy.REASON_MODEL_ASSERTED_DOMAIN not in decision.reasons


def test_a_provider_that_succeeded_is_not_second_guessed() -> None:
    decision = policy.evaluate(
        _evidence(
            lookup_status=EnrichmentLookupStatus.OK,
            candidates=({"domain": "quanthealth.ai", "name": "QuantHealth", "rank": 1},),
            model_lookup_status=EnrichmentLookupStatus.OK,
            model_domain="wrongcompany.com",
        )
    )

    assert decision.selected_domain == "quanthealth.ai"
    assert decision.provider != policy.MODEL_PROVIDER


def test_not_asking_the_model_is_reported_as_not_asking() -> None:
    """Distinct from "asked and found nothing", which is a claim about the company."""

    decision = policy.evaluate(_evidence())

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert policy.REASON_MODEL_LOOKUP_NOT_RUN in decision.reasons
    assert policy.REASON_MODEL_NO_ANSWER not in decision.reasons


def test_an_unreachable_model_and_an_unreadable_answer_are_told_apart() -> None:
    """Both retryable, but they call for different things from an operator.

    One means check the CLI; the other means simply ask again.
    """

    unreachable = policy.evaluate(
        _evidence(model_lookup_status=EnrichmentLookupStatus.API_UNAVAILABLE)
    )
    unreadable = policy.evaluate(_evidence(model_lookup_status=EnrichmentLookupStatus.MALFORMED))

    assert policy.REASON_MODEL_UNAVAILABLE in unreachable.reasons
    assert policy.REASON_MODEL_ANSWER_UNUSABLE in unreadable.reasons


def test_every_reason_and_warning_code_has_operator_facing_words() -> None:
    """The UI never invents its own wording, so a code with no sentence renders blank."""

    codes = [
        policy.REASON_MODEL_ASSERTED_DOMAIN,
        policy.REASON_MODEL_EVIDENCE_UNCORROBORATED,
        policy.REASON_MODEL_NAME_ALIGNMENT_WAIVED,
        policy.REASON_MODEL_LOOKUP_NOT_RUN,
        policy.REASON_MODEL_NO_ANSWER,
        policy.REASON_MODEL_UNAVAILABLE,
        policy.REASON_MODEL_ANSWER_UNUSABLE,
        policy.REASON_MODEL_DOMAIN_UNSUITABLE,
    ]
    for code in codes:
        assert policy.REASON_TEXT.get(code), code
    assert policy.WARNING_TEXT.get(policy.WARNING_MODEL_ANSWER_NOT_DETERMINISTIC)


# ---------------------------------------------------------------------------
# End to end, through the resolution service
# ---------------------------------------------------------------------------


def test_a_capture_the_provider_could_not_match_is_rescued(db_session: Session) -> None:
    """The case the fallback exists for, end to end.

    logo.dev returns nothing; the model names the domain; the capture becomes a
    Contact — which is the outcome that was being lost.
    """

    snapshot = capture_factory.salesnav_capture(
        db_session, company_name="QuantHealth", location="Tel Aviv, Israel"
    )
    thinker = ScriptedThinker(
        {"domain": "quanthealth.ai", "source_url": "https://quanthealth.ai/about"}
    )

    outcome = resolution_service.resolve(
        db_session,
        snapshot=snapshot,
        access=_provider(),
        model=_model(thinker),
        actor=ACTOR,
    )

    assert outcome.provider_call_made
    assert outcome.model_call_made
    assert outcome.state is DomainResolutionState.PROVISIONAL
    assert outcome.selected_domain == "quanthealth.ai"
    assert outcome.auto_promoted, "the person should now be a Contact"


def test_the_source_page_the_model_read_is_kept(db_session: Session) -> None:
    """The most useful thing on the record for an operator confirming the domain."""

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    thinker = ScriptedThinker(
        {"domain": "quanthealth.ai", "source_url": "https://quanthealth.ai/about"}
    )

    resolution_service.resolve(
        db_session, snapshot=snapshot, access=_provider(), model=_model(thinker), actor=ACTOR
    )

    record = db_session.scalars(
        select(SalesNavCompanyEnrichment).where(SalesNavCompanyEnrichment.capture_id == snapshot.id)
    ).one()
    assert record.model_domain == "quanthealth.ai"
    assert record.model_source_url == "https://quanthealth.ai/about"
    assert record.model_lookup_status is EnrichmentLookupStatus.OK
    assert record.model_lookup_attempts == 1


def test_a_rejected_model_answer_is_still_recorded(db_session: Session) -> None:
    """What the model got wrong is provenance too.

    Anyone deciding whether to trust this fallback at all needs to see its misses,
    and a rejected answer that vanished would make the feature un-auditable.
    """

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    thinker = ScriptedThinker({"domain": "linkedin.com/company/quanthealth"})

    outcome = resolution_service.resolve(
        db_session, snapshot=snapshot, access=_provider(), model=_model(thinker), actor=ACTOR
    )

    assert outcome.state is DomainResolutionState.UNRESOLVED
    assert not outcome.auto_promoted
    record = db_session.scalars(
        select(SalesNavCompanyEnrichment).where(SalesNavCompanyEnrichment.capture_id == snapshot.id)
    ).one()
    assert record.model_domain == "linkedin.com"


def test_the_model_is_not_called_when_the_switch_is_off(db_session: Session) -> None:
    """Off means the behaviour is exactly what it was before this existed."""

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    thinker = ScriptedThinker({"domain": "quanthealth.ai"})

    outcome = resolution_service.resolve(
        db_session,
        snapshot=snapshot,
        access=_provider(),
        model=resolution_service.ModelAccess(),  # no factory: switched off
        actor=ACTOR,
    )

    assert not outcome.model_call_made
    assert thinker.requests == []
    assert outcome.state is DomainResolutionState.UNRESOLVED


def test_the_model_is_not_called_when_the_provider_already_answered(db_session: Session) -> None:
    """No call is spent where the deterministic path succeeded."""

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    thinker = ScriptedThinker({"domain": "wrongcompany.com"})
    body = json.dumps([{"name": "QuantHealth", "domain": "quanthealth.ai"}])

    outcome = resolution_service.resolve(
        db_session,
        snapshot=snapshot,
        access=_provider(body),
        model=_model(thinker),
        actor=ACTOR,
    )

    assert not outcome.model_call_made
    assert thinker.requests == []
    assert outcome.selected_domain == "quanthealth.ai"


def test_an_established_company_is_not_second_guessed_by_a_model(db_session: Session) -> None:
    """Established evidence resolves before a provider or a model is asked at all."""

    db_session.add(Company(id=uuid.uuid4(), name="QuantHealth", domain="quanthealth.com"))
    db_session.flush()
    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    thinker = ScriptedThinker({"domain": "quanthealth.ai"})

    outcome = resolution_service.resolve(
        db_session, snapshot=snapshot, access=_provider(), model=_model(thinker), actor=ACTOR
    )

    assert not outcome.provider_call_made
    assert not outcome.model_call_made
    assert outcome.state is DomainResolutionState.CONFIRMED
    assert outcome.selected_domain == "quanthealth.com"


def test_one_model_call_per_company_however_often_resolution_runs(db_session: Session) -> None:
    """A model call costs real time; a re-run must not spend a second one."""

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    thinker = ScriptedThinker(
        {"domain": None, "reason": "could not tell"}, {"domain": "quanthealth.ai"}
    )

    for _ in range(3):
        resolution_service.resolve(
            db_session,
            snapshot=snapshot,
            access=_provider(),
            model=_model(thinker),
            actor=ACTOR,
            force=True,
        )

    assert len(thinker.requests) == 1


# ---------------------------------------------------------------------------
# The configuration diagnosis
# ---------------------------------------------------------------------------


def test_readiness_names_every_unmet_precondition_and_where_to_change_it(
    db_session: Session,
) -> None:
    """The gap that made this a support question rather than a self-service fix.

    Four unrelated switches produce the same "not_started · 0 attempt(s)", and
    none of them was named anywhere. "The promotion flag" is not actionable
    without knowing what it is called and where it lives, so each blocker names
    the control and the screen that owns it — or, for the one precondition that
    is genuinely a deployment secret, the environment variable.
    """

    from app.services.resolution import pending

    settings = get_settings()
    readiness = pending.lookup_readiness(db_session, settings)

    assert not readiness.provider_ready
    settings_named = {blocker.setting for blocker in readiness.blockers}
    assert settings_named == {
        "Capture promotion — Admin → Configuration",
        "Automatic company-domain resolution — Admin → Configuration",
        "logo.dev domain lookup — Admin → Configuration",
        "LOGO_DEV_API_KEY=...",
    }
    # Each carries its own sentence too: a bare variable name says what to set and
    # not what it does, which is how a switch gets turned on without being understood.
    assert all(blocker.message for blocker in readiness.blockers)


def test_a_fully_configured_workbench_reports_nothing_missing(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.resolution import pending

    # Settings are frozen by design, so configuration is exercised the way it is
    # actually supplied — through the environment — rather than by reaching past
    # the model that guarantees it cannot change under a running request.
    for flag in (
        "CONTACT_CAPTURE_PROMOTION",
        "AUTOMATIC_COMPANY_DOMAIN_RESOLUTION",
        "SALESNAV_DOMAIN_ENRICHMENT",
        "MODEL_COMPANY_DOMAIN_LOOKUP",
    ):
        monkeypatch.setenv(f"FEATURES__{flag}", "true")
    monkeypatch.setenv("LOGO_DEV_API_KEY", "a-key")
    get_settings.cache_clear()
    try:
        readiness = pending.lookup_readiness(db_session, get_settings())
        assert readiness.provider_ready
        assert readiness.model_ready
        assert readiness.blockers == ()
    finally:
        get_settings.cache_clear()
