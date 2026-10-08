"""Who may ask and how every request is answered: the one HTTP middleware of the app.

For every request: the page language; refusals of a foreign host, the network while it is
closed, an oversized upload and a write from another site; the password for devices on the
network; recovery of an interrupted site transaction; writes one at a time; the security
headers. ``tow.web.app.create_app`` installs ``secure``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from tow import access, i18n
from tow.auth import SESSION_COOKIE, AuthConfigurationError
from tow.bind import origin_matches_request
from tow.bundle import MAX_BUNDLE_BYTES
from tow.torrent import MAX_TORRENT_BYTES
from tow.web import _context, services, site_store

_PORTABLE_UPLOAD_REQUEST_LIMIT = MAX_BUNDLE_BYTES + 2 * 1024 * 1024
_UPLOAD_LIMITS = {
    "/settings/portable/import": _PORTABLE_UPLOAD_REQUEST_LIMIT,
    "/content/prepare": MAX_TORRENT_BYTES + 1024 * 1024,
}
# Every other write is a form of a few fields. The largest, a topic with the file selection of
# a 20,000-file torrent, stays under 250 KB even at the fields' own limits. Below Starlette's
# 1 MiB spool size a file part someone adds stays in memory: nothing is written to data/tmp.
FORM_REQUEST_LIMIT = 512 * 1024
_PUBLIC_PATHS = {"/favicon.ico", "/healthz", "/login"}
_SITE_HTTP_LOCK = asyncio.Lock()
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Signing in has its own atomic throttle. Content previews never change topics or transfer
# tasks; explicit native magnet retrieval may contact peers. Cache commits use their own
# short persistence_lock, not a lock held during tracker/client I/O. A manual release check
# only refreshes the in-memory release cache (its own lock) after a GitHub request.
_UNSERIALIZED_WRITES = frozenset(
    {"/login", "/content/prepare", "/content/resolve", "/content/snapshot", "/updates/check"}
)
# Actions that wait on the network (a client, a site, a messenger) for up to a minute: they
# read their inputs, talk, and take the persistence lock only to save what they learned,
# re-reading the stores under it (doctor_report, run_check, topics_add, an edit that moves the
# folder in the client, adopting, the tracker login), so another writer's change is never lost.
# Holding the site lock meanwhile froze every save.
_NETWORK_ACTIONS = re.compile(
    r"/(check"
    r"|doctor/run"
    r"|settings/client/ping"
    r"|settings/notifier/[^/]+/test"
    r"|sites/[^/]+/probe"
    r"|topics/add"
    r"|topics/guess-title"
    r"|topics/[^/]+/(check|edit|adopt|replace-revision|tracker-login|tracker-browser-auth))"
)


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


def _body_refusal(request: Request) -> Response | None:
    """A write's body, judged by its headers before anything reads it: an upload needs its size
    and stays under its own limit; any other write is a form of at most FORM_REQUEST_LIMIT, sent
    with its size (or without a body), and signing in takes only a plain form. Starlette parses
    a form before the route runs, so without this anyone who reaches the sign-in page could
    make TOW store a file part of any size in data/tmp."""
    path = request.url.path
    upload = request.method == "POST" and path in _UPLOAD_LIMITS
    declared = request.headers.get("content-length")
    if "transfer-encoding" in request.headers:  # a chunked body says its size only at its end
        return Response("content length required", status_code=411)
    if declared is None and not upload:
        content_length = 0  # HTTP/1.1: no length and no transfer coding is a request without a body
    else:
        try:
            content_length = int(declared or "")
        except ValueError:
            return Response("content length required", status_code=411)
        if content_length < 0 or (upload and content_length == 0):
            return Response("content length required", status_code=411)
    if content_length > (_UPLOAD_LIMITS[path] if upload else FORM_REQUEST_LIMIT):
        return Response("upload too large" if upload else "request too large", status_code=413)
    if path == "/login" and request.method == "POST":
        media_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if media_type != "application/x-www-form-urlencoded":
            return Response("unsupported media type", status_code=415)
    return None


def _refusal(request: Request, cfg: dict[str, Any]) -> Response | None:
    """Who may not ask at all: a foreign host, the network while it is closed, an oversized or
    unsized body, a write from another site. Checks of the request alone, no I/O."""
    if not _trusted_request_host(request, cfg):
        return Response("untrusted host", status_code=403)
    if not access.network_open(cfg) and not access.is_local(request):
        return Response("LAN access is disabled", status_code=403)
    if request.method in _WRITE_METHODS:
        if (refused := _body_refusal(request)) is not None:
            return refused
        origin = request.headers.get("origin")
        req_host = request.headers.get("host") or (request.url.hostname or "")
        if not origin_matches_request(origin, req_host, request.url.scheme):
            return Response("forbidden", status_code=403)
    return None


def _wants_page(request: Request) -> bool:
    """A page or a form (posted by the browser or by app.js), not a fetch of JSON."""
    if request.headers.get("x-tow-fetch") == "1":
        return True
    return not request.url.path.endswith(".json") and "text/html" in request.headers.get("accept", "").lower()


def _session_refusal(request: Request) -> Response | None:
    """A device on the network without a valid session (blocking: decrypts the secrets). No usable
    credential refuses everyone - the sign-in page says why.

    A page or a form goes to the sign-in page - saying "sign in again" when the device had a
    session that ended (expired, signed out elsewhere) - a JSON caller gets the reason as JSON,
    and signing out always ends at the sign-in page with the cookie removed."""
    try:
        credential = access.credential(_context.secrets_or_none())
    except AuthConfigurationError:
        credential = None
    if credential is not None and access.session_is_valid(request, credential.session_key):
        return None
    no_store = {"Cache-Control": "no-store"}
    had_session = bool(request.cookies.get(SESSION_COOKIE))
    if request.url.path == "/logout" or _wants_page(request):
        ended = had_session and credential is not None and request.url.path != "/logout"
        location = "/login?again=1" if ended else "/login"
        response = RedirectResponse(location, status_code=303, headers=no_store)
        if had_session:
            response.delete_cookie(SESSION_COOKIE, path="/")
        # A form app.js posted gets {"redirect": ...}; a page (even one fetched) the 303 itself.
        return _redirect_for_fetch(request, response) if request.method in _WRITE_METHODS else response
    if credential is None:
        return JSONResponse({"error": i18n.t("web.login.no_password")}, status_code=503, headers=no_store)
    return JSONResponse({"error": i18n.t("web.login.again")}, status_code=401, headers=no_store)


def _recover_before_dispatch(undo_cleanup: bool = True) -> None:
    """An interrupted site transaction is rolled back and a pending secret-undo cleanup retried
    before the request reads anything (blocking: may wait for the persistence lock, which a
    scheduled check holds while it reads and commits). The rollback looks for its journal only;
    the cleanup's pre-check reads the whole state, so /healthz (polled by monitors, reading
    nothing) leaves it to the next page."""
    services.recover_store_transaction()
    if undo_cleanup and site_store.secret_undo_cleanup_pending():
        services.cleanup_secret_undo()


def _serialized(request: Request) -> bool:
    """Writes take the site lock one at a time; reads never wait for it.

    Every GET handler only reads (verified for 1.19: pages, the status JSON polled by app.js,
    downloads.json, the browser sign-in status, /doctor without probing, the GET form of
    /topics/{id}/tracker-browser-auth which only redirects). What a GET may write itself - the
    pending secret-undo cleanup, the owner's browser language - takes the persistence lock.
    A slow write (a site probe, a client check) therefore no longer holds up Home, and an
    action that waits on the network does not hold up a save either (``_NETWORK_ACTIONS``).
    """
    path = request.url.path
    return (
        request.method in _WRITE_METHODS
        and path not in _UNSERIALIZED_WRITES
        and _NETWORK_ACTIONS.fullmatch(path) is None
    )


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


def _response_headers(request: Request, response: Response) -> Response:
    """The same policy for dispatched responses and early refusals.

    Only successful static content (including revalidation) can be immutable;
    missing assets and refused requests must not leave cached failures behind.
    """
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if response.status_code >= 400:
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/") and (200 <= response.status_code < 300 or response.status_code == 304):
        # A versioned URL (?v=<content hash>) never changes: others revalidate.
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if request.query_params.get("v") else "no-cache"
        )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self';"
        " form-action 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
    )
    return response


async def secure(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    _context.begin()
    # Every file read runs in the thread pool: the event loop serves other requests meanwhile.
    cfg = await run_in_threadpool(_context.config)
    _use_request_language(request, cfg)  # here, not in a worker: it sets this request's language
    if (refused := _refusal(request, cfg)) is not None:
        return _response_headers(request, refused)
    path = request.url.path
    signed_in = access.is_local(request)  # this PC needs no password
    if not signed_in and access.network_open(cfg) and not _is_public_path(path):
        if (refused := await run_in_threadpool(_session_refusal, request)) is not None:
            return _response_headers(request, refused)
        signed_in = True
    if signed_in and request.headers.get("accept-language") and is_browser_navigation(request):
        await run_in_threadpool(_remember_request_language, request, cfg)
    static = path.startswith("/static/")

    async def dispatch() -> Response:
        if not static:  # a static file reads no data: it needs no recovery
            try:
                await run_in_threadpool(_recover_before_dispatch, path != "/healthz")
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
    return _response_headers(request, _redirect_for_fetch(request, response))
