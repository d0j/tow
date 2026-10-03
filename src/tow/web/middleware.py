"""Who may ask and how every request is answered: the one HTTP middleware of the app.

For every request: the page language; refusals of a foreign host, the network while it is
closed, an oversized upload and a write from another site; the password for devices on the
network; recovery of an interrupted site transaction; writes one at a time; the security
headers. ``tow.web.app.create_app`` installs ``secure``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from tow import access, i18n
from tow.auth import AuthConfigurationError
from tow.bind import origin_matches_request
from tow.bundle import MAX_BUNDLE_BYTES
from tow.web import _context, services, site_store

_PORTABLE_UPLOAD_REQUEST_LIMIT = MAX_BUNDLE_BYTES + 2 * 1024 * 1024
_PUBLIC_PATHS = {"/favicon.ico", "/healthz", "/login"}
_SITE_HTTP_LOCK = asyncio.Lock()
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Writes that do not take the site lock: signing in only checks the password (its throttle is
# atomic on its own), and must work while a long change holds the lock.
_UNSERIALIZED_WRITES = frozenset({"/login"})


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith("/static/")


def is_browser_navigation(request: Request) -> bool:
    """A page the browser opens (not a fetch of JSON, not a form post)."""
    if request.method not in {"GET", "HEAD"}:
        return False
    if request.url.path.endswith(".json"):
        return False
    return "text/html" in request.headers.get("accept", "").lower()


def _peer_is_global(request: Request) -> bool:
    """The connection itself comes from the internet (a forwarded router port), whatever Host says."""
    try:
        peer = ipaddress.ip_address((request.client.host if request.client else "").split("%", 1)[0])
    except ValueError:
        return False  # not an IP peer (a test transport, a local socket)
    if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
        peer = peer.ipv4_mapped
    return peer.is_global  # loopback, private, link-local and Tailscale (100.64.0.0/10) are not


def _trusted_request_host(request: Request, cfg: dict[str, Any]) -> bool:
    """Reject internet peers and DNS-rebound hostnames; IP literals remain usable on private LANs."""
    if _peer_is_global(request):
        return False
    raw = request.headers.get("host") or ""
    try:
        parsed = urlparse("//" + raw)
        host = (parsed.hostname or "").lower()
        if not host or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return False
        if parsed.port is not None and not (1 <= parsed.port <= 65535):
            return False
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        configured = str(cfg.get("bind") or "").lower()
        machine = socket.gethostname().lower()
        names = {configured, machine, f"{machine}.local"} - {"", "0.0.0.0", "::"}
        return host in names
    # A public address in Host means the request came from the internet (a forwarded router
    # port): TOW is for this PC, the home network and a VPN such as Tailscale (100.64.0.0/10).
    return not address.is_global


def _redirect_for_fetch(request: Request, response: Response) -> Response:
    """A form posted by app.js gets {"redirect": url} instead of a 303 (D6).

    fetch() follows a 303 itself, so the server rendered the next page once for the
    fetch and once more for the location change. Cookies set on the redirect stay.
    """
    if request.headers.get("x-tow-fetch") != "1" or response.status_code not in (302, 303):
        return response
    location = response.headers.get("location")
    if not location:
        return response
    converted = JSONResponse({"redirect": location}, headers={"Cache-Control": "no-store"})
    for cookie in response.headers.getlist("set-cookie"):
        converted.headers.append("set-cookie", cookie)
    return converted


def _refusal(request: Request, cfg: dict[str, Any]) -> Response | None:
    """Who may not ask at all: a foreign host, the network while it is closed, an oversized or
    unsized upload, a write from another site. Checks of the request alone, no I/O."""
    if not _trusted_request_host(request, cfg):
        return Response("untrusted host", status_code=403)
    if not access.network_open(cfg) and not access.is_local(request):
        return Response("LAN access is disabled", status_code=403)
    if request.method == "POST" and request.url.path == "/settings/portable/import":
        try:
            content_length = int(request.headers.get("content-length") or "")
        except ValueError:
            return Response("content length required", status_code=411)
        if content_length <= 0:
            return Response("content length required", status_code=411)
        if content_length > _PORTABLE_UPLOAD_REQUEST_LIMIT:
            return Response("upload too large", status_code=413)
    if request.method in _WRITE_METHODS:
        origin = request.headers.get("origin")
        req_host = request.headers.get("host") or (request.url.hostname or "")
        if not origin_matches_request(origin, req_host, request.url.scheme):
            return Response("forbidden", status_code=403)
    return None


def _session_refusal(request: Request) -> Response | None:
    """A device on the network without a valid session (blocking: decrypts the secrets). No usable
    credential refuses everyone - the sign-in page says why."""
    try:
        credential = access.credential(_context.secrets_or_none())
    except AuthConfigurationError:
        credential = None
    if credential is not None and access.session_is_valid(request, credential.session_key):
        return None
    if is_browser_navigation(request):
        return RedirectResponse("/login", status_code=303, headers={"Cache-Control": "no-store"})
    if credential is None:
        return Response("LAN authentication is not configured", status_code=503, headers={"Cache-Control": "no-store"})
    return Response("authentication required", status_code=401, headers={"Cache-Control": "no-store"})


def _recover_before_dispatch() -> None:
    """An interrupted site transaction is rolled back and a pending secret-undo cleanup retried
    before the request reads anything (blocking: may wait for the persistence lock, which a
    scheduled check holds while it reads and commits)."""
    services.recover_store_transaction()
    if site_store.secret_undo_cleanup_pending():
        services.cleanup_secret_undo()


def _serialized(request: Request) -> bool:
    """Writes take the site lock one at a time; reads never wait for it.

    Every GET handler only reads (verified for 1.19: pages, the status JSON polled by app.js,
    downloads.json, the browser sign-in status, /doctor without probing, the GET form of
    /topics/{id}/tracker-browser-auth which only redirects). What a GET may write itself - the
    pending secret-undo cleanup, the owner's browser language - takes the persistence lock.
    A slow write (a site probe, a client check) therefore no longer holds up Home.
    """
    return request.method in _WRITE_METHODS and request.url.path not in _UNSERIALIZED_WRITES


def _use_request_language(request: Request, cfg: dict[str, Any]) -> None:
    """The page language: the one chosen in Settings, or the browser's (Accept-Language).

    Chosen before any check, so even a refusal is in the visitor's language; remembered for
    the owner's messages only once the request is the owner's (``_remember_request_language``).
    """
    i18n.use(i18n.for_request(cfg, request.headers.get("accept-language")))


def _remember_request_language(request: Request, cfg: dict[str, Any]) -> None:
    """Scheduled messages follow the owner's browser: called only for this PC or a signed-in device."""
    accept = request.headers.get("accept-language")
    if accept and i18n.setting(cfg) == i18n.AUTO and is_browser_navigation(request):
        i18n.remember_browser_language(i18n.for_request(cfg, accept))


async def secure(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    _context.begin()
    # Every file read runs in the thread pool: the event loop serves other requests meanwhile.
    cfg = await run_in_threadpool(_context.config)
    _use_request_language(request, cfg)  # here, not in a worker: it sets this request's language
    if (refused := _refusal(request, cfg)) is not None:
        return refused
    path = request.url.path
    signed_in = access.is_local(request)  # this PC needs no password
    if not signed_in and access.network_open(cfg) and not _is_public_path(path):
        if (refused := await run_in_threadpool(_session_refusal, request)) is not None:
            return refused
        signed_in = True
    if signed_in and request.headers.get("accept-language") and is_browser_navigation(request):
        await run_in_threadpool(_remember_request_language, request, cfg)
    static = path.startswith("/static/")

    async def dispatch() -> Response:
        if not static:  # a static file reads no data: it needs no recovery
            try:
                await run_in_threadpool(_recover_before_dispatch)
            except RuntimeError:
                return Response("TOW site transaction recovery unavailable", status_code=503)
        if request.method in _WRITE_METHODS and not path.startswith("/updates/") and path not in {"/login", "/logout"}:
            try:
                update = await run_in_threadpool(services.web_update_status)
            except RuntimeError:
                return JSONResponse({"error": i18n.t("releases.job_unreadable")}, status_code=503)
            if update.get("active"):
                return JSONResponse({"error": i18n.t("releases.busy")}, status_code=409)
        return await call_next(request)

    if _serialized(request):
        async with _SITE_HTTP_LOCK:
            response = await dispatch()
    else:
        response = await dispatch()
    response = _redirect_for_fetch(request, response)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/static/"):
        # A versioned URL (?v=<content hash>) never changes: the browser keeps it; others revalidate.
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if request.query_params.get("v") else "no-cache"
        )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self';"
        " form-action 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
    )
    return response
