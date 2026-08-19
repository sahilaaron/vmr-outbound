"""What counts as a Report URL — the page Email 2 offers the prospect.

The Campaign's Report URL is the one destination this product deliberately puts
in front of a stranger, so the rules here are stricter than the ones
:mod:`app.services.campaign_offering.urls` applies to an offering page, and they
are strict in a different direction.

**Nothing is repaired.** The offering validator adds ``https://`` to a bare
domain, drops the fragment and gives an empty path a ``/``, because that address
is a *lookup key* for a page VMR reads once. This one is neither: it is copied
verbatim into an email. A dashboard whose route lives after the ``#``, a query
string that selects the segment the prospect is meant to see, a trailing path
that matters — all of them are destroyed by tidying. So the address is accepted
exactly as typed (minus surrounding whitespace) or refused with a sentence the
operator can act on. A refusal an operator can fix in ten seconds is always
better than a silently different link arriving in somebody's inbox.

**The scheme must be written.** ``reports.example.com/carbon`` is refused rather
than promoted to ``https://``. The operator is naming a specific page, and
guessing the scheme of a page nobody here will ever open is guessing about the
one thing that decides whether the link works at all.

**Nothing fetches it, and nothing may.** No code path in this repository opens a
socket to this address, and the language model is never asked to read it, never
shown it, and never given the chance to reproduce it — see
``app/services/personalization/sequence.py`` for how the exact string reaches
Email 2 without passing through a prompt. The private-host refusal below is
therefore not an SSRF guard; it is the observation that a link to
``http://localhost:8000/report`` is meaningless to the person receiving it.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from app.services.campaign_offering.urls import (
    MAX_URL_LENGTH,
    PRIVATE_HOST_NAMES,
    PRIVATE_HOST_SUFFIXES,
)

__all__ = [
    "MAX_RESOURCE_URL_LENGTH",
    "CampaignResourceUrlError",
    "normalize_campaign_resource_url",
    "stored_resource_url",
]

#: The same bound the offering address uses, and the same bound the column
#: carries. Shared rather than restated so the three cannot drift apart.
MAX_RESOURCE_URL_LENGTH = MAX_URL_LENGTH


class CampaignResourceUrlError(ValueError):
    """The Report URL cannot be used, with a sentence an operator can act on."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _refuse(message: str, *, code: str) -> CampaignResourceUrlError:
    return CampaignResourceUrlError(message, code=code)


def _host_is_private_literal(host: str) -> bool:
    """True when the host is an IP literal that is not a public address.

    Deliberately identical in behaviour to the offering validator's rule,
    including unwrapping IPv4-mapped IPv6 so ``[::ffff:127.0.0.1]`` cannot walk
    past the IPv4 case.
    """

    candidate = host
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not address.is_global


def normalize_campaign_resource_url(raw: str | None) -> str:
    """Validate one Report URL and return it exactly as it will be sent.

    The only change made to the operator's input is stripping the whitespace
    around it, which is not part of any address and is what a paste leaves
    behind. Everything else — case, path, query, fragment, port — survives
    untouched, because the value returned here is the value a prospect clicks.
    """

    candidate = (raw or "").strip()
    if not candidate:
        raise _refuse("Enter the address of the report page.", code="resource_url_missing")
    if len(candidate) > MAX_RESOURCE_URL_LENGTH:
        raise _refuse(
            f"That address is longer than {MAX_RESOURCE_URL_LENGTH} characters.",
            code="resource_url_too_long",
        )
    if any(character.isspace() for character in candidate):
        raise _refuse(
            "That does not look like a web address — it contains a space.",
            code="resource_url_malformed",
        )
    # Control characters would survive urlsplit and travel into an email body,
    # where a bare newline is enough to make the rest of a sentence look like a
    # separate line. Refused rather than stripped, for the same reason as above.
    if any(character < " " or character == "\x7f" for character in candidate):
        raise _refuse(
            "That address contains a character that cannot appear in a web address.",
            code="resource_url_malformed",
        )

    parts = urlsplit(candidate)
    if parts.scheme.lower() not in {"http", "https"}:
        # Says what was actually wrong. A relative path and a `javascript:` URL
        # are both refused here and are not the same operator mistake, so the
        # message names the case rather than reporting a shared code.
        #
        # A single-letter scheme is a Windows drive letter, not a protocol:
        # ``C:\reports\market.pdf`` parses as scheme ``c``, and telling
        # somebody who pasted a file path that the *scheme* is unsupported would
        # send them looking for the wrong mistake.
        if not parts.scheme or len(parts.scheme) == 1:
            raise _refuse(
                "Give the full address of the report page, starting with https://.",
                code="resource_url_not_absolute",
            )
        raise _refuse(
            "Only http and https addresses can be shared with a prospect.",
            code="resource_url_scheme_not_allowed",
        )
    if parts.username or parts.password:
        raise _refuse(
            "Remove the username and password from the address.",
            code="resource_url_has_credentials",
        )

    host = (parts.hostname or "").strip().lower()
    if not host:
        raise _refuse("That address has no website name in it.", code="resource_url_malformed")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise _refuse("That address has an impossible port.", code="resource_url_malformed")

    if (
        _host_is_private_literal(host)
        or host in PRIVATE_HOST_NAMES
        or any(host.endswith(suffix) for suffix in PRIVATE_HOST_SUFFIXES)
    ):
        raise _refuse(
            "That address points at a private or internal machine, so nobody outside "
            "your network could open it.",
            code="resource_url_not_public",
        )
    if "." not in host:
        raise _refuse(
            "That does not look like a public web address.",
            code="resource_url_not_public",
        )
    return candidate


def stored_resource_url(value: str | None) -> str | None:
    """The Report URL a Campaign row holds, if it is one that may still be used.

    Read-side companion to :func:`normalize_campaign_resource_url`. A stored
    value is re-validated rather than trusted, because a row can predate a rule
    or have been written by a route that no longer exists, and an address the
    product would refuse to save is one it must also refuse to send. ``None``
    means "this Campaign has no usable Report URL", which is the only thing
    every caller here needs to know.
    """

    try:
        return normalize_campaign_resource_url(value)
    except CampaignResourceUrlError:
        return None
