"""The web app: ``create_app()`` builds it from the route modules; ``app`` is the one ``tow serve``
(``uvicorn tow.web:app``), the supervisor and the tests use.

Every route module has its own ``router``; the factory includes them in ``ROUTERS`` order and
sets up everything else - the middleware, the error pages, the static files and the template
globals. Importing a route module registers nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from tow import access
from tow.config import ConfigError
from tow.releases import ReleaseProducer
from tow.snapshots import SnapshotError
from tow.store import StoreCorruptionError, StoreWriteError
from tow.web import (
    middleware,
    routes_auth,
    routes_backup,
    routes_check,
    routes_clients,
    routes_content,
    routes_health,
    routes_history,
    routes_home,
    routes_messages,
    routes_notifiers,
    routes_password,
    routes_protection,
    routes_service,
    routes_settings,
    routes_sites,
    routes_sync,
    routes_topic_login,
    routes_topics,
    routes_undo,
    routes_volumes,
    templating,
)
from tow.web.messages import flash_redirect, resolve_subject
from tow.web.text import t
from tow.yaml_guard import YamlLimitError

# The order routes are matched in: a fixed path before a parameterised one that would match
# it too, and for one path its GET before its POST, as in v1.19 (tests/test_routes.py).
ROUTERS: tuple[APIRouter, ...] = (
    routes_auth.router,
    routes_health.router,
    routes_history.router,
    routes_messages.router,
    routes_home.router,
    routes_sync.router,  # /topics/rows: before any /topics/{tid}
    routes_content.router,
    routes_topics.router,
    routes_topic_login.router,
    routes_undo.router,
    routes_check.router,
    routes_sites.router,
    routes_settings.router,
    routes_protection.router,
    routes_clients.router,
    routes_notifiers.router,
    routes_backup.router,
    routes_service.router,
    routes_password.router,
    routes_volumes.router,
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
    # Release discovery goes on while the server runs, whether or not a page asks (tow.releases).
    producer = ReleaseProducer()
    producer.start()
    reconciler = asyncio.create_task(_reconcile_update_results())
    try:
        yield
    finally:
        reconciler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reconciler
        producer.stop()


UPDATE_RESULT_EVERY_SEC = 15.0


async def _reconcile_update_results() -> None:
    """A finished web update's result reaches Recent messages once per job (its id is the
    operation id), from the update's own job record: the server looks every 15 s, never a page's
    request (a GET stays without side effects)."""
    from tow.web.messages import reconcile_update_results

    while True:
        await asyncio.sleep(UPDATE_RESULT_EVERY_SEC)
        try:
            await run_in_threadpool(reconcile_update_results)
        except Exception:  # noqa: BLE001 - an unreadable job record is tried again; the server keeps serving
            _CONFIG_LOG.warning("the result of a web update could not be added to the messages yet")


async def _store_corruption(_request: Request, exc: StoreCorruptionError) -> Response:
    # The stored error can contain a local path or a damaged value. The browser needs only a
    # stable failure message; recovery details remain with the local diagnostic tools. Plain text in
    # UTF-8, as a sentence: without a charset a browser showed the Russian words as mojibake.
    text = t("web.data_unavailable")
    return PlainTextResponse(text[:1].upper() + text[1:], status_code=503)


async def _snapshot_failed(request: Request, exc: SnapshotError) -> Response:
    """A night copy's error no page caught: an interrupted restore that cannot be checked or
    undone refuses every take of the data lock (Settings, a save), and that was a bare "Internal
    Server Error". Its words name what to do and the files to keep - on this computer; from the
    network, as for damaged data, that the data is unavailable."""
    _CONFIG_LOG.error("TOW data is unavailable: a nightly backup operation failed (%s)", type(exc).__name__)
    text = str(exc) if access.is_local(request) else t("web.data_unavailable")
    return Response(text, status_code=503, media_type="text/plain; charset=utf-8")


def _back_to(request: Request) -> str:
    """The page the owner acted on (the Referer of this site, without its message), else Home."""
    referer = urlsplit(request.headers.get("referer") or "")
    if referer.netloc != request.url.netloc or not referer.path.startswith("/"):
        return "/"
    query = urlencode([(key, value) for key, value in parse_qsl(referer.query) if key != "flash"])
    return referer.path + (f"?{query}" if query else "")


async def _store_write_failed(request: Request, exc: StoreWriteError) -> Response:
    """A single-file save failed (another program keeps the data file open longer than the
    retries, the disk is full): the stores are as they were - say so and to try again, on the
    page the owner acted on, instead of a server error."""
    _CONFIG_LOG.warning("a data file could not be written: %s %s", type(exc.__cause__ or exc).__name__, exc.errno)
    return flash_redirect(_back_to(request), "web.data_busy", "err")


async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """A missing page is a TOW page for a browser (with the way back) and JSON for everything else."""
    if exc.status_code == 404 and middleware.is_browser_navigation(request):
        return templating.TEMPLATES.TemplateResponse(
            request, "not_found.html", {"title": t("not_found.title")}, status_code=404
        )
    return await http_exception_handler(request, exc)


_CONFIG_LOG = logging.getLogger("uvicorn.error")  # serve.log
_config_logged: list[str] = []


def _config_unreadable(request: Request, exc: ConfigError | YamlLimitError) -> Response:
    """config.yaml became unreadable while TOW runs: every page said a bare "Internal Server
    Error" and serve.log nothing. Now a short page (JSON for everything but a browser page)
    names the file, the place and the way back; /healthz still answers - the server itself is
    fine, and a restart could not read the file either. Logged once per distinct problem."""
    from tow import i18n

    with contextlib.suppress(Exception):
        i18n.use(i18n.negotiate(request.headers.get("accept-language")))
    problem = str(exc)
    seen = f"{exc.code} {sorted(exc.params.items())}"
    if seen not in _config_logged:
        _config_logged[:] = [seen]
        _CONFIG_LOG.error("config.yaml cannot be used: %s", exc.text("en"))
    if request.url.path == "/healthz":
        from tow import __version__

        answer: dict[str, object] = {"ok": True, "version": __version__}
        if access.is_local(request):
            from tow.web import services

            answer.update(install=services.install_id(), config_error=problem)
        return JSONResponse(answer, headers={"Cache-Control": "no-store"})
    title = t("web.config_unreadable.title")
    if access.is_local(request):
        lines = [problem, t("web.config_unreadable.recover")]
    else:  # who may ask from the network is in that very file: no detail beyond this computer
        lines = [t("web.config_unreadable.network")]
    headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }
    if not middleware.is_browser_navigation(request):
        return JSONResponse({"ok": False, "error": title, "detail": lines}, status_code=503, headers=headers)
    paragraphs = "".join(f"<p>{escape(line)}</p>" for line in lines)
    # In the reader's colours (a dark theme got a white page) and inside <main>, so a screen
    # reader finds the text (axe: landmark-one-main, region).
    body = (
        f'<!doctype html><html lang="{escape(i18n.current())}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{escape(title)}</title><style>body{{font-family:system-ui,sans-serif;max-width:42rem;"
        "margin:2rem auto;padding:0 1rem;line-height:1.5}</style></head>"
        f"<body><main><h1>{escape(title)}</h1>{paragraphs}</main></body></html>"
    )
    return HTMLResponse(body, status_code=503, headers=headers)


async def _config_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Outside every other middleware: the security middleware itself reads config.yaml first."""
    try:
        return await call_next(request)
    except (ConfigError, YamlLimitError) as exc:
        return _config_unreadable(request, exc)


async def _local_only(_request: Request, exc: access.LocalOnly) -> Response:
    """A this-computer-only route asked from the network (``access.require_local``)."""
    if exc.message:
        return flash_redirect(exc.location, exc.message, exc.kind)
    return RedirectResponse(exc.location, status_code=303)


def create_app() -> FastAPI:
    """A new TOW web app."""
    app = FastAPI(
        title="TOW",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
        # A topic's or a site's route names it for its messages before it changes anything.
        dependencies=[Depends(resolve_subject)],
    )
    # Pages compress about ten times (a long topic list is around a megabyte of HTML). Added
    # first, so it sits inside the security middleware, which sees every response.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.exception_handler(StoreCorruptionError)(_store_corruption)
    app.exception_handler(StoreWriteError)(_store_write_failed)
    app.exception_handler(SnapshotError)(_snapshot_failed)
    app.exception_handler(access.LocalOnly)(_local_only)
    app.exception_handler(StarletteHTTPException)(_http_error)
    app.middleware("http")(middleware.secure)
    app.middleware("http")(_config_guard)  # added last: the outermost
    templating.configure()
    for router in ROUTERS:
        app.include_router(router)
    if templating.STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(templating.STATIC_DIR)), name="static")
    return app


app = create_app()
