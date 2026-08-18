"""What counts as an offering page address VMR will ask Claude to read.

**This module is not a fetcher, and the application still has none.** Nothing in
this package opens a socket to the address it validates. The page is read by the
Claude CLI's own ``WebFetch`` tool, under the operator's own subscription, in the
same way ``app/services/research/fallback.py`` already reads other organisations'
public pages. Adding a server-side fetcher here would have created exactly the
unrestricted request forgery surface the repository does not have.

So what is this for, if the server never makes the request? Two things, and both
are worth the code:

1. **A private address must never be typed into a prompt.** The model's fetch
   runs on the worker host, so ``http://localhost:8000/admin`` or
   ``http://169.254.169.254/`` would be resolved *there*, with that host's
   network position. The tool may well refuse; relying on that would be trusting
   another program's policy for our own safety property. Refusing the address
   before it reaches the prompt is ours to do, and it is cheap.
2. **A malformed address should fail on the form, not in a queue.** An operator
   who typed a sentence into the URL box should be told so on the page, in a
   second, rather than watching a job spend a model call to discover it.

What this deliberately does *not* claim: it cannot police redirects. A public URL
may redirect anywhere, and only the fetching tool sees where it landed. That is
why the run records what the model reports it actually read
(``source_url_read``) rather than assuming it read what it was given, and why the
prompt states plainly that a page which redirects somewhere private must be
reported as unreadable rather than summarised.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH = 2048

#: Host suffixes that name a machine on the worker's own network rather than a
#: public page. ``.local``/``.internal``/``.home.arpa`` are the reserved private
#: namespaces; ``localhost`` is matched exactly and as a suffix.
PRIVATE_HOST_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".home.arpa",
)

PRIVATE_HOST_NAMES: frozenset[str] = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


class OfferingUrlError(ValueError):
    """The address cannot be used, with a sentence an operator can act on."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _refuse(message: str, *, code: str) -> OfferingUrlError:
    return OfferingUrlError(message, code=code)


def _host_is_private_literal(host: str) -> bool:
    """True when the host is an IP literal that is not a public address.

    A public IP literal is allowed: unusual for an offering page, but legitimate,
    and refusing it would be a rule about tidiness rather than about safety.
    Everything else — loopback, private, link-local (which includes the cloud
    metadata address), multicast, reserved and unspecified — is refused, in both
    address families, and IPv4-mapped IPv6 is unwrapped first so
    ``[::ffff:127.0.0.1]`` cannot walk past the IPv4 rule.
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


def normalize_offering_url(raw: str) -> tuple[str, str]:
    """Validate one address and return ``(normalized_url, host)``.

    Tolerant in exactly one way, matching ``app/services/seller/generate.py``: a
    bare domain is given ``https://``, because an operator pasting their own
    product page should not have to remember the scheme. Everything else is
    refused rather than repaired — a URL nobody can read is better rejected on
    the form than guessed at.
    """

    candidate = (raw or "").strip().strip(",")
    if not candidate:
        raise _refuse("Enter the address of the offering page.", code="url_missing")
    if len(candidate) > MAX_URL_LENGTH:
        raise _refuse(
            f"That address is longer than {MAX_URL_LENGTH} characters.",
            code="url_too_long",
        )
    if any(character.isspace() for character in candidate):
        raise _refuse(
            "That does not look like a web address — it contains a space.",
            code="url_malformed",
        )
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"}:
        raise _refuse(
            "Only http and https addresses can be read.",
            code="url_scheme_not_allowed",
        )
    # Credentials in the address are refused rather than stripped: an operator
    # who pasted one has pasted a secret, and this row is durable.
    if parts.username or parts.password:
        raise _refuse(
            "Remove the username and password from the address.",
            code="url_has_credentials",
        )

    host = (parts.hostname or "").strip().lower()
    if not host:
        raise _refuse("That address has no website name in it.", code="url_malformed")

    if _host_is_private_literal(host):
        raise _refuse(
            "That address points at a private or internal machine, so it cannot be read.",
            code="url_not_public",
        )
    if host in PRIVATE_HOST_NAMES or any(host.endswith(s) for s in PRIVATE_HOST_SUFFIXES):
        raise _refuse(
            "That address points at a private or internal machine, so it cannot be read.",
            code="url_not_public",
        )
    if "." not in host:
        # A single label is either an intranet name or a typo. Both are wrong for
        # a public offering page, and the message says which one we assumed.
        raise _refuse(
            "That does not look like a public web address.",
            code="url_not_public",
        )

    netloc = host
    if parts.port is not None:
        if not 1 <= parts.port <= 65535:
            raise _refuse("That address has an impossible port.", code="url_malformed")
        netloc = f"{host}:{parts.port}"

    # The fragment is dropped: it never reaches a server and would only make two
    # requests for the same page look like different work.
    normalized = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    if len(normalized) > MAX_URL_LENGTH:
        raise _refuse(
            f"That address is longer than {MAX_URL_LENGTH} characters.",
            code="url_too_long",
        )
    return normalized, host
