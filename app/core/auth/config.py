"""Configuration for hosted-operator authentication.

Everything in this module is read from the environment under the ``AUTH__``
prefix, exactly like ``FEATURES__``. The two secrets (``AUTH__SESSION_SECRET``
and ``AUTH__GOOGLE_CLIENT_SECRET``) carry ``repr=False`` and ``exclude=True`` so
they never reach ``repr(settings)``, ``settings.model_dump()``, a template, a
log line or the diagnostics screen — the same treatment the provider keys
already get in ``app/core/config.py``.

The approved-operator list is configuration, not data. For the first hosted Beta
that is the right shape: two or three named people, changed by editing
``/etc/vmr/vmr.env`` and restarting, with no screen to build, no table to
migrate, and no way for an application bug to grant access. It is also why a
Google *domain* is not sufficient on its own — "everyone at the company" is a
much larger blast radius than this Beta needs.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# The identity scopes and nothing else. `openid` and `email` are what the
# allow-list decision needs; `profile` supplies the display name shown in the
# account menu. Gmail scopes are deliberately absent: signing in to VMR must
# never imply mailbox authorization, which is a separate grant with separate
# credentials in a later slice.
GOOGLE_IDENTITY_SCOPES: tuple[str, ...] = ("openid", "email", "profile")

# Shortest secret that still makes a signing key worth having. 32 characters of
# `secrets.token_urlsafe(32)` is the documented recipe.
MIN_SESSION_SECRET_CHARS = 32


def normalize_operator_email(value: str) -> str:
    """Return one comparable form of an email address, or ``""`` when unusable.

    Comparison rules are chosen to be *safe*, not clever:

    * **The address must be ASCII.** Anything else is unusable, full stop. This
      is the one rule that decides the others, and it replaces the NFKC
      normalisation this function used to apply. NFKC is a *widening* transform:
      it folds ``ｏperator@vmr.example`` (fullwidth ``o``) and
      ``ＯＰＥＲＡＴＯＲ@ＶＭＲ.ＥＸＡＭＰＬＥ`` onto ``operator@vmr.example``, and so
      created allow-list matches that nobody configured. A normalisation step
      must never be able to turn an address that is *not* on the list into one
      that is. Refusing non-ASCII is strictly narrower than folding it, and it
      costs nothing here: Google issues ASCII ``email`` claims, and the
      configured allow-list is written by hand from those.
    * The address is lower-cased, which for an ASCII-only value is a plain
      per-character mapping with no locale or compatibility behaviour. Google
      issues addresses lower-cased already, and the local part being technically
      case-sensitive in the RFC is not a distinction any real mailbox honours —
      treating ``A@x`` and ``a@x`` as different would create an allow-list that
      silently fails to match.
    * Surrounding whitespace is stripped; interior whitespace makes the value
      unusable rather than being squeezed out.
    * Exactly one ``@`` with a non-empty local part and domain is required.

    Deliberately *not* applied: Gmail's dot-insensitivity and ``+tag``
    stripping. Those are provider-specific delivery conveniences; folding them
    here would make ``a.b@x`` match an allow-list entry of ``ab@x``, which is a
    widening of access that nobody configured.

    The same rule runs on both sides of the comparison — a configured allow-list
    entry is normalised through this function at load time and a non-ASCII one
    makes the process refuse to start — so a lookalike cannot be smuggled in
    from either direction.
    """

    if not value:
        return ""
    candidate = value.strip()
    if not candidate or not candidate.isascii():
        return ""
    candidate = candidate.lower()
    if any(character.isspace() for character in candidate):
        return ""
    local, separator, domain = candidate.partition("@")
    if not separator or not local or not domain or "@" in domain:
        return ""
    return candidate


class AuthSettings(BaseModel):
    """Hosted-operator authentication settings (env prefix ``AUTH__``)."""

    model_config = {"frozen": True}

    enabled: bool = Field(
        default=False,
        description="Require an approved Google identity for operator surfaces.",
    )

    session_secret: str | None = Field(
        default=None,
        repr=False,
        exclude=True,
        description="HMAC signing secret for the operator session cookie (secret).",
    )

    google_client_id: str | None = Field(
        default=None,
        description="Google OAuth 2.0 client id for VMR application identity only.",
    )
    google_client_secret: str | None = Field(
        default=None,
        repr=False,
        exclude=True,
        description="Google OAuth 2.0 client secret (secret).",
    )

    # The allow-list. Empty means "nobody", never "everybody": every decision
    # path treats an empty list as a refusal, and the startup contract refuses
    # to boot a hosted deployment that has one.
    allowed_operator_emails: tuple[str, ...] = Field(
        default=(),
        description="Explicitly approved internal operator email addresses.",
    )

    # Optional second gate, not a substitute for the allow-list. When set, the
    # address must ALSO end in this domain, so a mistyped personal address in
    # the allow-list still cannot sign in.
    allowed_google_domain: str | None = Field(
        default=None,
        description="Optional Workspace domain the approved address must also belong to.",
    )

    # The canonical public origin operators reach. Used to build the OAuth
    # redirect URI, which must match Google Cloud Console byte-for-byte, and to
    # decide same-origin for writes. It is configuration rather than something
    # derived from the Host header because a redirect URI derived from an
    # attacker-influenced header is how open redirectors are built.
    public_base_url: str | None = Field(
        default=None,
        description="Canonical external origin, e.g. https://srv1885453.hstgr.cloud.",
    )

    session_max_age_seconds: int = Field(
        default=12 * 60 * 60,
        gt=0,
        le=7 * 24 * 60 * 60,
        description="Absolute lifetime of one operator session (default 12 hours).",
    )
    login_transaction_max_age_seconds: int = Field(
        default=10 * 60,
        gt=0,
        le=60 * 60,
        description="Lifetime of one in-flight sign-in transaction (default 10 minutes).",
    )

    cookie_secure: bool = Field(
        default=True,
        description="Send auth cookies only over HTTPS. Only local development may unset this.",
    )
    cookie_domain: str | None = Field(
        default=None,
        description="Optional cookie Domain. Unset means host-only, which is the safer default.",
    )

    # Overridable only so a test can point at a stub; the defaults are the
    # documented Google endpoints and are what any real deployment uses.
    google_authorization_endpoint: str = Field(
        default="https://accounts.google.com/o/oauth2/v2/auth",
        description="Google OAuth 2.0 authorization endpoint.",
    )
    google_token_endpoint: str = Field(
        default="https://oauth2.googleapis.com/token",
        description="Google OAuth 2.0 token endpoint (server-to-server, over TLS).",
    )
    google_issuers: tuple[str, ...] = Field(
        default=("https://accounts.google.com", "accounts.google.com"),
        description="Accepted ``iss`` values in a Google ID token.",
    )
    google_request_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=60,
        description="Wall-clock budget for one Google token exchange.",
    )

    @field_validator("allowed_operator_emails")
    @classmethod
    def _normalize_allow_list(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Normalise and de-duplicate at load time, and refuse unusable entries.

        Refusing here rather than silently dropping matters: an operator who
        mistypes their own address should find out when the process refuses to
        start, not when they are denied at the door and cannot tell whether the
        allow-list or the identity provider is at fault. The exception message
        never contains the offending value, because settings errors are logged.

        A non-ASCII entry — a fullwidth or Cyrillic lookalike pasted from a
        document — is one of the unusable shapes, so a deployment configured
        with one refuses to start rather than booting with an allow-list entry
        that no Google identity can ever match.
        """

        normalized: list[str] = []
        for entry in value:
            candidate = normalize_operator_email(entry)
            if not candidate:
                raise ValueError(
                    "AUTH__ALLOWED_OPERATOR_EMAILS must contain only well-formed "
                    "ASCII email addresses"
                )
            if candidate not in normalized:
                normalized.append(candidate)
        return tuple(normalized)

    @field_validator("allowed_google_domain")
    @classmethod
    def _normalize_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # ASCII-only for the same reason as `normalize_operator_email`: a
        # compatibility fold on the domain gate would let a lookalike domain
        # satisfy a gate that was configured for a different one.
        candidate = value.strip()
        if not candidate.isascii():
            raise ValueError("AUTH__ALLOWED_GOOGLE_DOMAIN must be a bare ASCII domain name")
        candidate = candidate.lower().lstrip("@")
        if not candidate or any(character.isspace() for character in candidate) or "@" in candidate:
            raise ValueError("AUTH__ALLOWED_GOOGLE_DOMAIN must be a bare domain name")
        return candidate

    @field_validator("public_base_url")
    @classmethod
    def _normalize_base_url(cls, value: str | None) -> str | None:
        """Keep one canonical scheme+host[:port] origin with no trailing slash."""

        if value is None:
            return None
        candidate = value.strip().rstrip("/")
        if not candidate:
            return None
        scheme, separator, remainder = candidate.partition("://")
        if not separator or scheme.lower() not in {"http", "https"} or not remainder:
            raise ValueError("AUTH__PUBLIC_BASE_URL must be an http(s) origin")
        if "/" in remainder or any(character.isspace() for character in remainder):
            raise ValueError("AUTH__PUBLIC_BASE_URL must be an origin without a path")
        return f"{scheme.lower()}://{remainder.lower()}"

    def has_session_secret(self) -> bool:
        """True when a usable signing secret is configured (never logs it)."""

        secret = (self.session_secret or "").strip()
        return len(secret) >= MIN_SESSION_SECRET_CHARS

    def has_google_client(self) -> bool:
        """True when both halves of the Google identity client are configured."""

        return bool(
            (self.google_client_id or "").strip() and (self.google_client_secret or "").strip()
        )

    def is_approved(self, email: str) -> bool:
        """Whether ``email`` is an explicitly approved internal operator.

        Fails closed on every uncertain input: an unusable address, an empty
        allow-list, or an address outside the optional Workspace domain.
        """

        candidate = normalize_operator_email(email)
        if not candidate or not self.allowed_operator_emails:
            return False
        if self.allowed_google_domain is not None:
            domain = candidate.rpartition("@")[2]
            if domain != self.allowed_google_domain:
                return False
        return candidate in self.allowed_operator_emails

    def redirect_uri(self) -> str | None:
        """The one OAuth redirect URI, or ``None`` when no public origin is set."""

        if self.public_base_url is None:
            return None
        return f"{self.public_base_url}/auth/callback"
