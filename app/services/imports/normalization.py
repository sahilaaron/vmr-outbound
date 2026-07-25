"""Conservative field normalization for imported contacts (DAT-003).

Normalization here is deliberately *lossless in meaning*: it trims and collapses
whitespace, lower-cases values that are genuinely case-insensitive (email
addresses, hostnames), and canonicalizes obvious URL noise. It never guesses,
title-cases names, expands abbreviations, or maps country synonyms — those are
lossy and would risk silently changing a person's data. The original values are
always preserved on the immutable raw row, so normalization can stay cautious.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit

_WHITESPACE = re.compile(r"\s+")
# A pragmatic email shape check: one @, non-space local and domain, a dotted
# domain. Not RFC-complete on purpose — it rejects the obviously malformed
# without pretending to prove deliverability (that is verification, a later phase).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Hostname label validation (each dot-separated label, letters/digits/hyphen).
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def collapse_whitespace(value: str | None) -> str | None:
    """Trim and collapse internal runs of whitespace to a single space."""

    if value is None:
        return None
    collapsed = _WHITESPACE.sub(" ", value).strip()
    return collapsed or None


def normalize_name(value: str | None) -> str | None:
    """Normalize a person or company name: trim + collapse whitespace only.

    Case is preserved: "McDonald", "O'Brien", and "van der Berg" must not be
    mangled by naive title-casing.
    """

    return collapse_whitespace(value)


def normalize_text(value: str | None) -> str | None:
    """Normalize a free-text field (title, industry, company size): trim/collapse."""

    return collapse_whitespace(value)


def normalize_country(value: str | None) -> str | None:
    """Normalize a country value conservatively.

    Trim and collapse whitespace; upper-case values that look like a 2- or
    3-letter ISO code. Full country-name canonicalization needs a curated map and
    is deferred (it is not required to import a batch).
    """

    collapsed = collapse_whitespace(value)
    if collapsed is None:
        return None
    if len(collapsed) in (2, 3) and collapsed.isalpha():
        return collapsed.upper()
    return collapsed


def normalize_email(value: str | None) -> str | None:
    """Lower-case and trim an email address. Returns None for empty input.

    Validity is checked separately by :func:`is_valid_email`; this only
    canonicalizes. Lower-casing is safe: mail systems treat the domain
    case-insensitively and, in practice, the local part too.
    """

    if value is None:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def is_valid_email(value: str) -> bool:
    """Return True if *value* has a plausible email shape."""

    return bool(_EMAIL_RE.match(value))


def normalize_domain(value: str | None) -> str | None:
    """Extract and normalize a hostname from a company-domain cell.

    Accepts bare domains ("Acme.COM"), full URLs ("https://www.acme.com/about"),
    and values with a leading "www.". Returns the lower-cased registrable
    hostname, or None if nothing host-like can be extracted.
    """

    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None

    # If it looks like a URL, parse the network location; otherwise treat the
    # whole string as a host and drop any path/query fragment.
    if "://" in raw:
        host = urlsplit(raw).netloc
    else:
        host = raw.split("/", 1)[0]

    host = host.strip().lower()
    # Drop credentials and port if present.
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    host = host.strip(".")
    return host or None


def is_valid_hostname(value: str) -> bool:
    """Return True if *value* is a syntactically valid, dotted hostname."""

    return bool(_HOSTNAME_RE.match(value))


def normalize_linkedin_url(value: str | None) -> str | None:
    """Normalize a LinkedIn/profile URL for provenance (not scraped).

    Trims, ensures an https scheme, lower-cases the host, and strips a trailing
    slash. Path case is preserved (some profile slugs are case-sensitive to the
    source). Returns None if the value has no host.
    """

    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = parts.netloc.lower()
    if not host:
        return None
    path = parts.path.rstrip("/")
    normalized = f"https://{host}{path}"
    if parts.query:
        normalized += f"?{parts.query}"
    return normalized


def normalize_linkedin_profile_url(value: str | None) -> str | None:
    """Normalize a LinkedIn PERSON-profile URL into the exact identity key.

    This is the single normalization applied to *every* side of profile-identity
    matching (existing contacts, imports, SalesNav rows, and profile captures —
    DAT-012E): builds on :func:`normalize_linkedin_url`, then requires a
    linkedin.com host and a MAIN ``/in/<public-identifier>`` path, drops the
    query string and fragment (tracking context, never identity), and lower-cases
    the public identifier — LinkedIn identifiers are case-insensitive, so
    ``/in/Jane-Doe`` and ``/in/jane-doe`` are the same person.

    Returns ``None`` for anything that is not a main profile URL (company pages,
    Sales Navigator lead URLs, sub-routes, non-LinkedIn hosts). It never repairs
    or invents an identity.
    """

    base = normalize_linkedin_url(value)
    if base is None:
        return None
    parts = urlsplit(base)
    host = parts.netloc.lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    match = re.fullmatch(r"/in/([^/]+)", parts.path.rstrip("/"))
    if match is None or not match.group(1):
        return None
    slug = unquote(match.group(1)).lower()
    return f"https://www.linkedin.com/in/{quote(slug, safe='')}"


def normalize_linkedin_company_url(value: str | None) -> str | None:
    """Normalize a LinkedIn COMPANY URL into the exact identity key.

    Mirrors :func:`normalize_linkedin_profile_url` for ``/company/<id>`` pages:
    canonical ``www`` host, no query/fragment, no trailing ``/about`` segment,
    lower-cased identifier. Returns ``None`` for non-company URLs.
    """

    base = normalize_linkedin_url(value)
    if base is None:
        return None
    parts = urlsplit(base)
    host = parts.netloc.lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    match = re.fullmatch(r"/company/([^/]+)(?:/about)?", parts.path.rstrip("/"))
    if match is None or not match.group(1):
        return None
    slug = unquote(match.group(1)).lower()
    if slug == "unavailable":
        return None
    return f"https://www.linkedin.com/company/{quote(slug, safe='')}"


def build_natural_key(first_name: str, last_name: str, domain: str) -> str:
    """Build the deterministic dedup fingerprint for an email-less contact.

    Case-insensitive on the name parts; the domain is already normalized
    lower-case. This is an *exact* key — it never fuzzy-matches similar names.
    """

    return f"{first_name.casefold()}|{last_name.casefold()}|{domain}"
