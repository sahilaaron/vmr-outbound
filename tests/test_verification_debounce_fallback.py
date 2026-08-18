"""VER-02: DeBounce as the verification fallback behind MillionVerifier.

The claim under test is narrow and easy to get wrong in the flattering
direction: DeBounce exists to *recover contacts MillionVerifier could not
settle*, not to offer a second opinion on ones it did. So the suite is
two-sided throughout. Every case that proves the fallback ran is paired with a
case that proves it did not — after a confirmed VALID, after a confirmed
INVALID, and on the operator-supplied paths that must never reach a provider at
all.

The other half of the file is about not manufacturing verdicts. A fallback
provider is only worth having if it fails honestly: an unreadable reply, a
``success = 0`` envelope, a missing classification and a rejected credential are
all *provider failures*, never a statement about the mailbox. The temptation
they create is real — "Unknown" is a valid DeBounce answer and would absorb all
of them silently — so each is asserted to stay outside the address-evidence
family rather than merely "not valid".
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest
from app.core.config import Settings, get_settings
from app.core.features import FeatureFlags
from app.models.email_evidence import ExactEmailVerification
from app.models.email_verification_studio import VerificationProviderAttempt
from app.models.enums import (
    AgentIdentifier,
    EmailPreciseStatus,
    EmailVerificationResult,
    VerificationFailureClass,
)
from app.models.verification_attempt import VerificationAttempt
from app.models.verification_job import AgentJob
from app.services.agent_studio.email_verification_report import EmailVerificationReportReader
from app.services.verification import fallback as fallback_policy
from app.services.verification import queue as jobs
from app.services.verification import service, studio, waterfall
from app.services.verification.decisions import VerificationDecision, decide
from app.services.verification.policy import get_policy
from app.services.verification.provider import (
    HttpDebounce,
    ProviderResponse,
    ProviderTransientError,
    SimulatedDebounce,
    _as_optional_bool,
    evidence_provider_label,
)
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

POLICY = "ver-1"
KEY = "debounce-secret-value"
EMAIL = "ada.lovelace@kiln.example"


# --------------------------------------------------------------------------
# Transport doubles. The adapter is exercised through its real HTTP seam so the
# request construction, the redaction and the parsing are all under test.
# --------------------------------------------------------------------------


class _Body:
    """Returns one fixed body for any request."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.urls: list[str] = []

    def get(self, url: str, timeout: float) -> str:
        assert timeout > 0
        self.urls.append(url)
        return self.body


class _Status:
    """Raises one fixed HTTP status."""

    def __init__(self, status: int) -> None:
        self.status = status

    def get(self, url: str, timeout: float) -> str:
        raise urllib.error.HTTPError(url, self.status, "boom", {}, None)  # type: ignore[arg-type]


def _debounce(body: str) -> ProviderResponse:
    return HttpDebounce(KEY, transport=_Body(body)).verify(EMAIL)


def _payload(**fields: Any) -> str:
    import json

    return json.dumps({"success": "1", "debounce": {"email": EMAIL, **fields}})


# --------------------------------------------------------------------------
# 6-9. DeBounce classifications map into canonical VMR semantics.
# --------------------------------------------------------------------------


def test_safe_to_send_is_the_only_classification_that_can_reach_valid() -> None:
    response = _debounce(_payload(result="Safe to Send", code="5", role=False))
    mapped = get_policy(get_settings()).map_response(response)

    assert mapped.is_address_evidence
    assert mapped.result is EmailVerificationResult.VALID
    assert mapped.precise is EmailPreciseStatus.VALID


def test_invalid_maps_to_the_invalid_verdict() -> None:
    mapped = get_policy(get_settings()).map_response(
        _debounce(_payload(result="Invalid", code="6"))
    )

    assert mapped.is_address_evidence
    assert mapped.result is EmailVerificationResult.INVALID
    assert mapped.precise is EmailPreciseStatus.INVALID


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("4", EmailPreciseStatus.CATCH_ALL),
        ("3", EmailPreciseStatus.DISPOSABLE),
        ("8", EmailPreciseStatus.ROLE_BASED),
        ("", EmailPreciseStatus.UNKNOWN),
    ],
)
def test_risky_maps_conservatively_and_never_to_an_accepted_address(
    code: str, expected: EmailPreciseStatus
) -> None:
    """Risky is one DeBounce word for several uncertainties. None of them pass.

    The claim is deliberately about the *decision*, not the stored result. A
    role account is recorded as a valid mailbox carrying the role flag, because
    that is the only way this model can say "role account" — and the precise
    status it produces is still one no Campaign Contact advances on.
    """

    mapped = get_policy(get_settings()).map_response(_debounce(_payload(result="Risky", code=code)))

    assert mapped.precise is expected
    assert decide(mapped.precise).accepted is False


def test_unknown_stays_unknown_however_loudly_the_provider_recommends_sending() -> None:
    """``send_transactional`` answers a different question than cold outbound asks."""

    response = _debounce(
        _payload(result="Unknown", code="7", send_transactional="1", reason="Unknown")
    )
    mapped = get_policy(get_settings()).map_response(response)

    assert mapped.result is EmailVerificationResult.UNKNOWN
    assert mapped.precise is EmailPreciseStatus.UNKNOWN
    # The signal is preserved for audit, and consulted by nothing.
    assert response.raw["send_transactional"] == "1"


def test_a_role_address_that_is_safe_to_send_is_a_warning_not_a_pass() -> None:
    mapped = get_policy(get_settings()).map_response(
        _debounce(_payload(result="Safe to Send", code="5", role="true"))
    )

    assert mapped.result is EmailVerificationResult.VALID
    assert mapped.precise is EmailPreciseStatus.ROLE_BASED


# --------------------------------------------------------------------------
# 10-14. Provider failures are never mailbox verdicts.
# --------------------------------------------------------------------------


def test_success_zero_is_a_provider_failure_not_a_verdict() -> None:
    import json

    response = HttpDebounce(KEY, transport=_Body(json.dumps({"success": "0"}))).verify(EMAIL)
    mapped = get_policy(get_settings()).map_response(response)

    assert response.result is None
    assert not mapped.is_address_evidence
    assert mapped.unusable
    assert mapped.precise is EmailPreciseStatus.PROVIDER_ERROR


def test_a_missing_classification_is_unreadable_rather_than_unknown() -> None:
    """The failure mode this asserts against is the quiet one.

    ``unknown`` is a real thing DeBounce says about a mailbox. If "we could not
    read the reply" also became ``unknown``, a parsing regression would look
    exactly like a normal uncertain verdict and would be stored as evidence.
    """

    mapped = get_policy(get_settings()).map_response(_debounce(_payload(reason="Unavailable")))

    assert not mapped.is_address_evidence
    assert mapped.unusable
    assert mapped.result is None


def test_an_unrecognised_classification_is_not_downgraded_into_a_verdict() -> None:
    mapped = get_policy(get_settings()).map_response(
        _debounce(_payload(result="Probably Fine", code="99"))
    )

    assert not mapped.is_address_evidence
    assert mapped.unusable


def test_malformed_json_raises_a_transient_failure_and_stores_nothing() -> None:
    with pytest.raises(ProviderTransientError):
        HttpDebounce(KEY, transport=_Body("{not json")).verify(EMAIL)


@pytest.mark.parametrize(
    ("status", "error", "retryable"),
    [
        (401, "access_rejected", False),
        (402, "insufficient_credits", False),
        (403, "access_rejected", False),
    ],
)
def test_settled_operator_conditions_never_retry(status: int, error: str, retryable: bool) -> None:
    response = HttpDebounce(KEY, transport=_Status(status)).verify(EMAIL)
    mapped = get_policy(get_settings()).map_response(response)

    assert response.error == error
    assert mapped.retryable is retryable
    assert not mapped.is_address_evidence


def test_a_rate_limit_is_bounded_and_retriable() -> None:
    with pytest.raises(ProviderTransientError) as raised:
        HttpDebounce(KEY, transport=_Status(429)).verify(EMAIL)

    assert raised.value.condition == "rate_limit"


def test_a_provider_five_hundred_is_transient_not_permanent() -> None:
    with pytest.raises(ProviderTransientError) as raised:
        HttpDebounce(KEY, transport=_Status(503)).verify(EMAIL)

    assert raised.value.condition == "provider_5xx"


# --------------------------------------------------------------------------
# 21. The credential travels in the query string and must escape nowhere.
# --------------------------------------------------------------------------


def test_the_api_key_reaches_no_diagnostic_surface() -> None:
    transport = _Body(_payload(result="Safe to Send", code="5", reason=f"issued to {KEY}"))
    provider = HttpDebounce(KEY, transport=transport)
    response = provider.verify(EMAIL)

    # It is genuinely sent — DeBounce authenticates this way — and genuinely
    # absent from every surface that is stored, rendered or raised.
    assert KEY in transport.urls[0]
    assert KEY not in provider.redacted_url(EMAIL)
    assert KEY not in str(response.raw)
    assert KEY not in str(response.subresult)
    assert KEY not in str(response.error)


def test_a_transport_failure_message_carries_no_credential() -> None:
    class _Leaky:
        def get(self, url: str, timeout: float) -> str:
            raise OSError(f"connect failed for {url}")

    with pytest.raises(ProviderTransientError) as raised:
        HttpDebounce(KEY, transport=_Leaky()).verify(EMAIL)

    assert KEY not in str(raised.value)


# --------------------------------------------------------------------------
# The centralized fallback rule itself.
# --------------------------------------------------------------------------


def _outcome(
    precise: EmailPreciseStatus,
    *,
    failure_class: VerificationFailureClass = VerificationFailureClass.NONE,
    condition: str | None = None,
) -> service.VerificationOutcome:
    return service.VerificationOutcome(
        email=EMAIL,
        precise=precise,
        result=None,
        evidence=None,
        reused=False,
        provider_called=True,
        provider_label="millionverifier",
        failure_class=failure_class,
        policy_version=POLICY,
        condition=condition,
    )


@pytest.mark.parametrize(
    "precise",
    [
        EmailPreciseStatus.VALID,
        EmailPreciseStatus.INVALID,
        EmailPreciseStatus.DISPOSABLE,
        EmailPreciseStatus.ROLE_BASED,
    ],
)
def test_a_settled_verdict_is_authoritative_whatever_it_says(
    precise: EmailPreciseStatus,
) -> None:
    """Including INVALID. Asking a second vendor to disagree is not a fallback."""

    assessment = fallback_policy.assess(_outcome(precise))

    assert assessment.authoritative
    assert not assessment.fallback_eligible


@pytest.mark.parametrize(
    ("precise", "failure_class", "condition", "expected"),
    [
        (
            EmailPreciseStatus.CATCH_ALL,
            VerificationFailureClass.NONE,
            "unresolved_result",
            fallback_policy.ProviderCondition.UNRESOLVED_RESULT,
        ),
        (
            EmailPreciseStatus.UNKNOWN,
            VerificationFailureClass.NONE,
            "unresolved_result",
            fallback_policy.ProviderCondition.UNRESOLVED_RESULT,
        ),
        (
            EmailPreciseStatus.PROVIDER_ERROR,
            VerificationFailureClass.TRANSIENT_PROVIDER,
            "transport_failure",
            fallback_policy.ProviderCondition.TRANSPORT_FAILURE,
        ),
        (
            EmailPreciseStatus.PROVIDER_ERROR,
            VerificationFailureClass.TRANSIENT_PROVIDER,
            "throttled",
            fallback_policy.ProviderCondition.THROTTLED,
        ),
        (
            EmailPreciseStatus.PROVIDER_ERROR,
            VerificationFailureClass.TRANSIENT_PROVIDER,
            "unusable_response",
            fallback_policy.ProviderCondition.UNUSABLE_RESPONSE,
        ),
        (
            EmailPreciseStatus.PROVIDER_ERROR,
            VerificationFailureClass.PERMANENT_PROVIDER,
            "access_rejected",
            fallback_policy.ProviderCondition.ACCESS_REJECTED,
        ),
        (
            EmailPreciseStatus.INSUFFICIENT_CREDITS,
            VerificationFailureClass.INSUFFICIENT_CREDITS,
            "exhausted_credits",
            fallback_policy.ProviderCondition.EXHAUSTED_CREDITS,
        ),
    ],
)
def test_every_inability_to_answer_is_fallback_eligible(
    precise: EmailPreciseStatus,
    failure_class: VerificationFailureClass,
    condition: str,
    expected: fallback_policy.ProviderCondition,
) -> None:
    assessment = fallback_policy.assess(
        _outcome(precise, failure_class=failure_class, condition=condition)
    )

    assert assessment.condition is expected
    assert assessment.fallback_eligible
    assert not assessment.authoritative


def test_a_job_with_no_address_never_falls_back() -> None:
    assessment = fallback_policy.assess(
        _outcome(
            EmailPreciseStatus.PROVIDER_ERROR,
            failure_class=VerificationFailureClass.INVALID_INPUT,
            condition="not_attempted",
        )
    )

    assert not assessment.fallback_eligible


# --------------------------------------------------------------------------
# Traversal. Fakes are installed at the registry seam so the traversal, the
# persistence and the reuse rules are all the real ones.
# --------------------------------------------------------------------------


class _Fake:
    """A provider that replays a script and counts what it was actually asked."""

    def __init__(self, name: str, script: list[Any]) -> None:
        self.name = name
        self.simulated = False
        self.script = list(script)
        self.calls = 0

    def verify(self, email: str) -> ProviderResponse:
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, ProviderResponse)
        return item

    def redact(self, text: str) -> str:
        return text


def _install(monkeypatch: pytest.MonkeyPatch, **providers: Any) -> None:
    def factory(provider_id: str, **_: Any) -> Any:
        if provider_id not in providers:
            raise AssertionError(f"{provider_id} was built when it should not have been")
        return providers[provider_id]

    monkeypatch.setattr(waterfall, "build_provider_by_id", factory)


def _settings(*, debounce: bool = True) -> Settings:
    return Settings(
        millionverifier_api_key="mv-key",
        debounce_api_key=KEY if debounce else None,
        features=FeatureFlags(debounce=debounce),
    )


def _mv(result: str, **fields: Any) -> ProviderResponse:
    codes = {"ok": 1, "catch_all": 2, "unknown": 3, "disposable": 5, "invalid": 6}
    return ProviderResponse(
        email=EMAIL, result=result, resultcode=codes[result], raw={"result": result}, **fields
    )


def _claim(session: Session, email: str = EMAIL) -> Any:
    jobs.enqueue_verification(session, email=email, policy_version=POLICY, max_attempts=4)
    claimed = jobs.claim_next_job(session, worker_id="ver02", lease_seconds=60)
    assert claimed is not None
    return claimed


def _run(
    session: Session,
    settings: Settings,
    email: str = EMAIL,
    job: Any | None = None,
) -> waterfall.WaterfallOutcome:
    claimed = job if job is not None else _claim(session, email)
    return waterfall.verify(session, claimed, settings=settings, policy=get_policy(settings))


def test_a_valid_primary_result_never_reaches_the_fallback(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = _Fake("millionverifier", [_mv("ok")])
    fallback = _Fake("debounce", [_mv("invalid")])
    _install(monkeypatch, millionverifier=primary, debounce=fallback)

    outcome = _run(db_session, _settings())

    assert primary.calls == 1
    assert fallback.calls == 0
    assert outcome.providers_attempted == ("millionverifier",)
    assert outcome.fallback_used is False
    assert outcome.outcome.precise is EmailPreciseStatus.VALID


def test_an_authoritative_invalid_is_never_second_guessed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expensive mistake this prevents: buying a credit to shop for a yes."""

    primary = _Fake("millionverifier", [_mv("invalid")])
    fallback = _Fake("debounce", [_mv("ok")])
    _install(monkeypatch, millionverifier=primary, debounce=fallback)

    outcome = _run(db_session, _settings())

    assert fallback.calls == 0
    assert outcome.outcome.precise is EmailPreciseStatus.INVALID
    assert outcome.fallback_used is False


@pytest.mark.parametrize(
    ("primary_script", "condition"),
    [
        ([ProviderTransientError("timed out")], "transport_failure"),
        ([ProviderTransientError("HTTP 429", condition="rate_limit")], "throttled"),
        (
            [
                ProviderResponse(
                    email=EMAIL, result=None, resultcode=None, error="insufficient_credits"
                )
            ],
            "exhausted_credits",
        ),
        (
            [ProviderResponse(email=EMAIL, result=None, resultcode=None, error="access_rejected")],
            "access_rejected",
        ),
        ([_mv("catch_all")], "unresolved_result"),
        ([_mv("unknown")], "unresolved_result"),
    ],
)
def test_the_fallback_runs_exactly_once_when_the_primary_cannot_answer(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    primary_script: list[Any],
    condition: str,
) -> None:
    primary = _Fake("millionverifier", primary_script)
    fallback = _Fake("debounce", [_mv("ok")])
    _install(monkeypatch, millionverifier=primary, debounce=fallback)

    outcome = _run(db_session, _settings())

    assert primary.calls == 1
    assert fallback.calls == 1
    assert outcome.providers_attempted == ("millionverifier", "debounce")
    assert outcome.fallback_used is True
    assert outcome.fallback_condition == condition
    assert outcome.fallback_reason


def test_an_unreadable_primary_reply_falls_back_without_recording_a_verdict(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = _Fake(
        "millionverifier",
        [ProviderResponse(email=EMAIL, result="something-new", resultcode=None)],
    )
    fallback = _Fake("debounce", [_mv("ok")])
    _install(monkeypatch, millionverifier=primary, debounce=fallback)

    outcome = _run(db_session, _settings())

    assert outcome.fallback_condition == "unusable_response"
    rows = db_session.scalars(
        select(ExactEmailVerification).where(ExactEmailVerification.email == EMAIL)
    ).all()
    # Exactly one evidence row, and it is the fallback's — the unreadable primary
    # reply contributed no evidence at all.
    assert [row.provider for row in rows] == ["debounce"]


# --------------------------------------------------------------------------
# 15-16, 22. Provenance is durable and readable.
# --------------------------------------------------------------------------


def test_provider_and_fallback_provenance_survive_in_the_database(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = _Fake("millionverifier", [_mv("catch_all")])
    fallback = _Fake("debounce", [_mv("ok")])
    _install(monkeypatch, millionverifier=primary, debounce=fallback)

    outcome = _run(db_session, _settings())
    db_session.flush()

    steps = db_session.scalars(
        select(VerificationProviderAttempt).order_by(VerificationProviderAttempt.provider_order)
    ).all()
    assert [step.provider_id for step in steps] == ["millionverifier", "debounce"]
    assert steps[0].precise_status == EmailPreciseStatus.CATCH_ALL.value
    # Why the fallback ran is stored, not merely inferable.
    assert steps[0].error_summary is not None
    assert steps[0].error_summary.startswith("unresolved_result:")
    assert steps[1].error_summary is None or not steps[1].error_summary.startswith(
        "unresolved_result:"
    )

    accepted = outcome.outcome.evidence
    assert accepted is not None
    assert accepted.provider == "debounce"
    assert steps[1].verification_id == accepted.id
    assert steps[1].started_at is not None and steps[1].finished_at is not None

    # And the aggregate attempt names the traversal rather than one vendor.
    aggregate = db_session.scalars(select(VerificationAttempt)).one()
    assert aggregate.provider == "millionverifier -> debounce"


def test_the_admin_report_names_the_deciding_provider_and_the_fallback_reason(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = _Fake("millionverifier", [_mv("unknown")])
    fallback = _Fake("debounce", [_mv("ok")])
    _install(monkeypatch, millionverifier=primary, debounce=fallback)

    claimed = _claim(db_session)
    settings = _settings()
    waterfall.verify(db_session, claimed, settings=settings, policy=get_policy(settings))
    db_session.flush()

    report = EmailVerificationReportReader(db_session).read(
        claimed.id, AgentIdentifier.VERIFICATION
    )
    assert report is not None
    assert report.final_provider == "debounce"
    assert report.fallback_used is True
    assert report.fallback_reason is not None
    assert "unresolved_result" in report.fallback_reason
    assert [step.provider_id for step in report.provider_steps] == [
        "millionverifier",
        "debounce",
    ]


def test_no_credential_reaches_durable_verification_text(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real adapter is used here, echoing its own key back in the payload."""

    echoing = HttpDebounce(
        KEY,
        transport=_Body(_payload(result="Safe to Send", code="5", reason=f"key {KEY} accepted")),
    )
    primary = _Fake("millionverifier", [_mv("catch_all")])
    _install(monkeypatch, millionverifier=primary, debounce=echoing)

    _run(db_session, _settings())
    db_session.flush()

    evidence = db_session.scalars(select(ExactEmailVerification)).all()
    steps = db_session.scalars(select(VerificationProviderAttempt)).all()
    attempts = db_session.scalars(select(VerificationAttempt)).all()
    durable = " ".join(
        [str(row.raw_response) + str(row.subresult) + str(row.reason) for row in evidence]
        + [str(step.error_summary) for step in steps]
        + [str(item.error_summary) for item in attempts]
    )
    assert KEY not in durable


# --------------------------------------------------------------------------
# 19. Restart safety: a durable fallback result is never bought twice.
# --------------------------------------------------------------------------


def test_a_retry_after_a_completed_fallback_spends_no_second_credit(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scenario: MV fails, DeBounce answers, the worker dies before finishing.

    On the next pass the traversal must re-read the DeBounce row it already paid
    for. Without that, every restart in a failing-primary window buys the whole
    fallback set again.
    """

    primary = _Fake("millionverifier", [ProviderTransientError("timed out")])
    fallback = _Fake("debounce", [_mv("ok")])
    _install(monkeypatch, millionverifier=primary, debounce=fallback)
    settings = _settings()

    job = _claim(db_session)
    first = _run(db_session, settings, job=job)
    db_session.flush()
    assert fallback.calls == 1
    assert first.outcome.evidence is not None

    # The same job runs again, exactly as a reclaimed lease would replay it.
    second = _run(db_session, settings, job=job)
    db_session.flush()

    assert fallback.calls == 1, "the fallback was charged for a result already on file"
    assert second.outcome.reused is True
    assert second.outcome.precise is EmailPreciseStatus.VALID
    assert second.outcome.evidence is not None
    assert second.outcome.evidence.id == first.outcome.evidence.id


def test_the_fallback_never_answers_from_the_result_that_summoned_it(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse must be per-provider for a fallback step, or it can never run.

    A fallback step allowed to reuse *any* fresh evidence would immediately find
    the primary's catch-all — the very result that made it eligible — and report
    it as its own answer while never calling anybody.
    """

    primary = _Fake("millionverifier", [_mv("catch_all")])
    fallback = _Fake("debounce", [_mv("ok")])
    _install(monkeypatch, millionverifier=primary, debounce=fallback)

    outcome = _run(db_session, _settings())

    assert fallback.calls == 1
    assert outcome.outcome.provider_label == "debounce"
    assert outcome.outcome.reused is False


# --------------------------------------------------------------------------
# 20. Unconfigured is genuinely inert.
# --------------------------------------------------------------------------


def test_without_configuration_the_traversal_is_millionverifier_only(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = _Fake("millionverifier", [_mv("catch_all")])
    # DeBounce is deliberately absent from the factory: building it is a failure.
    _install(monkeypatch, millionverifier=primary)

    outcome = _run(db_session, _settings(debounce=False))

    assert outcome.providers_attempted == ("millionverifier",)
    assert outcome.fallback_used is False
    assert outcome.outcome.precise is EmailPreciseStatus.CATCH_ALL


def test_the_key_alone_does_not_authorize_spending_it(db_session: Session) -> None:
    keyed_but_off = Settings(debounce_api_key=KEY, features=FeatureFlags(debounce=False))
    flagged_but_keyless = Settings(features=FeatureFlags(debounce=True))

    assert waterfall.fallback_credentialed(db_session, keyed_but_off) is False
    assert waterfall.fallback_credentialed(db_session, flagged_but_keyless) is False
    assert waterfall.fallback_credentialed(db_session, _settings()) is True


def test_the_default_traversal_gains_debounce_only_once_it_is_usable(
    db_session: Session,
) -> None:
    off = waterfall.default_configuration(db_session, _settings(debounce=False))
    on = waterfall.default_configuration(db_session, _settings())

    assert [item["id"] for item in off["providers"]] == ["millionverifier"]
    assert [item["id"] for item in on["providers"]] == ["millionverifier", "debounce"]


def test_an_absent_optional_provider_never_blocks_startup() -> None:
    """No key, no flag, no failure — the Settings model still constructs."""

    settings = Settings()

    assert settings.has_debounce_key() is False
    assert settings.features.debounce is False
    assert settings.debounce_timeout_seconds >= 2


# --------------------------------------------------------------------------
# Simulated DeBounce is not verification.
# --------------------------------------------------------------------------


def test_simulated_debounce_evidence_can_never_advance_a_contact() -> None:
    """Provenance is checked by label, and the DeBounce simulator has its own."""

    label = evidence_provider_label(SimulatedDebounce(api_key="anything"))
    outcome = service.VerificationOutcome(
        email=EMAIL,
        precise=EmailPreciseStatus.VALID,
        result=EmailVerificationResult.VALID,
        evidence=None,
        reused=False,
        provider_called=True,
        provider_label=label,
        failure_class=VerificationFailureClass.NONE,
        policy_version=POLICY,
    )

    assert label == "debounce-simulator"
    assert outcome.simulated is True


# --------------------------------------------------------------------------
# 17-18. The bypass paths still reach no provider at all.
#
# VER-02 adds a second place a credit can be spent, so the two paths that are
# deliberately *not* verification are re-proved against the real pipeline with
# every provider constructor booby-trapped. "We were given this address" and
# "we asked a provider and it answered" must stay different sentences.
# --------------------------------------------------------------------------


@pytest.fixture()
def _no_provider_may_be_built(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(provider_id: str, **_: Any) -> Any:
        raise AssertionError(f"a bypass path built the {provider_id} provider")

    monkeypatch.setattr(waterfall, "build_provider_by_id", refuse)
    monkeypatch.setattr(
        service,
        "get_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a bypass path built a verification provider")
        ),
    )


def _assert_no_verification_happened(session: Session) -> None:
    assert session.scalars(select(ExactEmailVerification)).all() == []
    assert session.scalars(select(VerificationProviderAttempt)).all() == []


def test_an_operator_supplied_sheet_address_reaches_neither_provider(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    _no_provider_may_be_built: None,
) -> None:
    from tests import test_supplied_contact_data_fast_path as sheets

    monkeypatch.setenv("FEATURES__COMPANY_RESEARCH", "true")
    monkeypatch.setenv("FEATURES__RESEARCH_CLAUDE_FALLBACK", "true")
    get_settings.cache_clear()

    campaign = sheets.make_campaign(db_session)
    sheets.seed_company(db_session)
    sheets.submit(db_session, campaign, email=sheets.SUPPLIED_EMAIL)
    membership = sheets.membership_of(db_session, campaign)

    sheets.run_pipeline(db_session)
    db_session.flush()

    assert sheets.email_result(db_session, membership)["domain_outcome"] == (
        "supplied_email_accepted"
    )
    _assert_no_verification_happened(db_session)


@pytest.mark.usefixtures("enable_csv_import")
def test_an_imported_supplied_address_reaches_neither_provider(
    db_session: Session,
    _no_provider_may_be_built: None,
) -> None:
    from tests import test_campaign_import_email as imports

    campaign, _record = imports._import_one(db_session)
    imports._run_to_email(db_session, campaign)
    db_session.flush()

    email_job = db_session.scalars(
        select(AgentJob).where(AgentJob.agent_id == AgentIdentifier.EMAIL)
    ).one()
    assert email_job.result is not None
    assert email_job.result["domain_outcome"] == "imported_email_accepted"
    assert email_job.result["provider_call_created"] is False
    _assert_no_verification_happened(db_session)


# --------------------------------------------------------------------------
# Repair regressions for review findings F1-F4.
#
# Each of these reproduces a defect a reviewer demonstrated against the first
# candidate, so each is written to fail loudly if the repair is ever undone.
# --------------------------------------------------------------------------


def _envelope(success: object, **fields: Any) -> str:
    """The published Single Validation shape.

    ``success`` and ``balance`` are siblings of ``debounce``, not nested inside
    it, which is how the vendor actually replies.
    """

    import json

    return json.dumps(
        {"debounce": {"email": EMAIL, **fields}, "success": success, "balance": "329918"}
    )


# --- F1. success is documented as an integer and rendered as a string ------


@pytest.mark.parametrize("success", [1, "1", True])
def test_f1_every_documented_true_form_of_success_is_a_usable_envelope(
    success: object,
) -> None:
    """The defect: integer 1 fell through and made a good result unreadable."""

    response = HttpDebounce(
        KEY, transport=_Body(_envelope(success, result="Safe to Send", code="5"))
    ).verify(EMAIL)

    assert response.error is None
    assert response.result == "ok"


@pytest.mark.parametrize("success", [0, "0", False])
def test_f1_every_documented_false_form_of_success_is_a_provider_failure(
    success: object,
) -> None:
    """A false envelope stays a provider failure, never a mailbox verdict."""

    response = HttpDebounce(
        KEY, transport=_Body(_envelope(success, result="Safe to Send", code="5"))
    ).verify(EMAIL)
    mapped = get_policy(get_settings()).map_response(response)

    assert response.result is None
    assert response.error == "unusable_response"
    assert not mapped.is_address_evidence
    assert mapped.precise is not EmailPreciseStatus.UNKNOWN


@pytest.mark.parametrize("value", [2, -1, "maybe", "affirmative", [], {}, None])
def test_f1_undocumented_forms_fail_closed_rather_than_guessing(value: object) -> None:
    """No truthiness shortcut: an undocumented value is unreadable, not True."""

    assert _as_optional_bool(value) is None


def test_f1_a_boolean_field_reads_the_same_however_the_vendor_renders_it() -> None:
    """The widening covers role and free_email too, not only success."""

    integers = HttpDebounce(
        KEY, transport=_Body(_envelope(1, result="Safe to Send", code="5", role=0, free_email=1))
    ).verify(EMAIL)
    strings = HttpDebounce(
        KEY,
        transport=_Body(
            _envelope("1", result="Safe to Send", code="5", role="false", free_email="true")
        ),
    ).verify(EMAIL)

    assert (integers.role, integers.free) == (False, True)
    assert (strings.role, strings.free) == (False, True)


# --- F2. The switch outranks every credential source ----------------------


def _rotate_studio_credential(session: Session, settings: Settings) -> None:
    studio.rotate_credential(
        session,
        provider_id="debounce",
        secret=KEY,
        label="operator key",
        actor="test",
        settings=settings,
    )


def _activate_waterfall_with_debounce(session: Session) -> None:
    version = studio.create_waterfall_version(
        session,
        name="MV then DeBounce",
        configuration={"providers": [{"id": "millionverifier"}, {"id": "debounce"}]},
        change_note="test",
        actor="test",
    )
    studio.activate_waterfall(session, policy_version_id=version.id, actor="test", reason="test")
    session.flush()


def _studio_only(*, switch: bool) -> Settings:
    return Settings(
        millionverifier_api_key="mv-key",
        debounce_api_key=None,
        features=FeatureFlags(debounce=switch),
        provider_credential_encryption_key=Fernet.generate_key().decode(),
    )


def test_f2_a_studio_credential_cannot_re_enable_a_switched_off_provider(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect: the Studio secret was returned before the switch was read."""

    settings = _studio_only(switch=False)
    _rotate_studio_credential(db_session, settings)
    _activate_waterfall_with_debounce(db_session)

    primary = _Fake("millionverifier", [_mv("catch_all")])
    # DeBounce is absent from the factory, so constructing it fails the test.
    _install(monkeypatch, millionverifier=primary)

    outcome = _run(db_session, settings)

    assert outcome.providers_attempted == ("millionverifier",)
    assert outcome.fallback_used is False
    assert waterfall.fallback_credentialed(db_session, settings) is False


def test_f2_a_switched_off_provider_is_not_even_offered_a_credential(
    db_session: Session,
) -> None:
    """Proved at the seam: no secret is returned, so nothing can be built."""

    settings = _studio_only(switch=False)
    _rotate_studio_credential(db_session, settings)

    assert waterfall._credential(db_session, "debounce", settings) is None
    # The primary is unaffected by the DeBounce switch.
    assert waterfall._credential(db_session, "millionverifier", settings) == "mv-key"


def test_f2_a_switched_off_provider_refuses_an_explicit_studio_live_test(
    db_session: Session,
) -> None:
    """The other path that can construct and call DeBounce."""

    settings = _studio_only(switch=False)
    _rotate_studio_credential(db_session, settings)

    with pytest.raises(studio.StudioConfigurationError) as raised:
        studio.provider_test(
            db_session,
            provider_id="debounce",
            email=EMAIL,
            live=True,
            actor="test",
            settings=settings,
        )

    assert "FEATURES__DEBOUNCE" in str(raised.value)


def test_f2_a_studio_credential_is_a_real_credential_once_the_switch_is_on(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch is a veto, not a second key requirement."""

    settings = _studio_only(switch=True)
    _rotate_studio_credential(db_session, settings)

    assert waterfall.fallback_credentialed(db_session, settings) is True
    assert [
        item["id"] for item in waterfall.default_configuration(db_session, settings)["providers"]
    ] == ["millionverifier", "debounce"]

    primary = _Fake("millionverifier", [_mv("catch_all")])
    fallback = _Fake("debounce", [_mv("ok")])
    _install(monkeypatch, millionverifier=primary, debounce=fallback)

    outcome = _run(db_session, settings)

    assert fallback.calls == 1
    assert outcome.providers_attempted == ("millionverifier", "debounce")


# --- F3. A deterministic defect is bought once, not once per attempt -------


def test_f3_an_unusable_reply_is_purchased_once_not_once_per_retry(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect: a reviewer reproduced four paid calls for one address.

    An unusable reply stores no reusable evidence, so nothing throttled the
    retry loop. It is now permanent for the provider that sent it — the same
    reply parses the same way every time — so the job stops instead of buying
    the identical unreadable answer on every remaining attempt.
    """

    unusable = ProviderResponse(
        email=EMAIL, result=None, resultcode=None, error="unusable_response"
    )
    primary = _Fake("millionverifier", [_mv("catch_all")])
    fallback = _Fake("debounce", [unusable])
    _install(monkeypatch, millionverifier=primary, debounce=fallback)
    job = _claim(db_session)

    outcome = _run(db_session, _settings(), job=job)

    assert fallback.calls == 1
    assert outcome.outcome.failure_class is VerificationFailureClass.PERMANENT_PROVIDER
    assert outcome.outcome.condition == "unusable_response"
    # The pipeline decision is a truthful stop: not a retry, not a verdict.
    decision = decide(
        outcome.outcome.precise,
        failure_class=outcome.outcome.failure_class,
        retry_available=True,
    )
    assert decision.decision is VerificationDecision.STOP_NO_RESULT
    assert decision.status is EmailPreciseStatus.PROVIDER_ERROR


def test_f3_an_unusable_reply_is_still_never_a_mailbox_verdict(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = _Fake("millionverifier", [_mv("catch_all")])
    fallback = _Fake(
        "debounce",
        [ProviderResponse(email=EMAIL, result=None, resultcode=None, error="unusable_response")],
    )
    _install(monkeypatch, millionverifier=primary, debounce=fallback)

    _run(db_session, _settings())
    db_session.flush()

    stored = db_session.scalars(select(ExactEmailVerification)).all()
    assert [row.provider for row in stored] == ["millionverifier"]
    assert stored[0].result is EmailVerificationResult.CATCH_ALL


def test_f3_a_transport_failure_keeps_its_bounded_retry(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinction the repair rests on: transient still means retry."""

    primary = _Fake("millionverifier", [_mv("catch_all")])
    fallback = _Fake("debounce", [ProviderTransientError("HTTP 429", condition="rate_limit")])
    _install(monkeypatch, millionverifier=primary, debounce=fallback)

    outcome = _run(db_session, _settings())

    assert outcome.outcome.failure_class is VerificationFailureClass.TRANSIENT_PROVIDER
    assert (
        decide(
            outcome.outcome.precise,
            failure_class=outcome.outcome.failure_class,
            retry_available=True,
        ).decision
        is VerificationDecision.RETRY_LATER
    )


def test_f3_an_unusable_primary_still_lets_the_fallback_answer(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permanent for one provider must not mean permanent for the address."""

    primary = _Fake(
        "millionverifier",
        [ProviderResponse(email=EMAIL, result="something-new", resultcode=None)],
    )
    fallback = _Fake("debounce", [_mv("ok")])
    _install(monkeypatch, millionverifier=primary, debounce=fallback)

    outcome = _run(db_session, _settings())

    assert outcome.fallback_condition == "unusable_response"
    assert fallback.calls == 1
    assert outcome.outcome.precise is EmailPreciseStatus.VALID


# --- F4. Code 8 is Role, and Role is a documented classification ----------


def test_f4_a_code_only_role_reply_is_a_classification_not_a_parse_failure() -> None:
    """The defect: code 8 was absent from the table and read as unreadable."""

    response = HttpDebounce(KEY, transport=_Body(_envelope("1", code="8"))).verify(EMAIL)
    mapped = get_policy(get_settings()).map_response(response)

    assert response.error is None
    assert response.role is True
    assert mapped.is_address_evidence
    assert mapped.precise is EmailPreciseStatus.ROLE_BASED


def test_f4_risky_with_the_role_code_is_role_based() -> None:
    response = HttpDebounce(
        KEY, transport=_Body(_envelope("1", result="Risky", code="8", reason="Role"))
    ).verify(EMAIL)
    mapped = get_policy(get_settings()).map_response(response)

    assert mapped.precise is EmailPreciseStatus.ROLE_BASED
    # Role is a flag on a valid address in this model, never a result of its own.
    assert mapped.result is EmailVerificationResult.VALID


def test_f4_the_role_code_sets_the_flag_even_when_the_field_disagrees() -> None:
    """Code 8 is itself the role signal; an absent or false field cannot erase it."""

    absent = HttpDebounce(KEY, transport=_Body(_envelope("1", code="8"))).verify(EMAIL)
    contradicted = HttpDebounce(
        KEY, transport=_Body(_envelope("1", code="8", role="false"))
    ).verify(EMAIL)

    assert absent.role is True
    assert contradicted.role is True


def test_f4_role_based_is_authoritative_and_stops_the_traversal(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Role is an answer about the mailbox, so nothing else is asked."""

    role_reply = HttpDebounce(KEY, transport=_Body(_envelope("1", result="Risky", code="8")))
    primary = _Fake("millionverifier", [_mv("catch_all")])
    _install(monkeypatch, millionverifier=primary, debounce=role_reply)

    outcome = _run(db_session, _settings())

    assert outcome.outcome.precise is EmailPreciseStatus.ROLE_BASED
    assert fallback_policy.assess(outcome.outcome).authoritative


def test_f4_a_role_address_never_advances_a_campaign_contact() -> None:
    """ROLE_BASED keeps its own state; it does not become an accepted address."""

    decision = decide(EmailPreciseStatus.ROLE_BASED)

    assert decision.decision is VerificationDecision.TRY_NEXT_CANDIDATE
    assert decision.accepted is False


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("1", EmailPreciseStatus.INVALID),
        ("2", EmailPreciseStatus.INVALID),
        ("3", EmailPreciseStatus.DISPOSABLE),
        ("4", EmailPreciseStatus.CATCH_ALL),
        ("5", EmailPreciseStatus.VALID),
        ("6", EmailPreciseStatus.INVALID),
        ("7", EmailPreciseStatus.UNKNOWN),
        ("8", EmailPreciseStatus.ROLE_BASED),
    ],
)
def test_f4_the_whole_published_code_table_resolves(
    code: str, expected: EmailPreciseStatus
) -> None:
    """Every documented code resolves; none of them is an unreadable reply."""

    response = HttpDebounce(KEY, transport=_Body(_envelope("1", code=code))).verify(EMAIL)
    mapped = get_policy(get_settings()).map_response(response)

    assert response.error is None
    assert mapped.precise is expected


def test_f4_a_code_outside_the_published_table_is_still_unreadable() -> None:
    """Widening the table must not widen it into guessing."""

    response = HttpDebounce(KEY, transport=_Body(_envelope("1", code="99"))).verify(EMAIL)

    assert response.error == "unusable_response"
