"""The acceptance policy for a model-asserted company domain.

The fallback behind logo.dev could already find a domain. What it could not do
was justify one. Acceptance was two checks — the string parses as a hostname, and
the hostname is not on the unsuitable-domain blocklist — and for the population
this fallback exists to serve those two are close to no check at all: every
company here is one a brand matcher already failed to match, which is exactly
where a same-named company in another country is most likely to be what a web
search surfaces first. A well-formed ``acme.com`` for the Acme in Pune passes
both checks and is wrong.

So this file tests the refusals more than the acceptances. The rule asserted
throughout is that a domain is accepted on what the model *read*, never on how
the answer is *spelled* — and that when the evidence is not there, the contact
stays unresolved rather than moving on behind a plausible guess. Remaining
unresolved is a state the product already handles; a wrong domain starts a
research crawl against a stranger and puts a stranger's facts in front of an
operator.

Nothing here reaches the network or a real Claude CLI. Every answer is scripted.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.models.company import Company
from app.models.enums import DomainResolutionState, EnrichmentLookupStatus
from app.models.salesnav_enrichment import SalesNavCompanyEnrichment
from app.services.enrichment import model_domain
from app.services.resolution import gates as resolution_gates
from app.services.resolution import policy
from app.services.resolution import service as resolution_service
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests import capture_factory
from tests.test_model_domain_lookup import ScriptedThinker, _evidenced, _model, _provider

ACTOR = "model-acceptance-test"

DOMAIN = "quanthealth.ai"


def _claim(**overrides: Any) -> dict[str, Any]:
    """A claim that satisfies every rule, before *overrides* spoil one of them."""

    claim: dict[str, Any] = {
        "schema_version": model_domain.LOOKUP_VERSION,
        "domain": DOMAIN,
        "official_website_url": f"https://{DOMAIN}/about",
        "confidence": "high",
        "evidence": [
            {
                "url": f"https://{DOMAIN}/about",
                "host": DOMAIN,
                "kind": "official_site",
                "detail": "About page names QuantHealth and its Tel Aviv office",
            }
        ],
        "ambiguity": None,
        "reasoning_summary": "Read the company's own About page.",
    }
    claim.update(overrides)
    return claim


def _decide(claim: dict[str, Any] | None, *, domain: str = DOMAIN) -> policy.PolicyDecision:
    """The full policy decision for a model answer of *domain* backed by *claim*."""

    return policy.evaluate(
        policy.ResolutionEvidence(
            company_name="QuantHealth",
            normalized_company_name="quanthealth",
            lookup_status=EnrichmentLookupStatus.NO_MATCH,
            model_lookup_status=EnrichmentLookupStatus.OK,
            model_domain=domain,
            model_claim=claim,
        )
    )


# ---------------------------------------------------------------------------
# The rule the whole gate exists for
# ---------------------------------------------------------------------------


def test_a_well_formed_domain_with_nothing_behind_it_is_refused() -> None:
    """The single most important assertion in this file.

    ``quanthealth.ai`` is a perfectly good hostname, is on no blocklist, and would
    have been accepted before this gate. It is refused because nothing recorded
    says the model established it belongs to THIS employer — which is the whole
    difference between a finding and an assertion.
    """

    decision = _decide(None)

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert decision.selected_domain is None
    assert policy.REASON_MODEL_CLAIM_MISSING in decision.reasons


def test_an_evidenced_answer_is_accepted_and_says_why() -> None:
    """The other half: a claim that carries its receipts resolves, provisionally."""

    decision = _decide(_claim())

    assert decision.state is DomainResolutionState.PROVISIONAL
    assert decision.selected_domain == DOMAIN
    assert policy.REASON_MODEL_EVIDENCE_ACCEPTED in decision.reasons
    # Never confirmed, however good the evidence. A model is not a deterministic
    # source, and the stages that spend money stay shut behind provisional.
    assert decision.state is not DomainResolutionState.CONFIRMED


@pytest.mark.parametrize("confidence", ["medium", "low", "unknown", "very high", "", None, 0.99])
def test_only_a_high_confidence_answer_is_accepted(confidence: object) -> None:
    """Anything but "high" is refused, including a number that looks confident.

    A numeric score would invite a threshold comparison that reads meaning into
    the difference between 0.71 and 0.70. Three named grades cannot be over-read,
    and an unrecognised grade is not quietly promoted to the top one.
    """

    decision = _decide(_claim(confidence=confidence))

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert policy.REASON_MODEL_CONFIDENCE_TOO_LOW in decision.reasons


def test_a_stated_competing_company_refuses_the_answer() -> None:
    """The India/US case, in the form the model is asked to report it.

    This is the failure the gate is built around: two real companies, one name,
    and a search that surfaces the larger one. A model that names the rival has
    done its job — and the right outcome is still no domain.
    """

    decision = _decide(_claim(ambiguity="A US company of the same name operates quanthealth.com"))

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert policy.REASON_MODEL_COMPANY_AMBIGUOUS in decision.reasons


def test_silence_about_a_rival_is_not_a_denial_of_one() -> None:
    """An answer that never addressed the question is refused, not assumed clean.

    Absent and null are deliberately different. "I checked and there is no other
    company of this name" is an answer the policy accepts; not mentioning it looks
    identical to a confident answer that never considered it, and that is exactly
    how the wrong same-named company gets selected.
    """

    claim = _claim()
    claim.pop("ambiguity")

    decision = _decide(claim)

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert policy.REASON_MODEL_COMPANY_AMBIGUOUS in decision.reasons


# ---------------------------------------------------------------------------
# What counts as having read the company's own site
# ---------------------------------------------------------------------------


def test_an_answer_that_cites_no_page_is_refused() -> None:
    decision = _decide(_claim(evidence=[]))

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert policy.REASON_MODEL_EVIDENCE_MISSING in decision.reasons


def test_directory_pages_alone_cannot_confirm_a_domain() -> None:
    """A profile somebody filed on an aggregator is not the company saying "this is us".

    Named apart from the general off-domain refusal because an operator acts
    differently on it: "it only ever found a Crunchbase entry" usually means the
    company has little web presence, not that the domain is wrong.
    """

    decision = _decide(
        _claim(
            evidence=[
                {
                    "url": "https://www.crunchbase.com/organization/quanthealth",
                    "host": "crunchbase.com",
                    "kind": "directory",
                    "detail": "Profile lists the site",
                },
                {
                    "url": "https://www.linkedin.com/company/quanthealth",
                    "host": "linkedin.com",
                    "kind": "linkedin_company",
                    "detail": "Company page",
                },
            ]
        )
    )

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert policy.REASON_MODEL_EVIDENCE_DIRECTORY_ONLY in decision.reasons


def test_reading_a_real_site_that_is_not_this_one_is_refused() -> None:
    """The acronym case: it read *a* company's site, just not the one it named."""

    decision = _decide(
        _claim(
            evidence=[
                {
                    "url": "https://quanthealth.com/about",
                    "host": "quanthealth.com",
                    "kind": "official_site",
                    "detail": "About page of a same-named US company",
                }
            ]
        )
    )

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert policy.REASON_MODEL_EVIDENCE_OFF_DOMAIN in decision.reasons


def test_a_page_on_a_subdomain_still_counts_as_the_companys_own_site() -> None:
    """``careers.acme.com`` is the company's site. Requiring the apex would refuse it."""

    decision = _decide(
        _claim(
            evidence=[
                {
                    "url": f"https://careers.{DOMAIN}/roles",
                    "host": f"careers.{DOMAIN}",
                    "kind": "official_site",
                    "detail": "Careers site names the company",
                }
            ]
        )
    )

    assert decision.state is DomainResolutionState.PROVISIONAL
    assert decision.selected_domain == DOMAIN


def test_an_unparseable_citation_supports_nothing() -> None:
    """A citation whose host could not be read is not evidence of anything.

    A half-parsed citation counted as a citation would let an unreadable URL stand
    in for having read the site.
    """

    decision = _decide(
        _claim(evidence=[{"url": "see the website", "host": None, "kind": None, "detail": None}])
    )

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert policy.REASON_MODEL_EVIDENCE_MISSING in decision.reasons


@pytest.mark.parametrize("domain", ["gmail.com", "outlook.com", "yahoo.com", "protonmail.com"])
def test_a_free_mailbox_provider_is_never_a_company_domain(domain: str) -> None:
    """Perfectly evidenced and still refused — the host itself disqualifies it.

    This feature must never be the thing that turns a personal address into a
    company's official domain, which is a rule the parallel supplied-contact-data
    work depends on holding here too.
    """

    decision = _decide(
        _claim(
            domain=domain,
            evidence=[
                {
                    "url": f"https://{domain}/",
                    "host": domain,
                    "kind": "official_site",
                    "detail": "Homepage",
                }
            ],
        ),
        domain=domain,
    )

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert policy.REASON_MODEL_DOMAIN_UNSUITABLE in decision.reasons


# ---------------------------------------------------------------------------
# When the fallback becomes eligible at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lookup_status",
    [
        EnrichmentLookupStatus.API_UNAVAILABLE,
        EnrichmentLookupStatus.RATE_LIMITED,
        EnrichmentLookupStatus.ERROR,
        EnrichmentLookupStatus.MALFORMED,
        EnrichmentLookupStatus.NOT_STARTED,
    ],
)
def test_a_provider_that_is_still_owed_a_retry_does_not_spend_the_model(
    lookup_status: EnrichmentLookupStatus,
) -> None:
    """The fallback is admitted after a genuine "nothing", not after a bad day.

    A provider that timed out has not said anything about this company yet, and
    answering with a model instead would both spend a call the retry would have
    made unnecessary and record a searched answer where a deterministic one was
    still coming.
    """

    decision = policy.evaluate(
        policy.ResolutionEvidence(
            company_name="QuantHealth",
            normalized_company_name="quanthealth",
            lookup_status=lookup_status,
            # Fully evidenced and irrelevant: it must not be consulted at all.
            model_lookup_status=EnrichmentLookupStatus.OK,
            model_domain=DOMAIN,
            model_claim=_claim(),
        )
    )

    assert decision.state is DomainResolutionState.UNRESOLVED
    assert decision.selected_domain is None
    assert policy.REASON_MODEL_EVIDENCE_ACCEPTED not in decision.reasons


def test_the_fallback_is_eligible_once_the_provider_has_genuinely_finished() -> None:
    """The other side of the same boundary: NO_MATCH is an answer about this company."""

    decision = _decide(_claim())

    assert decision.state is DomainResolutionState.PROVISIONAL
    assert policy.REASON_PROVIDER_NO_CANDIDATES in decision.reasons


# ---------------------------------------------------------------------------
# What a refusal and an acceptance leave behind
# ---------------------------------------------------------------------------


def test_a_refused_domain_is_kept_as_a_rejected_candidate() -> None:
    """What the model got wrong is provenance.

    A refusal that discarded the answer would make a run of thin model answers
    look identical to the model never having been asked.
    """

    decision = _decide(_claim(confidence="low"))

    rejected = decision.candidates[-1]
    assert rejected.domain == DOMAIN
    assert rejected.eligible is False
    assert rejected.rejection_reason == policy.REASON_MODEL_CONFIDENCE_TOO_LOW


def test_an_accepted_domain_carries_the_evidence_it_was_accepted_on() -> None:
    """ "Why was this accepted" has to be answerable from the decision itself."""

    decision = _decide(_claim())

    claim = (decision.selected_candidate or {}).get("claim") or {}
    assert claim["confidence"] == "high"
    assert claim["ambiguity"] is None
    assert claim["evidence"][0]["host"] == DOMAIN
    assert claim["reasoning_summary"]
    # The provenance stays honest: this is a model answer, not logo.dev's.
    assert decision.provider == policy.MODEL_PROVIDER


def test_the_stored_claim_is_a_summary_not_a_transcript() -> None:
    """No prompt, no raw response, no working. An audit needs the finding only."""

    claim = (_decide(_claim()).selected_candidate or {}).get("claim") or {}

    assert set(claim) == {"confidence", "ambiguity", "reasoning_summary", "evidence"}


# ---------------------------------------------------------------------------
# Reading the model's answer into a claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("high", model_domain.ModelConfidence.HIGH),
        ("HIGH", model_domain.ModelConfidence.HIGH),
        (" medium ", model_domain.ModelConfidence.MEDIUM),
        ("low", model_domain.ModelConfidence.LOW),
        ("unknown", model_domain.ModelConfidence.UNKNOWN),
        ("certain", model_domain.ModelConfidence.UNKNOWN),
        (0.9, model_domain.ModelConfidence.UNKNOWN),
        (None, model_domain.ModelConfidence.UNKNOWN),
    ],
)
def test_the_grades_a_model_may_claim(given: object, expected: object) -> None:
    assert model_domain._read_confidence(given) is expected


def test_evidence_is_normalized_bounded_and_stripped_of_unusable_entries() -> None:
    """Each citation arrives with its host already parsed, and the list is capped.

    The host is parsed there rather than in the policy so there is one URL
    normalizer, and so the policy compares strings instead of learning to read
    URLs — the same reason the model's domain itself is normalized there.
    """

    items = model_domain._read_evidence(
        [
            "https://www.QuantHealth.ai/about",  # a bare string is taken as a URL
            {"url": "https://quanthealth.ai/team", "kind": "official_site", "detail": "x" * 400},
            {"kind": "official_site"},  # no URL: dropped entirely
            "not a url at all",  # kept, but with no host to support anything
            *[{"url": f"https://quanthealth.ai/{n}"} for n in range(10)],
        ]
    )

    assert len(items) == model_domain.MAX_EVIDENCE_ITEMS
    assert items[0]["host"] == "quanthealth.ai"
    assert items[0]["url"] == "https://www.QuantHealth.ai/about"
    assert len(items[1]["detail"]) == model_domain.MAX_DETAIL_CHARS
    assert items[2]["host"] is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ambiguity": None}, None),
        ({"ambiguity": "none"}, None),
        ({"ambiguity": " N/A "}, None),
        ({"ambiguity": "a US company of the same name"}, "a US company of the same name"),
        ({"ambiguity": 7}, model_domain.AMBIGUITY_NOT_STATED),
        ({}, model_domain.AMBIGUITY_NOT_STATED),
    ],
)
def test_how_the_rival_company_question_is_read(payload: dict[str, Any], expected: object) -> None:
    assert model_domain._read_ambiguity(payload) == expected


def test_a_refusal_and_a_failure_produce_no_claim_to_grade() -> None:
    """An empty claim would be indistinguishable from an unevidenced answer.

    They are different facts — "it said it could not tell" versus "it named a
    domain and showed nothing" — and the policy reports them with different reason
    codes, which it can only do if one of them stores nothing at all.
    """

    declined = model_domain.look_up(
        company_name="QuantHealth", thinker=ScriptedThinker({"domain": None, "reason": "unsure"})
    )
    malformed = model_domain.look_up(
        company_name="QuantHealth", thinker=ScriptedThinker({"nonsense": True})
    )

    assert declined.claim_payload() is None
    assert malformed.claim_payload() is None


def test_the_answer_is_read_under_either_name_for_the_official_url() -> None:
    """The column is ``model_source_url``; the contract now says
    ``official_website_url``. An answer in either shape is understood, so a model
    echoing the older key is not recorded as having cited nothing."""

    new_shape = model_domain.look_up(
        company_name="QuantHealth", thinker=ScriptedThinker(_evidenced(DOMAIN))
    )
    old_shape = model_domain.look_up(
        company_name="QuantHealth",
        thinker=ScriptedThinker({"domain": DOMAIN, "source_url": f"https://{DOMAIN}/about"}),
    )

    assert new_shape.source_url == f"https://{DOMAIN}/about"
    assert old_shape.source_url == f"https://{DOMAIN}/about"
    # But the old shape carries no evidence, so it is not an accepted answer.
    assert old_shape.confidence is model_domain.ModelConfidence.UNKNOWN


def test_the_prompt_asks_for_every_field_the_policy_grades() -> None:
    """A gate that grades a field the model was never asked for rejects everything."""

    prompt = model_domain.build_prompt("QuantHealth", ("Tel Aviv, Israel",))

    for field in ("confidence", "evidence", "ambiguity", "official_website_url"):
        assert field in prompt
    assert policy.MODEL_ACCEPTED_CONFIDENCE in prompt


# ---------------------------------------------------------------------------
# End to end: what the pipeline does with each outcome
# ---------------------------------------------------------------------------


def test_an_unevidenced_rescue_leaves_the_capture_unresolved(db_session: Session) -> None:
    """No fake domain is written, and the honest unresolved outcome survives.

    The answer here is the one the old contract asked for — a domain and a source
    URL, nothing else — which is exactly what a model that ignores the new fields
    returns. It is parsed into a claim that rates itself ``unknown`` and cites
    nothing, so it is refused on confidence before the evidence rules are even
    reached. The capture is not promoted and no Company is created: the state the
    product already knows how to show an operator, rather than a plausible domain
    that would have sent research after the wrong company.
    """

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    thinker = ScriptedThinker({"domain": DOMAIN, "source_url": f"https://{DOMAIN}/about"})

    outcome = resolution_service.resolve(
        db_session, snapshot=snapshot, access=_provider(), model=_model(thinker), actor=ACTOR
    )

    assert outcome.model_call_made, "the fallback must still have been asked"
    assert outcome.state is DomainResolutionState.UNRESOLVED
    assert outcome.selected_domain is None
    assert not outcome.auto_promoted
    assert policy.REASON_MODEL_CONFIDENCE_TOO_LOW in [str(r) for r in outcome.decision.reasons]

    # What the model said is still recorded, refusal and all. A rejection is not
    # an erasure, and the claim shows precisely what was missing.
    record = db_session.scalars(
        select(SalesNavCompanyEnrichment).where(SalesNavCompanyEnrichment.capture_id == snapshot.id)
    ).one()
    assert record.model_domain == DOMAIN
    assert record.model_claim is not None
    assert record.model_claim["confidence"] == model_domain.ModelConfidence.UNKNOWN.value
    assert record.model_claim["evidence"] == []
    assert record.model_claim["ambiguity"] == model_domain.AMBIGUITY_NOT_STATED


def test_a_row_answered_before_the_claim_contract_is_refused_not_trusted(
    db_session: Session,
) -> None:
    """The migration's behaviour change, asserted where it actually lands.

    ``model_claim`` is NULL on every row answered before this contract existed.
    Those rows keep the domain they asserted — nothing is rewritten — but the
    policy grades them as unevidenced rather than trusting them, because nothing
    was recorded that could be checked. Re-deciding costs no second model call:
    the record's own lookup status still says it was asked.
    """

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    thinker = ScriptedThinker(_evidenced(DOMAIN))
    resolution_service.resolve(
        db_session, snapshot=snapshot, access=_provider(), model=_model(thinker), actor=ACTOR
    )

    record = db_session.scalars(
        select(SalesNavCompanyEnrichment).where(SalesNavCompanyEnrichment.capture_id == snapshot.id)
    ).one()
    record.model_claim = None  # what a pre-migration row looks like
    db_session.flush()

    outcome = resolution_service.resolve(
        db_session,
        snapshot=snapshot,
        access=_provider(),
        model=_model(thinker),
        actor=ACTOR,
        force=True,
    )

    assert len(thinker.requests) == 1, "no second call is bought to re-judge a stored answer"
    assert outcome.state is DomainResolutionState.UNRESOLVED
    assert outcome.selected_domain is None
    assert policy.REASON_MODEL_CLAIM_MISSING in [str(r) for r in outcome.decision.reasons]
    assert record.model_domain == DOMAIN, "the asserted domain is kept, not erased"


def test_an_ambiguous_company_is_refused_end_to_end(db_session: Session) -> None:
    """Pune versus the US, through the real service rather than the pure policy."""

    snapshot = capture_factory.salesnav_capture(
        db_session, company_name="QuantHealth", location="Pune, India"
    )
    thinker = ScriptedThinker(
        _evidenced(DOMAIN, ambiguity="A US company of the same name operates quanthealth.com")
    )

    outcome = resolution_service.resolve(
        db_session, snapshot=snapshot, access=_provider(), model=_model(thinker), actor=ACTOR
    )

    assert outcome.state is DomainResolutionState.UNRESOLVED
    assert outcome.selected_domain is None
    assert policy.REASON_MODEL_COMPANY_AMBIGUOUS in [str(r) for r in outcome.decision.reasons]


def test_an_accepted_fallback_hands_company_intelligence_a_researchable_domain(
    db_session: Session,
) -> None:
    """The handoff, asserted where Company Intelligence actually reads it.

    A successful fallback must need no operator click: the Company exists, carries
    the domain, and the research gate opens — exactly as it would have if logo.dev
    had answered. That the domain came from a model changes the confidence
    ceiling, not the shape of the handoff.
    """

    snapshot = capture_factory.salesnav_capture(
        db_session, company_name="QuantHealth", location="Tel Aviv, Israel"
    )

    outcome = resolution_service.resolve(
        db_session,
        snapshot=snapshot,
        access=_provider(),
        model=_model(ScriptedThinker(_evidenced(DOMAIN))),
        actor=ACTOR,
    )

    assert outcome.state is DomainResolutionState.PROVISIONAL
    assert outcome.company is not None
    assert outcome.company.domain == DOMAIN
    assert outcome.auto_promoted

    gate = resolution_gates.research_readiness(
        db_session, company_id=outcome.company.id, domain=outcome.company.domain
    )
    assert gate.ready, gate.reason


def test_a_refused_answer_is_not_re_asked_on_every_retry(db_session: Session) -> None:
    """A rejected claim must not become a per-retry spend.

    The refusal lives in the policy, which is re-run for free; the call is guarded
    by the record's own lookup status, which a rejection does not reset. Without
    this, every unevidenced answer would be re-purchased on each pass over the
    same capture.
    """

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    thinker = ScriptedThinker(
        {"domain": DOMAIN},  # unevidenced: refused
        _evidenced(DOMAIN),  # would be accepted, and must never be reached
    )

    for _ in range(3):
        outcome = resolution_service.resolve(
            db_session,
            snapshot=snapshot,
            access=_provider(),
            model=_model(thinker),
            actor=ACTOR,
            force=True,
        )

    assert len(thinker.requests) == 1
    assert outcome.state is DomainResolutionState.UNRESOLVED


def test_an_accepted_domain_is_reused_rather_than_re_researched(db_session: Session) -> None:
    """Replay of a satisfied resolution spends nothing and changes nothing."""

    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    thinker = ScriptedThinker(_evidenced(DOMAIN))

    first = resolution_service.resolve(
        db_session, snapshot=snapshot, access=_provider(), model=_model(thinker), actor=ACTOR
    )
    second = resolution_service.resolve(
        db_session, snapshot=snapshot, access=_provider(), model=_model(thinker), actor=ACTOR
    )

    assert len(thinker.requests) == 1
    assert second.created is False
    assert second.decision.id == first.decision.id
    assert second.selected_domain == DOMAIN


def test_an_established_domain_is_never_sent_to_the_model(db_session: Session) -> None:
    """The seam the parallel supplied-contact-data work relies on.

    Once a canonical domain exists for this company, the deterministic half of the
    policy answers before any lookup is authorized — so a branch that records a
    supplied domain durably switches this fallback off for that contact without
    touching a line of it.
    """

    db_session.add(Company(id=uuid.uuid4(), name="QuantHealth", domain="quanthealth.com"))
    db_session.flush()
    snapshot = capture_factory.salesnav_capture(db_session, company_name="QuantHealth")
    thinker = ScriptedThinker(_evidenced(DOMAIN))

    outcome = resolution_service.resolve(
        db_session, snapshot=snapshot, access=_provider(), model=_model(thinker), actor=ACTOR
    )

    assert thinker.requests == []
    assert not outcome.model_call_made
    assert not outcome.provider_call_made
    assert outcome.selected_domain == "quanthealth.com"
