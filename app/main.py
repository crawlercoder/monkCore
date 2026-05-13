"""FastAPI application entrypoint.

Run locally:

    uvicorn app.main:app --reload

Or programmatically:

    python -m app.main
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import api_router
from app.api.v1.health import router as health_router
from app.config import GitlabTokenStorage, Settings, get_settings
from app.db.session import close_db, init_db
from app.errors import register_exception_handlers
from app.logging import configure_logging, get_logger
from app.middleware import RequestContextMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory.

    Accepting an explicit `settings` makes the app easy to test with
    per-test config without monkey-patching the env.
    """
    settings = settings or get_settings()
    configure_logging(settings)
    log = get_logger("app.main")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info(
            "startup",
            extra={
                "service": settings.app_name,
                "version": __version__,
                "environment": settings.environment.value,
                "debug": settings.debug,
            },
        )
        if settings.gitlab_token_storage is GitlabTokenStorage.LOCAL:
            log.warning(
                "gitlab tokens persist as files under %s/.local-gitlab-tokens — "
                "for local development only, never in production",
                settings.workspace_root,
            )
        await init_db(settings)
        try:
            yield
        finally:
            await close_db()
            log.info("shutdown")

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    @app.get("/", include_in_schema=False, summary="Service info")
    async def root() -> dict[str, str]:
        """Human-friendly when opening the API base URL in a browser (not an HTML UI)."""
        return {
            "service": settings.app_name,
            "version": __version__,
            "environment": settings.environment.value,
            "health": "/health",
            "ready": "/ready",
            "api": "/api",
            "docs": "/docs" if not settings.is_production else "(disabled in production)",
        }

    # `/health` at the root for platform probes + versioned copies under `/v1`.
    app.include_router(health_router, tags=["health"])
    app.include_router(api_router, prefix="/api")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_config=None,
    )
