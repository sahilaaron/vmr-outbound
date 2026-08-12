"""Configuration for Gmail mailbox authorization (#267).

A block of its own, under the ``GMAIL__`` prefix, and deliberately not a few
extra fields on ``AuthSettings``. The two are different authorities:

* ``AUTH__*`` configures the client that answers *who is this operator* and
  requests ``openid email profile`` and nothing else;
* ``GMAIL__*`` configures the client that asks a human for permission to write
  a draft into their mailbox.

Keeping them apart in configuration is what makes it impossible to widen VMR
sign-in into mailbox access with a single environment edit: adding a Gmail scope
to the identity client would have no effect here, and the Gmail client is never
consulted when anybody signs in. It also means the two consent screens, the two
redirect URIs and the two client secrets rotate independently.

The redirect origin is *not* configured here. It is
``AUTH__PUBLIC_BASE_URL``, because a deployment has exactly one canonical
external origin and two copies of it is one copy that can be wrong. The Gmail
callback path is fixed at ``/gmail/callback``.

Both secrets carry ``repr=False`` and ``exclude=True``, so neither reaches
``repr(settings)``, ``settings.model_dump()``, a template, a log line or the
diagnostics screen -- the same treatment ``AUTH__GOOGLE_CLIENT_SECRET`` and the
provider keys already get.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

#: The one Gmail scope this feature requests, plus the two identity scopes it
#: needs in order to say *which mailbox* was connected.
#:
#: ``gmail.compose`` is the narrowest scope Google documents for
#: ``users.drafts.create``; the alternatives (``gmail.modify``,
#: ``https://mail.google.com/``) are strictly wider and would grant inbox read
#: access this feature has no use for. ``gmail.compose`` does technically permit
#: ``users.drafts.send`` -- Google does not offer a create-only draft scope --
#: which is precisely why the adapter implements no send call and a regression
#: test asserts that no send endpoint is reachable from any application route.
#:
#: ``openid`` and ``email`` are here for a specific reason rather than by habit.
#: ``users.getProfile`` -- the obvious way to ask "whose mailbox is this?" --
#: requires ``gmail.metadata`` or wider, so using it would mean requesting more
#: mailbox access than drafting needs merely to learn an address. Asking for the
#: two identity scopes instead makes Google return an ID token in the same token
#: response, which names the account with a signature VMR already knows how to
#: verify, and grants no additional access to mail.
GMAIL_AUTHORIZATION_SCOPES: tuple[str, ...] = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.compose",
)

#: The scope that must be present in Google's grant for drafting to be possible.
#: Checked against what was *returned*, never against what was asked for: a
#: consent screen where the operator unticks the mailbox permission returns a
#: narrower grant, and a connection that silently recorded the requested list
#: would then claim a capability it does not have.
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"

#: The fixed callback path. Fixed rather than configurable because it has to
#: match the Google Cloud Console entry byte for byte, and a value that can
#: drift between two places is a value that will.
GMAIL_CALLBACK_PATH = "/gmail/callback"


class GmailSettings(BaseModel):
    """Gmail mailbox authorization settings (env prefix ``GMAIL__``)."""

    model_config = {"frozen": True}

    client_id: str | None = Field(
        default=None,
        description="Google OAuth 2.0 client id for the Gmail mailbox grant only.",
    )
    client_secret: str | None = Field(
        default=None,
        repr=False,
        exclude=True,
        description="Google OAuth 2.0 client secret for the Gmail grant (secret).",
    )

    # A dedicated key-encryption key, following the convention
    # `PROVIDER_CREDENTIAL_ENCRYPTION_KEY` established for Agent Studio
    # credentials. Dedicated rather than shared so that rotating one credential
    # domain never forces a rotation of the other, and so a key leaked from one
    # does not decrypt the other. There is deliberately no fallback: without an
    # explicit Fernet key, connecting a mailbox is unavailable rather than
    # silently storing a token in the clear.
    token_encryption_key: str | None = Field(
        default=None,
        repr=False,
        exclude=True,
        description="Fernet key for Gmail OAuth tokens at rest (secret).",
    )

    # Overridable only so a test can point at a stub; the defaults are the
    # documented Google endpoints and are what any real deployment uses.
    authorization_endpoint: str = Field(
        default="https://accounts.google.com/o/oauth2/v2/auth",
        description="Google OAuth 2.0 authorization endpoint.",
    )
    token_endpoint: str = Field(
        default="https://oauth2.googleapis.com/token",
        description="Google OAuth 2.0 token endpoint (server-to-server, over TLS).",
    )
    revocation_endpoint: str = Field(
        default="https://oauth2.googleapis.com/revoke",
        description="Google OAuth 2.0 revocation endpoint.",
    )
    api_base_url: str = Field(
        default="https://gmail.googleapis.com",
        description="Gmail REST API origin.",
    )
    request_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        le=60,
        description="Wall-clock budget for one Google or Gmail request.",
    )
    authorization_transaction_max_age_seconds: int = Field(
        default=10 * 60,
        gt=0,
        le=60 * 60,
        description="Lifetime of one in-flight Gmail authorization (default 10 minutes).",
    )

    #: The domain used in the ``Message-ID`` VMR mints for each draft. A
    #: registrable domain the deployment controls is the correct RFC 5322 value;
    #: it is configurable because this application does not otherwise know one.
    message_id_domain: str = Field(
        default="vmr-outbound.invalid",
        description="Domain part of the Message-ID minted for each Gmail draft.",
    )

    @field_validator("message_id_domain")
    @classmethod
    def _normalize_message_id_domain(cls, value: str) -> str:
        candidate = value.strip().lower()
        if not candidate or not candidate.isascii():
            raise ValueError("GMAIL__MESSAGE_ID_DOMAIN must be a bare ASCII domain name")
        if any(character in candidate for character in ("@", "<", ">", "/", " ", "\t")):
            raise ValueError("GMAIL__MESSAGE_ID_DOMAIN must be a bare domain name")
        return candidate

    def has_client(self) -> bool:
        """True when both halves of the Gmail OAuth client are configured."""

        return bool((self.client_id or "").strip() and (self.client_secret or "").strip())

    def has_encryption_key(self) -> bool:
        """True when a token-encryption key is configured (never logs it)."""

        return bool((self.token_encryption_key or "").strip())

    def is_configured(self) -> bool:
        """Whether a mailbox could be connected at all in this deployment."""

        return self.has_client() and self.has_encryption_key()
