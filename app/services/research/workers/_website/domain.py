"""Domain normalization and validation.

Accepts inputs like:
    example.com
    www.example.com
    https://example.com
    https://www.example.com/about
and normalizes to the registered (root) company domain, preserving the input.

Hosted platforms (github.io, wordpress.com subdomains, ...) keep their full
subdomain as the "company site" because the parent domain hosts many
unrelated tenants.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import tldextract

# Extract without live PSL fetch so the tool works offline deterministically.
_extractor = tldextract.TLDExtract(suffix_list_urls=())

# Parent domains that host many unrelated tenants: keep subdomain identity.
MULTI_TENANT_PARENTS = {
    "github.io", "gitlab.io", "netlify.app", "vercel.app", "pages.dev",
    "wordpress.com", "blogspot.com", "wixsite.com", "squarespace.com",
    "weebly.com", "webflow.io", "myshopify.com", "notion.site",
    "herokuapp.com", "web.app", "firebaseapp.com", "azurewebsites.net",
    "carrd.co", "godaddysites.com", "site123.me", "strikingly.com",
}

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,62}[a-z0-9])?$", re.IGNORECASE)


class DomainError(ValueError):
    """Raised when an input cannot be interpreted as a usable domain."""


@dataclass(frozen=True)
class NormalizedDomain:
    original_input: str
    host: str               # host to start crawling from (no scheme)
    registered_domain: str  # identity key for the queue, e.g. example.com
    is_multi_tenant_host: bool = False

    @property
    def start_url(self) -> str:
        return f"https://{self.host}/"


def normalize_domain(raw: str) -> NormalizedDomain:
    if raw is None:
        raise DomainError("Empty domain input")
    text = raw.strip()
    if not text:
        raise DomainError("Empty domain input")
    if any(c.isspace() for c in text):
        raise DomainError(f"Domain contains whitespace: {raw!r}")

    # Add a scheme so urlsplit finds the host.
    candidate = text if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", text) else f"https://{text}"
    parts = urlsplit(candidate)
    host = (parts.hostname or "").strip(".").lower()
    if not host:
        raise DomainError(f"No hostname found in input: {raw!r}")
    if parts.scheme not in ("http", "https"):
        raise DomainError(f"Unsupported scheme '{parts.scheme}' in input: {raw!r}")

    # IDN: store punycode form for consistent matching.
    try:
        host = host.encode("idna").decode("ascii") if not host.isascii() else host
    except UnicodeError as exc:
        raise DomainError(f"Invalid internationalized domain: {raw!r}") from exc

    labels = host.split(".")
    if len(labels) < 2:
        raise DomainError(f"Not a valid public domain: {raw!r}")
    for label in labels:
        if not _HOST_RE.match(label):
            raise DomainError(f"Invalid hostname label {label!r} in {raw!r}")

    ext = _extractor(host)
    subdomain, domain_label, suffix = ext.subdomain, ext.domain, ext.suffix
    if not domain_label or not suffix:
        # Unknown/reserved TLD (e.g. .test, .internal): fall back to the last
        # two labels so lab/test domains still work; real typos fail at fetch.
        if len(labels) >= 2 and labels[-1].isalpha() and len(labels[-1]) >= 2:
            subdomain = ".".join(labels[:-2])
            domain_label = labels[-2]
            suffix = labels[-1]
        else:
            raise DomainError(f"Could not determine registered domain for: {raw!r}")
    registered = f"{domain_label}.{suffix}"

    if registered in MULTI_TENANT_PARENTS and subdomain:
        # tenant site on a shared platform: identity includes the subdomain
        sub = subdomain.split(".")[-1]  # drop www-style prefixes
        key = f"{sub}.{registered}"
        return NormalizedDomain(
            original_input=raw, host=host, registered_domain=key,
            is_multi_tenant_host=True,
        )

    # Start crawling from the exact host given (minus www) if it is a
    # meaningful subdomain, else from the registered domain.
    start_host = host[4:] if host.startswith("www.") else host
    if start_host == registered or not subdomain or subdomain == "www":
        start_host = registered
    return NormalizedDomain(
        original_input=raw, host=start_host, registered_domain=registered,
    )


def same_site(url: str, nd: NormalizedDomain, include_subdomains: bool = True) -> bool:
    """True when url belongs to the company's site."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host.startswith("www."):
        host = host[4:]
    if nd.is_multi_tenant_host:
        return host == nd.registered_domain or host == nd.host
    if host == nd.registered_domain:
        return True
    if include_subdomains and host.endswith("." + nd.registered_domain):
        return True
    return False
