"""FastAPI application for the VMR outbound operating system."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.phase2 import router as phase2_router
from app.api.routes import router as api_router
from app.core.auth.csrf import CsrfError
from app.core.auth.identity import IdentityProvider
from app.core.auth.middleware import OperatorAuthenticationMiddleware
from app.core.auth.startup import HostedAuthConfigurationError, validate_hosted_auth_settings
from app.core.config import Settings, get_settings
from app.core.health import DatabaseReadinessProbe, ReadinessProbe, run_readiness_probe
from app.core.http import CanonicalTrustedHostMiddleware, ProductionHTTPMiddleware
from app.core.runtime import validate_runtime_settings
from app.web.auth_routes import IDENTITY_PROVIDER_STATE_KEY
from app.web.auth_routes import router as auth_router

# Compatibility alias. The old name described a rule that no longer exists —
# "workbench outside local" — and the new contract covers a wider surface than
# the workbench (see `app/core/auth/startup.py`). Kept so an external caller
# catching the old name still catches the refusal.
WorkbenchConfigurationError = HostedAuthConfigurationError


def create_app(
    settings: Settings | None = None,
    *,
    readiness_probe: ReadinessProbe | None = None,
    identity_provider: IdentityProvider | None = None,
) -> FastAPI:
    """Application factory."""

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

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.debug,
        summary=(
            "Contact-first outbound operations with durable Campaign, Agent, "
            "queue, and pipeline state."
        ),
    )

    # Settings and the identity provider are published on app state so the auth
    # routes resolve exactly the configuration this app was built with, and so a
    # test can inject a deterministic provider with no network.
    app.state.vmr_settings = settings
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
    app.add_middleware(OperatorAuthenticationMiddleware, settings=settings.auth)
    app.add_middleware(CanonicalTrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(
        ProductionHTTPMiddleware,
        max_request_bytes=settings.max_request_bytes,
        trusted_proxy_cidrs=settings.trusted_proxy_cidrs,
        hsts_max_age_seconds=settings.hsts_max_age_seconds,
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

        # Company Intelligence (CI-001) mounts as its own router, behind its own
        # default-off switch on top of `workbench`. Two consequences, both
        # deliberate: while the switch is off the paths do not exist at all
        # (a 404, not a page explaining a disabled feature), and the area can be
        # added or removed without touching a line of the existing workbench.
        if settings.features.company_intelligence:
            from app.web.company_intelligence import router as company_intelligence_router

            app.include_router(company_intelligence_router)

        app.include_router(web_router)

    return app


app = create_app()
