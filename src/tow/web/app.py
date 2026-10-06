"""The web app: ``create_app()`` builds it from the route modules; ``app`` is the one ``tow serve``
(``uvicorn tow.web:app``), the supervisor and the tests use.

Every route module has its own ``router``; the factory includes them in ``ROUTERS`` order and
sets up everything else - the middleware, the error pages, the static files and the template
globals. Importing a route module registers nothing.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import APIRouter, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from tow import access
from tow.store import StoreCorruptionError
from tow.web import (
    middleware,
    routes_auth,
    routes_backup,
    routes_check,
    routes_content,
    routes_health,
    routes_history,
    routes_home,
    routes_notifiers,
    routes_password,
    routes_service,
    routes_settings,
    routes_sites,
    routes_topic_login,
    routes_topics,
    routes_undo,
    templating,
)
from tow.web.text import t
from tow.web.views import flash_redirect

# The order routes are matched in: a fixed path before a parameterised one that would match
# it too, and for one path its GET before its POST, as in v1.19 (tests/test_routes.py).
ROUTERS: tuple[APIRouter, ...] = (
    routes_auth.router,
    routes_health.router,
    routes_history.router,
    routes_home.router,
    routes_content.router,
    routes_topics.router,
    routes_topic_login.router,
    routes_undo.router,
    routes_check.router,
    routes_sites.router,
    routes_settings.router,
    routes_notifiers.router,
    routes_backup.router,
    routes_service.router,
    routes_password.router,
)


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Temporary files (form uploads included) stay inside the install, also when the app is
    served by something else than `tow serve` (which has set this up already); new files are
    for this user only (umask 077 on Linux and macOS; keys/ and data/ closed to other accounts)."""
    from tow.paths import use_private_temp
    from tow.platform import use_private_files
    from tow.store import protect_install_folders

    use_private_files()
    use_private_temp()
    protect_install_folders()
    yield


async def _store_corruption(_request: Request, exc: StoreCorruptionError) -> Response:
    # The stored error can contain a local path or a damaged value. The browser needs only a
    # stable failure message; recovery details remain with the local diagnostic tools.
    return Response(t("web.data_unavailable"), status_code=503)


async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """A missing page is a TOW page for a browser (with the way back) and JSON for everything else."""
    if exc.status_code == 404 and middleware.is_browser_navigation(request):
        return templating.TEMPLATES.TemplateResponse(
            request, "not_found.html", {"title": t("not_found.title")}, status_code=404
        )
    return await http_exception_handler(request, exc)


async def _local_only(_request: Request, exc: access.LocalOnly) -> Response:
    """A this-computer-only route asked from the network (``access.require_local``)."""
    if exc.message:
        return flash_redirect(exc.location, exc.message, exc.kind)
    return RedirectResponse(exc.location, status_code=303)


def create_app() -> FastAPI:
    """A new TOW web app."""
    app = FastAPI(title="TOW", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    # Pages compress about ten times (a long topic list is around a megabyte of HTML). Added
    # first, so it sits inside the security middleware, which sees every response.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.exception_handler(StoreCorruptionError)(_store_corruption)
    app.exception_handler(access.LocalOnly)(_local_only)
    app.exception_handler(StarletteHTTPException)(_http_error)
    app.middleware("http")(middleware.secure)
    templating.configure()
    for router in ROUTERS:
        app.include_router(router)
    if templating.STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(templating.STATIC_DIR)), name="static")
    return app


app = create_app()
