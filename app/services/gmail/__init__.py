"""Gmail mailbox authorization and Gmail *draft* creation (#267).

Five modules, and the boundary between them is the point:

``tokens``    the encrypted-at-rest envelope for OAuth tokens. Nothing else in
              the application touches ciphertext.
``oauth``     the Gmail authorization-code client: consent URL, code exchange,
              refresh, revoke. A protocol, so tests inject a deterministic stub.
``provider``  the Gmail REST adapter. It owns request/response translation and
              nothing else: no lineage, no idempotency, no policy.
``mailbox``   grant persistence and the connected/reconnect-required state an
              operator sees.
``drafts``    the application service. It owns authorization, the stale-version
              check, lineage, idempotency and transaction semantics.

**This package creates drafts. It does not send.** There is no send method on
the provider protocol, no send call in the HTTP adapter, and no route that could
reach one -- asserted, not merely intended, by
``tests/test_gmail_draft_integration.py::test_no_send_endpoint_is_reachable``.
"""

from __future__ import annotations
