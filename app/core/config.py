"""Application configuration.

All configuration is read from environment variables (optionally via a local
``.env`` file). No secrets are committed to source control — see ``.env.example``
for the required variable names.

Phase 0 scope: local development only. The RDS/production variable *names* are
documented here and in ``.env.example`` but no production credentials exist in
the repository.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.features import FeatureFlags


class Settings(BaseSettings):
    """Typed application settings loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
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

    # --- Database ------------------------------------------------------------
    # Local dev default points at the documented local Postgres instance.
    # In staging/production this is supplied by the environment (RDS). The
    # value is a full SQLAlchemy URL; credentials never live in source.
    database_url: str = Field(
        default="postgresql+psycopg://dev@127.0.0.1:5433/vmr_dev",
        description="SQLAlchemy database URL. Supplied by the environment outside local dev.",
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

    def has_millionverifier_key(self) -> bool:
        """True when a non-empty MillionVerifier key is configured (never logs it)."""

        return bool(self.millionverifier_api_key and self.millionverifier_api_key.strip())

    features: FeatureFlags = Field(default_factory=FeatureFlags)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance for the process lifetime."""

    return Settings()
