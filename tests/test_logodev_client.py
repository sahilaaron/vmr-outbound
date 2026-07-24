"""logo.dev Search Brands client (DAT-010).

Exercises every provider condition through an injected transport so no network
is touched, and proves the API key never leaks into the URL, the result, or a
raised error.
"""

from __future__ import annotations

import json

import pytest
from app.models.enums import EnrichmentLookupStatus
from app.services.enrichment import logodev


def _transport(body: str, status: int = 200) -> logodev.Transport:
    def _call(url: str, headers: dict, timeout: float) -> logodev.RawResponse:  # type: ignore[type-arg]
        return logodev.RawResponse(status_code=status, body=body)

    return _call


def test_single_exact_candidate() -> None:
    result = logodev.search_brands(
        "Acme",
        api_key="k",
        transport=_transport(json.dumps([{"name": "Acme", "domain": "acme.com"}])),
    )
    assert result.status is EnrichmentLookupStatus.OK
    assert [c.domain for c in result.candidates] == ["acme.com"]
    assert result.candidates[0].name == "Acme"


def test_multiple_candidates_are_all_returned_and_deduped_and_normalized() -> None:
    body = json.dumps(
        [
            {"name": "Acme", "domain": "https://www.acme.com/about"},
            {"name": "Acme Labs", "domain": "acme.io"},
            {"name": "Dupe", "domain": "ACME.com"},  # duplicate of the first, normalized
        ]
    )
    result = logodev.search_brands("Acme", api_key="k", transport=_transport(body))
    assert result.status is EnrichmentLookupStatus.OK
    assert [c.domain for c in result.candidates] == ["acme.com", "acme.io"]


def test_no_match_is_truthful() -> None:
    result = logodev.search_brands("Nope", api_key="k", transport=_transport("[]"))
    assert result.status is EnrichmentLookupStatus.NO_MATCH
    assert result.candidates == ()


def test_rate_limited() -> None:
    result = logodev.search_brands("x", api_key="k", transport=_transport("", status=429))
    assert result.status is EnrichmentLookupStatus.RATE_LIMITED


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_unavailable(status: int) -> None:
    result = logodev.search_brands("x", api_key="k", transport=_transport("", status=status))
    assert result.status is EnrichmentLookupStatus.API_UNAVAILABLE


@pytest.mark.parametrize("status", [401, 403, 418])
def test_auth_and_unexpected_status_are_unavailable(status: int) -> None:
    result = logodev.search_brands("x", api_key="k", transport=_transport("", status=status))
    assert result.status is EnrichmentLookupStatus.API_UNAVAILABLE


def test_transport_error_is_unavailable() -> None:
    def _boom(url: str, headers: dict, timeout: float) -> logodev.RawResponse:  # type: ignore[type-arg]
        raise logodev.TransportError("connection reset")

    result = logodev.search_brands("x", api_key="k", transport=_boom)
    assert result.status is EnrichmentLookupStatus.API_UNAVAILABLE


@pytest.mark.parametrize(
    "body", ["not json", '{"unexpected": 1}', "[1, 2, 3]", '[{"no":"domain"}]']
)
def test_malformed_bodies(body: str) -> None:
    result = logodev.search_brands("x", api_key="k", transport=_transport(body))
    assert result.status is EnrichmentLookupStatus.MALFORMED


def test_data_envelope_is_accepted() -> None:
    result = logodev.search_brands(
        "x", api_key="k", transport=_transport('{"data": [{"domain": "z.com"}]}')
    )
    assert result.status is EnrichmentLookupStatus.OK
    assert result.candidates[0].domain == "z.com"


def test_blank_query_is_no_match_without_calling() -> None:
    def _never(url: str, headers: dict, timeout: float) -> logodev.RawResponse:  # type: ignore[type-arg]
        raise AssertionError("must not call the transport for a blank query")

    assert logodev.search_brands("   ", api_key="k", transport=_never).status is (
        EnrichmentLookupStatus.NO_MATCH
    )


def test_empty_api_key_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        logodev.search_brands("x", api_key="", transport=_transport("[]"))


def test_api_key_never_leaks_into_url_or_result() -> None:
    captured: dict[str, object] = {}

    def _spy(url: str, headers: dict, timeout: float) -> logodev.RawResponse:  # type: ignore[type-arg]
        captured["url"] = url
        captured["headers"] = dict(headers)
        return logodev.RawResponse(200, json.dumps([{"domain": "a.com"}]))

    secret = "sk_live_TOP_SECRET_VALUE"
    result = logodev.search_brands("Query", api_key=secret, transport=_spy)

    # The key is only ever in the Authorization header, never the URL or result.
    assert secret not in str(captured["url"])
    assert secret in captured["headers"]["Authorization"]  # type: ignore[index]
    assert secret not in repr(result)
