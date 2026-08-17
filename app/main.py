"""FastAPI application for the VMR outbound operating system."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.integrations_sheets import router as integrations_sheets_router
from app.api.phase2 import router as phase2_router
from app.api.routes import router as api_router
from app.core.auth.accounts import AccountDirectory
from app.core.auth.admin import AdminRequiredError
from app.core.auth.csrf import CsrfError
from app.core.auth.extension_link import ExtensionLinkDirectory
from app.core.auth.identity import IdentityProvider
from app.core.auth.middleware import OperatorAuthenticationMiddleware
from app.core.auth.sheets_assertion import AssertionVerifier, GoogleAssertionVerifier
from app.core.auth.startup import HostedAuthConfigurationError, validate_hosted_auth_settings
from app.core.config import Settings, get_settings
from app.core.health import DatabaseReadinessProbe, ReadinessProbe, run_readiness_probe
from app.core.http import CanonicalTrustedHostMiddleware, ProductionHTTPMiddleware
from app.core.runtime import validate_runtime_settings
from app.services.campaign_access import CampaignAccessError
from app.web.auth_routes import IDENTITY_PROVIDER_STATE_KEY
from app.web.auth_routes import router as auth_router
from app.web.extension_link_routes import router as extension_link_router
from app.web.gmail_routes import router as gmail_router

# Compatibility alias. The old name described a rule that no longer exists —
# "workbench outside local" — and the new contract covers a wider surface than
# the workbench (see `app/core/auth/startup.py`). Kept so an external caller
# catching the old name still catches the refusal.
WorkbenchConfigurationError = HostedAuthConfigurationError

_startup_logger = logging.getLogger("vmr.startup")


def create_app(
    settings: Settings | None = None,
    *,
    readiness_probe: ReadinessProbe | None = None,
    identity_provider: IdentityProvider | None = None,
    account_directory: AccountDirectory | None = None,
    extension_link_directory: ExtensionLinkDirectory | None = None,
    sheets_assertion_verifier: AssertionVerifier | None = None,
) -> FastAPI:
    """Application factory.

    ``account_directory`` is the same kind of seam as ``identity_provider``: the
    live one reads the ``users`` table, and a test injects a deterministic one so
    the authentication boundary can be exercised without a database. Left as
    ``None`` the middleware resolves the database-backed directory lazily, so
    building an app never opens a connection as a side effect.

    ``sheets_assertion_verifier`` is the fourth of the same kind. It answers "is
    this a Google identity assertion minted for our add-on, and whose is it",
    reads Google's published key set rather than a table, and is constructed
    eagerly because it holds only configuration and a lazily-filled key cache —
    building one opens no connection and contacts nothing.

    ``extension_link_directory`` is the third of the same kind, and it exists for
    the same reason: it answers "which VMR account does this ``vmre1`` token
    belong to, and is that account still active", it reads
    ``extension_sessions``, and it is resolved lazily so that importing or
    building the application opens nothing.
    """

    settings = settings or get_settings()

    # Startup contract. Replaces the previous "workbench only when APP_ENV=local"
    # rule, which guarded the UI but said nothing about the state-changing API
    # routes that mount in every environment. Refusing to start is what makes an
    # anonymous-by-accident hosted deployment impossible rather than merely
    # unlikely.
    validate_hosted_auth_settings(settings)
    validate_runtime_settings(settings)

    if readiness_probe is None:
        # Readiness opens a short-lived async driver connection and never uses
        # or changes the application's general-purpose SQLAlchemy pool.
        readiness_probe = DatabaseReadinessProbe(
            settings.database_url,
            timeout_seconds=settings.readiness_timeout_seconds,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Make sure the configured administrator exists, and only that.

        Best-effort by design. A database that is unreachable at boot must not
        stop the process starting — the probes and the sign-in page are exactly
        what an operator needs in that situation, and both work without it. The
        operation is idempotent, so the next successful start converges.

        It is also inert unless hosted authentication is on: a developer running
        locally has no sign-in and does not need an administrator row appearing in
        their database merely because they started the app.

        A lifespan handler rather than ``@app.on_event("startup")``, which is
        deprecated in this FastAPI version and which the test suite turns into an
        error.
        """

        if settings.auth.enabled:
            try:
                from app.db.session import SessionLocal
                from app.services.users import service as user_service

                with SessionLocal() as session:
                    user_service.ensure_bootstrap_admin(
                        session, email=settings.auth.bootstrap_admin_email
                    )
                    user_service.seed_from_allowlist(
                        session, emails=settings.auth.allowed_operator_emails
                    )
                    session.commit()
            except Exception:  # noqa: BLE001 - startup must not depend on the database
                _startup_logger.warning(
                    '{"event":"account_bootstrap_deferred",'
                    '"detail":"the database was not reachable at startup"}'
                )
        yield

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
        summary=(
            "Contact-first outbound operations with durable Campaign, Agent, "
            "queue, and pipeline state."
        ),
    )

    # Settings and the identity provider are published on app state so the auth
    # routes resolve exactly the configuration this app was built with, and so a
    # test can inject a deterministic provider with no network.
    app.state.vmr_settings = settings
    # Also published under the plain name the integration routers read, so a
    # router does not have to know the historical key.
    app.state.settings = settings
    # The Sheets add-on's credential verifier. One instance per application, so a
    # batch of add-on calls shares one cached Google key set instead of fetching
    # it per request. Constructed whatever the feature switch says; the routes
    # refuse with 404 before it is ever consulted.
    app.state.sheets_assertion_verifier = sheets_assertion_verifier or GoogleAssertionVerifier(
        allowed_audiences=settings.sheets.allowed_audiences,
        accepted_issuers=settings.auth.google_issuers,
        timeout_seconds=settings.auth.google_request_timeout_seconds,
    )
    if identity_provider is not None:
        setattr(app.state, IDENTITY_PROVIDER_STATE_KEY, identity_provider)

    # The last middleware added is the outermost user middleware, so the stack
    # runs outside-in as: hardening -> trusted host -> authentication -> routing.
    #
    # * Hardening outermost, unchanged: a 401, a sign-in redirect and a
    #   cross-site refusal all carry the request ID, the security headers and
    #   the access-log line exactly like any other response.
    # * Trusted host next, so a forged `Host` is rejected before any identity is
    #   read and before any URL is built from that host.
    # * Authentication innermost of the three, but still before routing, so the
    #   decision never depends on a route existing. An unmounted path and an
    #   alternate spelling of a protected path are refused identically.
    app.add_middleware(
        OperatorAuthenticationMiddleware,
        settings=settings.auth,
        extension_settings=settings.extension_auth,
        account_directory=account_directory,
        extension_link_directory=extension_link_directory,
        # The environment is passed in rather than re-read, because exactly one
        # decision depends on it inside the boundary: the legacy `vmrx1` shared
        # capture credential verifies only when this is a local deployment. In
        # staging and production it is worth nothing, whatever an environment
        # file still lists.
        app_env=settings.app_env,
    )
    app.add_middleware(CanonicalTrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(
        ProductionHTTPMiddleware,
        max_request_bytes=settings.max_request_bytes,
        trusted_proxy_cidrs=settings.trusted_proxy_cidrs,
        hsts_max_age_seconds=settings.hsts_max_age_seconds,
    )

    @app.exception_handler(AdminRequiredError)
    async def admin_required(request: Request, exc: AdminRequiredError) -> JSONResponse:
        """One shape for every administrator-only refusal.

        A 403 rather than a 404: pretending the users screen does not exist would
        be security through obscurity that also confuses the administrator who
        mistyped their own URL. It names no account and reveals nothing about who
        the administrators are.
        """

        return JSONResponse(
            status_code=403,
            content={
                "error": "admin_required",
                "status": 403,
                "message": "This area is limited to platform administrators.",
            },
        )

    @app.exception_handler(CampaignAccessError)
    async def campaign_access_denied(request: Request, exc: CampaignAccessError) -> JSONResponse:
        """One shape for every campaign refusal, whoever asked and however.

        A 403 rather than a 404, for the reason the exception's own docstring
        records: campaign names are unique and administered, so a 404 would hide
        very little and would send an operator who genuinely needs the campaign
        looking for a broken link instead of asking for the assignment they are
        missing. It names no campaign, no owner and no assignee.

        JSON rather than a rendered page, matching the two refusals either side
        of it: one handler answers a page request, a form POST and an API call
        alike, and a rendered page here would need a template on a code path
        that must keep working while the workbench feature switch is off.
        """

        return JSONResponse(
            status_code=403,
            content={
                "error": "campaign_access_denied",
                "status": 403,
                "message": str(exc),
            },
        )

    @app.exception_handler(CsrfError)
    async def csrf_failed(request: Request, exc: CsrfError) -> JSONResponse:
        """One shape for every CSRF refusal, and never a redirect.

        A redirect on a rejected write would be followed as a GET and could look
        like success to a client that cannot see the address bar.
        """

        return JSONResponse(
            status_code=403,
            content={
                "error": "csrf_failed",
                "status": 403,
                "message": "This request could not be verified. Reload the page and try again.",
            },
        )

    @app.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        """Process liveness only; never touches a dependency or configuration detail."""

        return {"status": "ok"}

    @app.get("/health", tags=["system"], include_in_schema=False)
    def health_compatibility_path() -> dict[str, str]:
        """Legacy path exposing the authoritative minimal liveness contract."""

        return {"status": "ok"}

    async def readiness_response() -> JSONResponse:
        database_status = "ok"
        status_code = 200
        try:
            await run_readiness_probe(readiness_probe)
        except Exception:
            # Raw database/driver errors belong only in internal telemetry. The
            # public response has one stable, bounded failure state.
            database_status = "failed"
            status_code = 503
        ready = status_code == 200
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ready" if ready else "not_ready",
                "checks": {"configuration": "ok", "database": database_status},
            },
        )

    @app.get("/readyz", tags=["system"])
    async def readyz() -> JSONResponse:
        """Readiness for application configuration and the local database dependency."""

        return await readiness_response()

    @app.get("/ready", tags=["system"], include_in_schema=False)
    async def ready_compatibility_path() -> JSONResponse:
        """Legacy path exposing the authoritative hardened readiness contract."""

        return await readiness_response()

    @app.get("/version", tags=["system"])
    def version() -> dict[str, str]:
        """Deployment-provided build identity; never shells out to Git."""

        return {"version": settings.release_id}

    # The sign-in surface. Mounted unconditionally: it is the only way into a
    # hosted deployment, and it must exist even when the workbench switch is
    # off, because the API surface behind it is protected either way.
    app.include_router(auth_router)

    # Gmail mailbox authorization (#267). Mounted unconditionally so that the
    # default-deny boundary covers it whatever the feature switch says: an
    # anonymous caller gets the same 401 here as on any other protected path,
    # and an approved operator gets a 404 while the switch is off. Mounting it
    # conditionally would make the switched-off case answer 404 to anonymous
    # callers too, which tells them the deployment's feature state.
    #
    # These routes are deliberately *not* on the anonymous allow-list in
    # `app/core/auth/policy.py`. The Gmail callback is not a sign-in path.
    app.include_router(gmail_router)

    # Extension account linking. Mounted unconditionally for the same reason the
    # Gmail router is: the default-deny boundary then covers it whatever the
    # feature switch says, an anonymous caller gets the same answer here as on
    # any other path, and the deployment's feature state is not readable from
    # whether a path 404s. Every route on it refuses outright when
    # `EXTENSION_AUTH__LINK_ENABLED` is false.
    #
    # `POST /extension/token` and `POST /extension/revoke` are the only paths on
    # this router that are not session-authenticated, and both are recorded
    # explicitly in `app/core/auth/policy.py` with the reasoning. The two
    # `/extension/authorize` routes are ordinary signed-in operator pages.
    app.include_router(extension_link_router)

    # The Google Sheets add-on. Mounted unconditionally, like the two routers
    # above and for the same reason: the classification in
    # `app/core/auth/policy.py` then covers it whatever the feature switch says,
    # and a disabled deployment answers 404 rather than advertising its feature
    # state through a different refusal. Every route on it refuses outright when
    # `FEATURES__GOOGLE_SHEETS_INTEGRATION` is false.
    #
    # Deliberately not mounted under `/api`, which is administrator-only by
    # policy. This surface is for ordinary accounts acting on their own
    # Campaigns.
    app.include_router(integrations_sheets_router)

    app.include_router(phase2_router)
    app.include_router(api_router)

    # Server-rendered UI. Two surfaces share one feature switch and one static
    # mount, because they are the same application seen by two audiences:
    #
    # * `/app` — the customer-facing interface. This is the default application
    #   experience, so `/` redirects to it.
    # * `/admin` and everything under it — the operator Workbench, unchanged.
    #
    # Both stay behind `features.workbench`, which is what keeps the whole UI
    # disabled unless deliberately enabled for local operation (FND-007 pattern)
    # and is guarded above against any non-local APP_ENV.
    if settings.features.workbench:
        from app.web.routes import router as web_router
        from app.web.v2.routes import router as v2_router

        app.mount(
            "/static",
            StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
            name="static",
        )

        @app.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            """Land on the customer-facing interface.

            A redirect rather than a second copy of the page: there is exactly one
            Today screen, and the admin overview keeps its own address at `/admin`.
            """

            return RedirectResponse("/app", status_code=307)

        # The administrator's account directory. Included before the general v2
        # router so that `/app/admin/...` resolves on the router that carries the
        # `require_admin` dependency, and can never be picked up by a broader
        # pattern added to the v2 router later.
        from app.web.v2.admin_campaign_access import router as admin_campaign_access_router
        from app.web.v2.admin_users import router as admin_users_router

        app.include_router(admin_users_router)
        # Campaign assignment, on the same administrator-only prefix and for the
        # same reason: `/app/admin/...` must resolve on a router that carries
        # `require_admin` before any broader `/app/...` pattern can pick it up.
        app.include_router(admin_campaign_access_router)

        # The v2 router is included first so its `/app/...` paths are matched
        # before any broader admin pattern can shadow them.
        app.include_router(v2_router)

        # The Admin Workbench is the primary operator surface. It is included
        # before the legacy web router on purpose: `/admin` and the new
        # `/admin/...` areas resolve here, while every legacy route the
        # Workbench does not redefine (Agent Studio, imports, verification,
        # captures, knowledge base, local tools, the legacy monitor) continues
        # to resolve in `app.web.routes` unchanged.
        from app.web.admin_workbench import router as admin_workbench_router

        app.include_router(admin_workbench_router)

        # Company Intelligence (CI-001) mounts as its own router on top of
        # `workbench`, so the area can be added or removed without touching a
        # line of the existing workbench.
        #
        # It is mounted unconditionally and gated per request by
        # `require_intelligence_enabled`. Mounting it on
        # `settings.features.company_intelligence` instead made the control
        # unreachable from the product: Company Intelligence is a PRODUCT_CONTROL,
        # an administrator can switch it on in Admin → Configuration, and no
        # database row can re-run `create_app`. On the staging deployment that
        # produced a screen reporting the control as effective while every page in
        # this area answered 404. The observable contract is unchanged — while the
        # control is off these paths do not exist — but the answer now comes from
        # the layer that owns it.
        from app.web.company_intelligence import router as company_intelligence_router

        app.include_router(company_intelligence_router)

        app.include_router(web_router)

    return app


app = create_app()
