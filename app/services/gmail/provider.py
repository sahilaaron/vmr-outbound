"""The Gmail REST adapter.

This module is the only place in the application that speaks HTTP to Gmail, and
it owns exactly two things: request/response translation, and turning a failure
into one of two categories the service layer can act on. It owns no lineage, no
idempotency, no policy and no transaction.

**Two methods, and neither of them sends.** ``create_draft`` posts to
``users.drafts.create``; ``find_draft_by_rfc_message_id`` reads
``users.drafts.list`` with an exact ``rfc822msgid:`` query for the bounded
reconciliation in ``drafts.py``. There is no ``send``, no
``users.messages.send``, no ``users.drafts.send``, and adding one would fail
``tests/test_gmail_draft_integration.py``.

Definite versus ambiguous failure
---------------------------------
The distinction is the whole reason this adapter raises a typed error rather
than returning ``None``:

* a **definite** failure (Gmail answered, and refused -- 400, 403, 404) proves
  no draft exists, so the service may safely retry later;
* an **ambiguous** failure (a timeout, a dropped connection, a 5xx) proves
  nothing at all. Gmail may have created the draft and lost the response. The
  service must never treat this as "no draft" -- that is exactly how one click
  becomes two drafts in a stranger-facing mailbox.

A 401 is treated as definite *and* flagged separately, because it means the
grant needs attention rather than the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.gmail_config import GmailSettings

#: The most drafts one reconciliation query will look at. The query is an exact
#: `rfc822msgid:` match, so a well-behaved answer is zero results or one; the
#: cap exists so a surprising answer cannot turn into an unbounded read of a
#: human's mailbox.
RECONCILIATION_RESULT_CAP = 5


@dataclass(frozen=True)
class GmailDraftHandle:
    """What Gmail returned about one draft."""

    draft_id: str
    #: Gmail's id for the message *inside* the draft. Not a sent-message
    #: identity: sending replaces it, and nothing in this feature sends.
    message_id: str | None
    thread_id: str | None


class GmailProviderError(Exception):
    """A Gmail request did not succeed.

    ``category`` is a bounded token (``http_400``, ``transport``,
    ``unauthorized``) and is the only thing persisted or logged. Gmail's own
    response body is never carried: it can echo the submitted request, and the
    submitted request is authenticated with a bearer token.
    """

    def __init__(self, category: str, *, ambiguous: bool, unauthorized: bool = False) -> None:
        super().__init__(category)
        self.category = category
        #: True when the outcome does not prove whether Gmail acted.
        self.ambiguous = ambiguous
        #: True when the grant, rather than the request, is the problem.
        self.unauthorized = unauthorized


class GmailProvider(Protocol):
    """The seam a test replaces with a deterministic fake."""

    def create_draft(self, *, access_token: str, raw_message: str) -> GmailDraftHandle: ...

    def find_draft_by_rfc_message_id(
        self, *, access_token: str, rfc_message_id: str
    ) -> GmailDraftHandle | None: ...


def _handle_from(payload: Any) -> GmailDraftHandle:
    if not isinstance(payload, dict):
        raise GmailProviderError("malformed_response", ambiguous=False)
    draft_id = payload.get("id")
    if not isinstance(draft_id, str) or not draft_id:
        raise GmailProviderError("malformed_response", ambiguous=False)
    message = payload.get("message")
    message_id = None
    thread_id = None
    if isinstance(message, dict):
        raw_message_id = message.get("id")
        raw_thread_id = message.get("threadId")
        message_id = raw_message_id if isinstance(raw_message_id, str) else None
        thread_id = raw_thread_id if isinstance(raw_thread_id, str) else None
    return GmailDraftHandle(draft_id=draft_id, message_id=message_id, thread_id=thread_id)


class HttpGmailProvider:
    """The live Gmail REST adapter."""

    def __init__(self, settings: GmailSettings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client

    def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._settings.api_base_url.rstrip('/')}{path}"
        headers = {"Authorization": f"Bearer {access_token}"}
        timeout = self._settings.request_timeout_seconds
        try:
            if self._client is not None:
                response = self._client.request(
                    method, url, headers=headers, json=json, params=params, timeout=timeout
                )
            else:
                with httpx.Client(timeout=timeout) as client:
                    response = client.request(
                        method, url, headers=headers, json=json, params=params
                    )
        except httpx.TimeoutException as exc:
            # Gmail may have acted. Nothing here knows.
            raise GmailProviderError("timeout", ambiguous=True) from exc
        except httpx.HTTPError as exc:
            raise GmailProviderError("transport", ambiguous=True) from exc

        status = response.status_code
        if status == 401:
            raise GmailProviderError("unauthorized", ambiguous=False, unauthorized=True)
        if status == 403:
            # Insufficient scope, or the mailbox no longer permits this client.
            raise GmailProviderError("forbidden", ambiguous=False, unauthorized=True)
        if status == 429 or status >= 500:
            # Rate limiting and server errors are both retryable, and a 5xx in
            # particular can follow a write Gmail already applied.
            raise GmailProviderError(f"http_{status}", ambiguous=True)
        if status >= 400:
            raise GmailProviderError(f"http_{status}", ambiguous=False)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise GmailProviderError("malformed_response", ambiguous=False) from exc

    def create_draft(self, *, access_token: str, raw_message: str) -> GmailDraftHandle:
        payload = self._request(
            "POST",
            "/gmail/v1/users/me/drafts",
            access_token=access_token,
            json={"message": {"raw": raw_message}},
        )
        return _handle_from(payload)

    def find_draft_by_rfc_message_id(
        self, *, access_token: str, rfc_message_id: str
    ) -> GmailDraftHandle | None:
        """One bounded lookup for a draft VMR may already have created.

        The query is an exact ``rfc822msgid:`` match on the deterministic
        ``Message-ID`` this application minted for the exact message version, so
        it cannot match a draft VMR did not write. Gmail returns draft stubs
        rather than message content, and nothing here reads a body.
        """

        payload = self._request(
            "GET",
            "/gmail/v1/users/me/drafts",
            access_token=access_token,
            params={
                "q": f"rfc822msgid:{rfc_message_id.strip('<>')}",
                "maxResults": RECONCILIATION_RESULT_CAP,
            },
        )
        if not isinstance(payload, dict):
            return None
        drafts = payload.get("drafts")
        if not isinstance(drafts, list) or not drafts:
            return None
        return _handle_from(drafts[0])
