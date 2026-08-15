"""Application configuration.

All configuration is read from environment variables (optionally via a local
``.env`` file). No secrets are committed to source control — see ``.env.example``
for the required variable names.

Phase 0 scope: local development only. The RDS/production variable *names* are
documented here and in ``.env.example`` but no production credentials exist in
the repository.
"""

from __future__ import annotations

import os
from functools import lru_cache
from ipaddress import IPv6Address, ip_address

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.auth.config import AuthSettings
from app.core.auth.extension import ExtensionAuthSettings
from app.core.features import FeatureFlags
from app.core.gmail_config import GmailSettings
from app.core.sheets_config import SheetsIntegrationSettings


def canonical_trusted_host(value: str) -> str:
    """Return one port-free canonical Host allow-list entry.

    DNS names are case-insensitive and a terminal root dot is semantically
    insignificant. Bracketed IPv6 literals stay bracketed so they cannot be
    confused with ``host:port`` syntax.
    """

    if value != value.strip():
        raise ValueError("trusted hosts must not have leading or trailing whitespace")
    raw = value.lower()
    wildcard = raw.startswith("*.")
    if wildcard:
        raw = raw[2:]
    if not raw or any(character in raw for character in ("\r", "\n", "/", " ", "\t")):
        raise ValueError("trusted hosts must be bounded hostnames without spaces or paths")
    if raw.startswith("[") and raw.endswith("]"):
        try:
            address = ip_address(raw[1:-1])
        except ValueError as exc:
            raise ValueError("trusted hosts contain an invalid bracketed IPv6 address") from exc
        if not isinstance(address, IPv6Address) or wildcard:
            raise ValueError("trusted hosts contain an invalid bracketed IPv6 address")
        return f"[{address.compressed}]"
    if ":" in raw:
        raise ValueError("trusted hosts must not include ports or raw IPv6 addresses")
    raw = raw.removesuffix(".")
    if not raw or len(raw) > 253:
        raise ValueError("trusted hosts must contain bounded non-empty hostnames")
    return f"*.{raw}" if wildcard else raw


def _env_file_for_environment() -> str | None:
    """Which dotenv file to read, or ``None`` under test.

    The automated suite must never inherit the operator's ``.env``. If it did:
    feature switches that default off would arrive on; provider credentials
    would be reachable from a test and could spend real MillionVerifier credits;
    and ``monkeypatch.delenv`` would appear to disable a flag while ``.env``
    silently re-enabled it — because a real environment variable outranks
    dotenv, so deleting that variable falls straight back to the file.

    ``VMR_TEST_MODE`` is set by the root ``conftest.py`` before any application
    module is imported, so the first ``Settings()`` built in the process already
    sees it. Nothing sets it outside tests, so normal behaviour is unchanged.
    """

    return None if os.getenv("VMR_TEST_MODE") == "1" else ".env"


class Settings(BaseSettings):
    """Typed application settings loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=_env_file_for_environment(),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- Application ---------------------------------------------------------
    app_name: str = "VMR Outbound Agent"
    app_env: str = Field(
        default="local",
        description="Deployment environment label: local | ci | staging | production.",
    )
    debug: bool = False
    release_id: str = Field(
        default="unknown",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}$",
        description=(
            "Deployment-provided release identifier; never discovered from Git at request time."
        ),
    )

    # Hosts accepted from the HTTP Host header. Local defaults intentionally
    # name only loopback/test hosts; staging and production must provide their
    # own explicit hostnames and may never use a wildcard.
    trusted_hosts: tuple[str, ...] = Field(
        default=("localhost", "127.0.0.1", "[::1]", "testserver"),
        min_length=1,
        description="Host header allow-list understood by Starlette TrustedHostMiddleware.",
    )

    @field_validator("trusted_hosts")
    @classmethod
    def _canonicalize_trusted_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(canonical_trusted_host(host) for host in value)

    # Forwarded headers are ignored unless the immediate TCP peer belongs to
    # one of these networks. Loopback is the safe local Nginx default; a remote
    # proxy network must be supplied explicitly by deployment configuration.
    trusted_proxy_cidrs: tuple[str, ...] = Field(
        default=("127.0.0.1/32", "::1/128"),
        description="Networks whose direct peers may supply X-Forwarded-* headers.",
    )
    # The app can reject declared oversized requests before reading the body.
    # The reverse proxy remains responsible for complete enforcement, including
    # requests without Content-Length and chunked transfer encoding.
    max_request_bytes: int = Field(
        default=25 * 1024 * 1024,
        gt=0,
        description="Application-wide ceiling for a declared HTTP request body.",
    )
    hsts_max_age_seconds: int = Field(
        default=31_536_000,
        ge=0,
        description=(
            "HSTS max-age emitted only when HTTPS is known directly or via a trusted proxy."
        ),
    )

    # --- Database ------------------------------------------------------------
    # Local dev default points at the documented local Postgres instance.
    # In staging/production this is supplied by the environment (RDS). The
    # value is a full SQLAlchemy URL; credentials never live in source.
    database_url: str = Field(
        default="postgresql+psycopg://dev@127.0.0.1:5433/vmr_dev",
        description="SQLAlchemy database URL. Supplied by the environment outside local dev.",
    )
    database_connect_timeout_seconds: int = Field(
        default=5,
        ge=1,
        le=30,
        description=(
            "Application connection timeout and readiness connection-establishment backstop."
        ),
    )
    readiness_timeout_seconds: float = Field(
        default=2.0,
        gt=0,
        le=30,
        description="End-to-end wall-clock budget for one readiness check.",
    )

    # --- Safety switches -----------------------------------------------------
    # Dry-run defaults ON so that no environment can schedule real email
    # without an explicit, deliberate opt-out. See GOAL.md / AGENTS.md.
    dry_run: bool = Field(
        default=True,
        description="When true, the workflow completes without scheduling real email.",
    )

    # --- Operator workbench --------------------------------------------------
    # Local directory holding short-lived staged uploads for the preview ->
    # confirm import flow. Never a database; see services/imports/staging.py.
    staged_uploads_dir: str = Field(
        default="var/staged_uploads",
        description="Directory for short-lived staged uploads (preview -> confirm flow).",
    )
    # Maximum accepted spreadsheet upload size. Oversized files are rejected
    # before parsing or staging. Conservative default: 25 MB.
    max_upload_bytes: int = Field(
        default=25 * 1024 * 1024,
        gt=0,
        description="Maximum spreadsheet upload size in bytes (default 25 MB).",
    )

    # --- Sales Navigator capture intake (DAT-009, local only) ----------------
    # Loopback base URL used to build the operator_workbench_url returned to the
    # capture extension. Must be a loopback origin; the extension only renders
    # the returned deep link when it is loopback.
    operator_base_url: str = Field(
        default="http://127.0.0.1:8000",
        description="Loopback base URL for operator workbench deep links (local only).",
    )
    # Maximum accepted Sales Navigator intake body size. The contract caps a
    # batch at 500 records of result-page-visible fields; 2 MB is a generous
    # ceiling. Oversized bodies are rejected with 413 before JSON parsing.
    salesnav_intake_max_bytes: int = Field(
        default=2 * 1024 * 1024,
        gt=0,
        description="Maximum Sales Navigator intake body size in bytes (default 2 MB).",
    )
    # Wall-clock budget for a single intake staging operation. Enforced
    # cooperatively inside the synchronous service (deadline checks) and, as a
    # database-side backstop, via PostgreSQL ``statement_timeout``. On breach the
    # staging transaction is rolled back and the request returns 504. Staging a
    # <=500-record batch takes milliseconds locally, so 15 s is conservative
    # without being flaky on a cold database.
    salesnav_intake_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        description="Wall-clock budget in seconds for one intake staging operation (default 15).",
    )

    # --- LinkedIn profile capture intake (DAT-012D, local only) ---------------
    # One reviewed profile snapshot (top card + experience entries with their
    # verbatim raw lines) is far smaller than a 500-record batch; 1 MB is a
    # generous ceiling. Oversized bodies are rejected with 413 before parsing.
    linkedin_profile_intake_max_bytes: int = Field(
        default=1 * 1024 * 1024,
        gt=0,
        description="Maximum LinkedIn profile intake body size in bytes (default 1 MB).",
    )
    linkedin_profile_intake_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        description="Wall-clock budget in seconds for one profile snapshot staging (default 15).",
    )
    # One company snapshot is small; 512 KB is a generous ceiling.
    linkedin_company_intake_max_bytes: int = Field(
        default=512 * 1024,
        gt=0,
        description="Maximum LinkedIn company intake body size in bytes (default 512 KB).",
    )

    # --- Contact-first capture intake (DAT-013, local only) -------------------
    # One submission may carry up to 500 reviewed people, each with its verbatim
    # raw snapshot, so the ceiling is larger than a single-profile capture.
    # Oversized bodies are rejected with 413 before parsing.
    contact_capture_intake_max_bytes: int = Field(
        default=8 * 1024 * 1024,
        gt=0,
        description="Maximum contact-capture submission body size in bytes (default 8 MB).",
    )
    # Wall-clock budget for one submission. Reconciling a 500-person submission
    # does far more work than staging a single profile, so the budget is larger;
    # a breach rolls the whole submission back and returns 504.
    contact_capture_intake_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Wall-clock budget in seconds for one contact-capture submission.",
    )

    # --- Company-domain enrichment via logo.dev (DAT-010, local only) --------
    # The official logo.dev Search Brands by Name API key. Read from
    # ``LOGO_DEV_API_KEY``. It is a SECRET: ``repr=False`` and ``exclude=True``
    # keep it out of ``repr(settings)`` and ``settings.model_dump()`` so it is
    # never accidentally logged, serialized into a template, or dumped to disk.
    # When unset, the enrichment lookup reports "API not configured" rather than
    # calling out; no domain is ever invented. No key is committed to source.
    logo_dev_api_key: str | None = Field(
        default=None,
        repr=False,
        exclude=True,
        description="logo.dev Search Brands API key (secret; supplied via LOGO_DEV_API_KEY).",
    )
    # Base URL for the logo.dev Search Brands by Name endpoint. Overridable only
    # so tests can point at a stub; production uses the documented default.
    logo_dev_search_url: str = Field(
        default="https://api.logo.dev/search",
        description="logo.dev Search Brands by Name endpoint.",
    )
    # Wall-clock budget for a single logo.dev lookup. A slow or hung provider is
    # treated as "API unavailable" and never blocks the operator indefinitely.
    logo_dev_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description="Wall-clock budget in seconds for one logo.dev lookup (default 10).",
    )
    # Upper bound on candidates surfaced per company. The operator still chooses
    # explicitly; this only bounds the list so a noisy response stays reviewable.
    logo_dev_max_candidates: int = Field(
        default=10,
        gt=0,
        description="Maximum logo.dev candidates surfaced per company (default 10).",
    )

    def has_logo_dev_key(self) -> bool:
        """True when a non-empty logo.dev API key is configured (never logs it)."""

        return bool(self.logo_dev_api_key and self.logo_dev_api_key.strip())

    # --- MillionVerifier exact-address verification (Phase 2) ----------------
    # The MillionVerifier Single API key. Read from ``MILLIONVERIFIER_API_KEY``.
    # It is a SECRET: ``repr=False`` and ``exclude=True`` keep it out of
    # ``repr(settings)`` and ``settings.model_dump()`` so it can never be logged,
    # serialized into a template, or dumped to disk. When unset, verification runs
    # in a safe deterministic simulation and no live call is ever made. No key is
    # committed to source. The documented test keys (``API_KEY_FOR_OK`` etc.) are
    # accepted and route to the simulator, never to the network.
    millionverifier_api_key: str | None = Field(
        default=None,
        repr=False,
        exclude=True,
        description="MillionVerifier Single API key (secret; via MILLIONVERIFIER_API_KEY).",
    )
    # Documented Single API endpoint. Overridable only so tests can point at a
    # stub; production uses the documented default.
    millionverifier_base_url: str = Field(
        default="https://api.millionverifier.com/api/v3",
        description="MillionVerifier Single API endpoint.",
    )
    # Per-call connection timeout passed to the provider (documented range 2-60s)
    # and used as the local wall-clock budget for one HTTP call.
    millionverifier_timeout_seconds: int = Field(
        default=20,
        ge=2,
        le=60,
        description="MillionVerifier per-call timeout in seconds (documented 2-60).",
    )
    # Maximum attempts for one verification job (initial try + retries). Only
    # transient failures (provider error, timeout, IP block) consume a retry; a
    # definite address result never retries.
    verification_max_attempts: int = Field(
        default=4,
        ge=1,
        le=10,
        description="Max attempts per verification job (transient failures only).",
    )
    # Base backoff in seconds for retry scheduling; grows exponentially per attempt
    # with bounded jitter (see services/verification/queue.py).
    verification_retry_base_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Base backoff seconds for verification retries (exponential + jitter).",
    )
    verification_retry_max_seconds: float = Field(
        default=1800.0,
        gt=0,
        description="Ceiling for a single verification retry backoff window.",
    )
    # Lease duration for a claimed job. A worker that dies without finishing lets
    # the lease expire so the job is safely reclaimed (interrupted-worker recovery).
    verification_lease_seconds: float = Field(
        default=120.0,
        gt=0,
        description="Seconds a claimed verification job stays leased before reclaim.",
    )
    # Freshness TTLs (days) per address-level result under the active policy. A
    # result older than its TTL is *stale*: reused only as amber evidence and
    # eligible for recheck, never as a fresh green pass.
    verification_ttl_valid_days: int = Field(default=30, gt=0)
    verification_ttl_invalid_days: int = Field(default=90, gt=0)
    verification_ttl_catch_all_days: int = Field(default=7, gt=0)
    verification_ttl_unknown_days: int = Field(default=3, gt=0)
    verification_ttl_disposable_days: int = Field(default=30, gt=0)

    # Estimated cost per *billed* MillionVerifier credit, in
    # ``millionverifier_currency``. This is a local estimate for cost visibility,
    # not a quote: MillionVerifier's Single API does not report a per-call price,
    # so set this to your plan's effective per-email rate. Default 0.0 means "rate
    # not configured" — the ledger still records units (credits) consumed, and the
    # UI shows credits with the cost left explicitly unestimated rather than
    # fabricating a number.
    millionverifier_cost_per_credit: float = Field(
        default=0.0,
        ge=0,
        description="Local estimated cost per billed MillionVerifier credit (0 = not set).",
    )
    millionverifier_currency: str = Field(
        default="USD",
        min_length=3,
        max_length=3,
        description="ISO currency code for MillionVerifier cost estimates.",
    )

    # Dedicated key-encryption key for credential versions created in Agent
    # Studio. There is deliberately no fallback key: without an explicit Fernet
    # key, credential writes and live Studio calls are unavailable.
    provider_credential_encryption_key: str | None = Field(
        default=None,
        repr=False,
        exclude=True,
        description="Fernet key for encrypted provider credentials.",
    )

    def has_millionverifier_key(self) -> bool:
        """True when a non-empty MillionVerifier key is configured (never logs it)."""

        return bool(self.millionverifier_api_key and self.millionverifier_api_key.strip())

    # --- Local Claude CLI (Research, Insights and Personalization Agents) ---
    #
    # These Agents call the operator's own ``claude`` executable under their
    # existing subscription. No key is stored here and no paid API is involved,
    # which is the reason this path was chosen over an SDK.
    claude_cli_path: str = Field(
        default="claude",
        description="Executable name or absolute path for the local Claude CLI.",
    )
    # The CLI's flags have changed between releases. Keeping argv in settings
    # means a CLI upgrade is an .env edit rather than a code change. The literal
    # token "{allowed_tools}" is replaced per call, and drops out entirely when
    # the call permits no tools.
    claude_cli_arguments: tuple[str, ...] = Field(
        default=("-p", "--output-format", "json", "{allowed_tools}"),
        description="Argument template passed to the Claude CLI; the prompt arrives on stdin.",
    )
    # Wall-clock ceiling for one call. A hung CLI must fail the stage, not hold
    # a job lease until it expires and the work is silently retried.
    claude_cli_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description="Maximum wall-clock seconds for one local Claude CLI call.",
    )
    claude_cli_working_directory: str | None = Field(
        default=None,
        description="Optional working directory for the Claude CLI subprocess.",
    )
    # Recorded verbatim on every dossier and draft this producer writes, so a
    # stored artefact can always answer "what produced this?".
    claude_cli_version_label: str = Field(
        default="claude-cli/v1",
        description="Producer version recorded alongside anything the CLI produces.",
    )
    # One domain question, not a dossier. Far below `claude_cli_timeout_seconds`
    # on purpose: a backfill pass walks up to fifty captures, and a stalled call
    # on the third must not consume the window the other forty-seven needed.
    model_domain_lookup_timeout_seconds: float = Field(
        default=90.0,
        gt=0,
        description="Maximum wall-clock seconds for one model company-domain lookup.",
    )

    # --- Research Claude CLI web-research fallback (RES-002) -----------------
    #
    # These bound the one Claude CLI call the Research Agent may make after its
    # deterministic website worker produced nothing usable. The capability itself
    # is gated by ``FEATURES__RESEARCH_CLAUDE_FALLBACK``, which defaults off;
    # these values only decide how far the call may go once it is allowed.
    research_claude_fallback_timeout_seconds: float = Field(
        default=240.0,
        gt=0,
        description="Maximum wall-clock seconds for one Research Claude CLI fallback call.",
    )
    # A ceiling on the *accepted* source URLs, stated to the model as its search
    # and fetch budget and enforced deterministically on the way back in. The
    # CLI's internal tool loop is not observable from this process, so this is
    # the honest boundary: how much evidence may be persisted, not how many
    # requests the CLI made.
    research_claude_fallback_max_sources: int = Field(
        default=8,
        gt=0,
        description="Maximum distinct source URLs accepted from one Research fallback call.",
    )
    research_claude_fallback_max_evidence_items: int = Field(
        default=20,
        gt=0,
        description="Maximum sourced claims accepted from one Research fallback call.",
    )
    # Recorded verbatim on every fact, dossier and job result this fallback
    # produces, so a stored claim can always answer "which producer wrote this,
    # under which contract?" without inferring it from the text.
    research_claude_fallback_producer_version: str = Field(
        default="research-claude-fallback/1",
        description="Producer version recorded on everything the Research fallback produces.",
    )
    # The narrowest permission set that still allows the two capabilities this
    # fallback exists for: finding pages and reading them. Deliberately not the
    # Insights/Personalization `allowed_tools=()`, and deliberately not wider —
    # no shell, no file access, no editing.
    research_claude_fallback_allowed_tools: tuple[str, ...] = Field(
        default=("WebSearch", "WebFetch"),
        description="Claude CLI tools the Research fallback may use. Web read-only.",
    )

    features: FeatureFlags = Field(default_factory=FeatureFlags)

    # Hosted-operator authentication (env prefix ``AUTH__``). Defaults to off so
    # local development is unchanged; the startup contract in
    # ``app/core/auth/startup.py`` makes it mandatory for any hosted environment.
    auth: AuthSettings = Field(default_factory=AuthSettings)

    # The Chrome capture extension's own credential (env prefix
    # ``EXTENSION_AUTH__``). Separate from ``auth`` on purpose: that block is the
    # human operator's browser session and the Google identity client behind it,
    # and neither may ever stand in for the other. Defaults to off, so an
    # environment that says nothing about extension capture has none.
    extension_auth: ExtensionAuthSettings = Field(default_factory=ExtensionAuthSettings)

    # Gmail mailbox authorization (env prefix ``GMAIL__``). A third, separate
    # authority: `auth` proves who the operator is, `extension_auth` is the
    # capture extension's own credential, and this is permission to write a
    # draft into a human's mailbox. None of the three may ever stand in for
    # another, which is why each has its own client, its own secret and its own
    # configuration block. Defaults to unconfigured, so an environment that says
    # nothing about Gmail has no mailbox authority at all.
    gmail: GmailSettings = Field(default_factory=GmailSettings)

    # The Google Sheets add-on seam (env prefix ``SHEETS__``). A fourth separate
    # authority, kept apart from the three above for the same reason they are
    # kept apart from each other. Defaults to accepting no audience at all, so an
    # environment that says nothing about Sheets grants nothing to Sheets.
    sheets: SheetsIntegrationSettings = Field(default_factory=SheetsIntegrationSettings)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance for the process lifetime."""

    return Settings()
